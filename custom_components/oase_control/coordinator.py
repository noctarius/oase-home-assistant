"""Coordinator for one OASE cloud account."""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass, replace
import json
import logging
from typing import Any, Awaitable, Mapping

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.helpers.event import async_track_time_interval

from pyoase import Device, Gateway, Inventory, OaseAuthError, OaseError, PumpState, SocketsState, onet, rdm

from .const import CONF_LOCAL_CREDENTIALS, DOMAIN, LOCAL_UPDATE_INTERVAL, UPDATE_INTERVAL
from .local import (
    LocalEgcDeviceState,
    LocalGatewayState,
    OaseLocalTransportError,
    OaseLocalSessionManager,
    local_credentials_from_inventory,
)
from .transport import OaseTransport

_LOGGER = logging.getLogger(__name__)
_RDM_DEVICE_ON = 0xFF
_CONTROLLER_NAME_ATTRIBUTE_ID = 103


@dataclass(frozen=True)
class GatewayFirmwareVersions:
    """Firmware metadata supplied by the cloud inventory."""

    controller: str | None = None
    wifi_module: str | None = None


class OaseDataUpdateCoordinator(DataUpdateCoordinator[Inventory]):
    """Poll the OASE inventory and expose it to all entities."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: OaseTransport,
        local_credentials: dict[str, str] | None = None,
        *,
        config_entry: ConfigEntry | None = None,
    ) -> None:
        super().__init__(
            hass,
            logger=_LOGGER,
            config_entry=config_entry,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
        )
        self.client = client
        self.local_credentials = local_credentials or {}
        self.local_sessions = OaseLocalSessionManager()
        self.local_cloud_connection_enabled: dict[str, bool] = {}
        self.local_wifi_networks: dict[str, str] = {}
        self.gateway_firmware_versions: dict[str, GatewayFirmwareVersions] = {}
        self.gateway_names: dict[str, str] = {}
        self._cancel_local_updates = None
        self._initial_local_update_task: asyncio.Task[None] | None = None
        self._local_read_lock = asyncio.Lock()
        self._local_refresh_running = False
        self._local_refresh_pending = False

    def async_start_local_updates(self) -> None:
        """Start frequent local status reads independently of cloud inventory polls."""
        if self._cancel_local_updates is not None:
            return

        async def _async_update(_now) -> None:
            await self.async_refresh_local_state()

        self._cancel_local_updates = async_track_time_interval(
            self.hass, _async_update, LOCAL_UPDATE_INTERVAL, cancel_on_shutdown=True
        )
        self._initial_local_update_task = self.hass.async_create_background_task(
            self.async_refresh_local_state(), "oase_control_initial_local_read"
        )

    async def async_refresh_local_state(self, *, follow_up: bool = False) -> None:
        """Refresh only local state, preserving the last known inventory topology.

        ``follow_up`` requests another pass when a refresh is already running,
        so a write is never reported with a value read before it was applied.
        """
        if self.data is None or not self.local_credentials:
            return
        if self._local_refresh_running:
            self._local_refresh_pending |= follow_up
            return
        self._local_refresh_running = True
        try:
            while True:
                self._local_refresh_pending = False
                states = await self._async_read_local_states(self.data)
                if states and self.data is not None:
                    self.local_cloud_connection_enabled = {
                        gateway_id: state.cloud_connection_enabled
                        for gateway_id, state in states.items()
                        if isinstance(state, LocalGatewayState)
                        and state.cloud_connection_enabled is not None
                    }
                    self.local_wifi_networks = {
                        gateway_id: state.wifi_network
                        for gateway_id, state in states.items()
                        if isinstance(state, LocalGatewayState) and state.wifi_network is not None
                    }
                    # Overlay onto the current inventory rather than the one the
                    # read started from: a cloud refresh may have completed in
                    # the meantime.  async_set_updated_data is deliberately not
                    # used, because it would reset the cloud polling interval.
                    self.data = _inventory_with_local_states(self.data, states)
                    self.async_update_listeners()
                if not self._local_refresh_pending:
                    return
        finally:
            self._local_refresh_running = False

    async def async_set_device_on(self, gateway_id: str, device_number: int, on: bool) -> None:
        """Set an EGC device locally when possible, otherwise use the cloud relay."""
        if await self._async_set_local_egc_parameter(
            gateway_id, device_number, rdm.Pid.DEVICE_ON, bytes([onet.ON if on else onet.OFF])
        ):
            await self.async_refresh_local_state(follow_up=True)
            return
        await self._async_cloud_command(self.client.async_set_device_on(gateway_id, device_number, on))

    async def async_set_pump_power(self, gateway_id: str, device_number: int, raw: int) -> None:
        """Set a pump's power locally when possible, otherwise use the cloud relay."""
        if await self._async_set_local_egc_parameter(
            gateway_id, device_number, rdm.Pid.PUMP_POWER, bytes([max(0, min(255, int(raw)))])
        ):
            await self.async_refresh_local_state(follow_up=True)
            return
        await self._async_cloud_command(self.client.async_set_pump_power(gateway_id, device_number, raw))

    async def async_set_socket(self, gateway_id: str, socket: onet.Socket, on: bool) -> None:
        """Switch an FM-Master outlet locally when possible, then via cloud."""
        if await self._async_set_local_socket(gateway_id, socket, onet.ON if on else onet.OFF):
            await self.async_refresh_local_state(follow_up=True)
            return
        await self._async_cloud_command(self.client.async_set_socket(gateway_id, socket, on))

    async def async_set_dimmer(self, gateway_id: str, on: bool, brightness: int | None = None) -> None:
        """Set an FM-Master dimmer locally when possible, then via cloud."""
        if await self._async_set_local_dimmer(gateway_id, on, brightness):
            await self.async_refresh_local_state(follow_up=True)
            return

        async def _async_command() -> None:
            if brightness is not None:
                await self.client.async_set_dimmer_value(gateway_id, brightness)
            await self.client.async_set_socket(gateway_id, onet.Socket.DIMMER_ONOFF, on)

        await self._async_cloud_command(_async_command())

    async def _async_cloud_command(self, command: Awaitable[Any]) -> None:
        """Run one cloud relay command and refresh the inventory afterwards."""
        try:
            await command
        except OaseError as err:
            raise HomeAssistantError(f"OASE Cloud command failed: {err}") from err
        await self.async_request_refresh()

    async def async_close(self) -> None:
        """Release local reverse-TLS sessions when the config entry unloads."""
        if self._cancel_local_updates is not None:
            self._cancel_local_updates()
            self._cancel_local_updates = None
        if self._initial_local_update_task is not None:
            self._initial_local_update_task.cancel()
            await asyncio.gather(self._initial_local_update_task, return_exceptions=True)
            self._initial_local_update_task = None
        await self.local_sessions.async_close()
        self.local_cloud_connection_enabled.clear()
        self.local_wifi_networks.clear()
        self.gateway_firmware_versions.clear()
        self.gateway_names.clear()

    async def _async_update_data(self) -> Inventory:
        try:
            raw_inventory = await self._async_get_raw_inventory()
            if raw_inventory is not None:
                inventory = _inventory_with_rdm_capabilities(Inventory.from_dict(raw_inventory), raw_inventory)
                self._async_update_local_credentials(raw_inventory, inventory)
                self._async_update_gateway_firmware_versions(raw_inventory, inventory)
                self._async_update_gateway_names(raw_inventory, inventory)
            else:
                inventory = await self.client.async_get_inventory()
        except OaseAuthError as err:
            local = await self._async_local_fallback()
            if local is not None:
                # Keep local control running, but still ask for new
                # credentials: cloud-only state is frozen until then.
                if self.config_entry is not None:
                    self.config_entry.async_start_reauth(self.hass)
                return local
            raise ConfigEntryAuthFailed("OASE authentication failed") from err
        except OaseError as err:
            local = await self._async_local_fallback()
            if local is not None:
                return local
            raise UpdateFailed(f"Error communicating with OASE Cloud: {err}") from err
        return await self._async_apply_local_reads(inventory)

    @callback
    def _async_update_local_credentials(
        self, raw_inventory: Mapping[str, Any], inventory: Inventory
    ) -> None:
        """Follow the cloud's local credentials, keeping a copy for cloud outages.

        Credentials are persisted in the config entry, limited to gateways still
        in the inventory, and never logged. Setup still needs one successful
        cloud inventory read; afterwards local control survives cloud outages.
        """
        gateway_ids = {gateway.id for gateway in inventory.gateways}
        merged = {**self.local_credentials, **local_credentials_from_inventory(raw_inventory)}
        updated = {gateway_id: value for gateway_id, value in merged.items() if gateway_id in gateway_ids}
        if updated == self.local_credentials:
            return
        self.local_credentials = updated
        if self.config_entry is not None:
            self.hass.config_entries.async_update_entry(
                self.config_entry,
                data={**self.config_entry.data, CONF_LOCAL_CREDENTIALS: updated},
            )

    @callback
    def _async_update_gateway_firmware_versions(
        self, raw_inventory: Mapping[str, Any], inventory: Inventory
    ) -> None:
        """Keep cloud-only firmware metadata aligned with the current inventory."""
        versions = gateway_firmware_versions_from_inventory(raw_inventory)
        gateway_ids = {gateway.id for gateway in inventory.gateways}
        self.gateway_firmware_versions = {
            gateway_id: version for gateway_id, version in versions.items() if gateway_id in gateway_ids
        }

    @callback
    def _async_update_gateway_names(
        self, raw_inventory: Mapping[str, Any], inventory: Inventory
    ) -> None:
        """Keep user-assigned cloud controller names aligned with the inventory."""
        names = gateway_names_from_inventory(raw_inventory)
        gateway_ids = {gateway.id for gateway in inventory.gateways}
        self.gateway_names = {
            gateway_id: name for gateway_id, name in names.items() if gateway_id in gateway_ids
        }

    async def _async_get_raw_inventory(self) -> dict[str, Any] | None:
        """Prefer the raw cloud response when the transport exposes it."""
        get_raw = getattr(self.client, "async_get_inventory_raw", None)
        if get_raw is None:
            return None
        raw_inventory = await get_raw()
        return raw_inventory if isinstance(raw_inventory, dict) else None

    async def _async_local_fallback(self) -> Inventory | None:
        """Keep locally reachable gateways working while the cloud is unavailable.

        Returns ``None`` when no gateway answered locally, so the outage is
        reported instead of the last inventory being presented as current.
        Gateways that did not answer are marked offline for the same reason.
        """
        if self.data is None:
            return None
        states = await self._async_read_local_states(self.data)
        if not states:
            return None
        return replace(
            self.data,
            gateways=[
                _gateway_with_local_state(gateway, states[gateway.id])
                if gateway.id in states
                else replace(gateway, is_online=False)
                for gateway in self.data.gateways
            ],
        )

    async def _async_apply_local_reads(self, inventory: Inventory) -> Inventory:
        """Prefer local EGC state, using cloud runtime reads only as a fallback."""
        states = await self._async_read_local_states(inventory)
        inventory = _inventory_with_local_states(inventory, states)
        return await self._async_apply_cloud_operating_hours(inventory, states)

    async def _async_apply_cloud_operating_hours(
        self, inventory: Inventory, states: Mapping[str, LocalGatewayState]
    ) -> Inventory:
        """Read missing runtime counters through the cloud relay when available."""
        get_hours = getattr(self.client, "async_get_operating_hours", None)
        if get_hours is None:
            return inventory
        locally_read = {
            (gateway_id, device.device_number)
            for gateway_id, state in states.items()
            for device in (state.egc_devices if isinstance(state, LocalGatewayState) else state)
            if device.operating_hours is not None
        }
        values: dict[tuple[str, int], int] = {}
        for gateway in inventory.gateways:
            for device in gateway.devices:
                if device.device_number is None or (gateway.id, device.device_number) in locally_read:
                    continue
                try:
                    value = await get_hours(gateway.id, device.device_number)
                except OaseError:
                    continue
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    values[(gateway.id, device.device_number)] = value
        return _inventory_with_operating_hours(inventory, values)

    async def _async_read_local_states(
        self, inventory: Inventory
    ) -> dict[str, LocalGatewayState]:
        """Read every reachable gateway's local state, keyed by gateway id."""
        await self.local_sessions.async_prune({gateway.id for gateway in inventory.gateways})
        valid_gateway_ids = {gateway.id for gateway in inventory.gateways}
        self.local_cloud_connection_enabled = {
            gateway_id: enabled
            for gateway_id, enabled in self.local_cloud_connection_enabled.items()
            if gateway_id in valid_gateway_ids
        }
        self.local_wifi_networks = {
            gateway_id: network
            for gateway_id, network in self.local_wifi_networks.items()
            if gateway_id in valid_gateway_ids
        }
        if not self.local_credentials:
            return {}
        # A periodic status refresh can overlap a cloud inventory refresh.  The
        # controller speaks a request/reply protocol over one TLS stream, so
        # serialise reads to prevent replies being consumed by the wrong caller.
        async with self._local_read_lock:
            states: dict[str, LocalGatewayState] = {}
            for gateway in inventory.gateways:
                credential = self.local_credentials.get(gateway.id)
                if not credential or not gateway.serial_number:
                    continue
                try:
                    states[gateway.id] = await self.local_sessions.async_read_gateway_state(
                        gateway.id,
                        gateway.serial_number,
                        credential,
                        read_sockets=gateway.is_fm_master,
                    )
                except OaseLocalTransportError:
                    continue
            return states

    async def _async_set_local_egc_parameter(
        self, gateway_id: str, device_number: int, pid: int, value: bytes
    ) -> bool:
        """Write locally only when a discovered gateway has cached credentials."""
        gateway = self.data.gateway(gateway_id) if self.data else None
        credential = self.local_credentials.get(gateway_id)
        if not gateway or not gateway.serial_number or not credential:
            return False
        try:
            await self.local_sessions.async_set_egc_parameter(
                gateway_id, gateway.serial_number, credential, device_number, pid, value
            )
        except OaseLocalTransportError:
            return False
        return True

    async def _async_set_local_socket(
        self, gateway_id: str, socket: onet.Socket, value: int
    ) -> bool:
        """Write a socket locally only on an FM-Master with cached credentials."""
        gateway = self.data.gateway(gateway_id) if self.data else None
        credential = self.local_credentials.get(gateway_id)
        if not gateway or not gateway.is_fm_master or not gateway.serial_number or not credential:
            return False
        try:
            await self.local_sessions.async_set_socket(
                gateway_id, gateway.serial_number, credential, socket, value
            )
        except OaseLocalTransportError:
            return False
        return True

    async def _async_set_local_dimmer(
        self, gateway_id: str, on: bool, brightness: int | None
    ) -> bool:
        """Write the dimmer locally only on an FM-Master with cached credentials."""
        gateway = self.data.gateway(gateway_id) if self.data else None
        credential = self.local_credentials.get(gateway_id)
        if not gateway or not gateway.is_fm_master or not gateway.serial_number or not credential:
            return False
        try:
            await self.local_sessions.async_set_dimmer(
                gateway_id, gateway.serial_number, credential, on, brightness
            )
        except OaseLocalTransportError:
            return False
        return True


def _inventory_with_rdm_capabilities(
    inventory: Inventory, raw_inventory: Mapping[str, Any]
) -> Inventory:
    """Add only RDM capabilities and values confirmed by the cloud cache."""
    raw_gateways = raw_inventory.get("gateways")
    if not isinstance(raw_gateways, list):
        return inventory
    raw_by_gateway = {
        item.get("id"): item for item in raw_gateways if isinstance(item, Mapping)
    }
    enriched_gateways = [
        _gateway_with_rdm_capabilities(gateway, raw_by_gateway.get(gateway.id))
        for gateway in inventory.gateways
    ]
    return replace(inventory, gateways=enriched_gateways)


def gateway_firmware_versions_from_inventory(
    raw_inventory: Mapping[str, Any],
) -> dict[str, GatewayFirmwareVersions]:
    """Extract gateway firmware fields retained only by the cloud response."""
    raw_gateways = raw_inventory.get("gateways")
    if not isinstance(raw_gateways, list):
        return {}
    versions: dict[str, GatewayFirmwareVersions] = {}
    for gateway in raw_gateways:
        if not isinstance(gateway, Mapping) or not isinstance(gateway.get("id"), str):
            continue
        reported_data = gateway.get("reportedData")
        if not isinstance(reported_data, Mapping):
            continue
        controller = _format_controller_firmware(reported_data.get("fwVersion"))
        wifi_module = _reported_value_as_string(reported_data.get("wiFiModuleFwVersion"))
        if controller is not None or wifi_module is not None:
            versions[gateway["id"]] = GatewayFirmwareVersions(controller, wifi_module)
    return versions


def gateway_names_from_inventory(raw_inventory: Mapping[str, Any]) -> dict[str, str]:
    """Extract user-assigned controller names retained in cloud custom attributes."""
    raw_gateways = raw_inventory.get("gateways")
    if not isinstance(raw_gateways, list):
        return {}
    names: dict[str, str] = {}
    for gateway in raw_gateways:
        if not isinstance(gateway, Mapping) or not isinstance(gateway.get("id"), str):
            continue
        attributes = gateway.get("customAttributesJson")
        try:
            parsed = json.loads(attributes) if isinstance(attributes, str) else attributes
        except json.JSONDecodeError:
            continue
        if not isinstance(parsed, list):
            continue
        for attribute in parsed:
            if not isinstance(attribute, Mapping) or attribute.get("Id") != _CONTROLLER_NAME_ATTRIBUTE_ID:
                continue
            value = attribute.get("Value")
            name = value.get("Value") if isinstance(value, Mapping) else None
            if isinstance(name, str) and (name := name.strip()):
                names[gateway["id"]] = name
            break
    return names


def _format_controller_firmware(record: Any) -> str | None:
    """Format the numeric controller version carried by the cloud inventory."""
    value = record.get("value") if isinstance(record, Mapping) else None
    if not isinstance(value, Mapping):
        return None
    high, low = value.get("firmwareHi"), value.get("firmwareLow")
    if not isinstance(high, int) or not isinstance(low, int):
        return None
    return f"E{high}.{low}"


def _reported_value_as_string(record: Any) -> str | None:
    """Return a non-empty scalar reported value without coercing structures."""
    value = record.get("value") if isinstance(record, Mapping) else None
    if isinstance(value, (str, int, float)) and str(value):
        return str(value)
    return None


def _gateway_with_rdm_capabilities(gateway: Gateway, raw_gateway: Any) -> Gateway:
    """Merge one gateway's cached RDM parameters into its devices."""
    if not isinstance(raw_gateway, Mapping) or not isinstance(raw_gateway.get("devices"), list):
        return gateway
    raw_devices = {
        item.get("deviceNumber"): item
        for item in raw_gateway["devices"]
        if isinstance(item, Mapping)
    }
    return replace(
        gateway,
        devices=[
            _device_with_rdm_capabilities(device, raw_devices.get(device.device_number))
            for device in gateway.devices
        ],
    )


def _device_with_rdm_capabilities(device: Device, raw_device: Any) -> Device:
    """Use successful cached RDM reads as evidence for safe device controls."""
    if not isinstance(raw_device, Mapping) or not isinstance(raw_device.get("rdmData"), list):
        return device
    values: dict[int, bytes] = {}
    for record in raw_device["rdmData"]:
        if not isinstance(record, Mapping):
            continue
        key, value = record.get("key"), record.get("value")
        parameter_id = key.get("parameterId") if isinstance(key, Mapping) else None
        encoded = value.get("value") if isinstance(value, Mapping) else None
        if not isinstance(parameter_id, int) or not isinstance(encoded, str):
            continue
        try:
            values[parameter_id] = base64.b64decode(encoded, validate=True)
        except ValueError:
            continue

    supported_pids = tuple(sorted(set(device.supported_pids) | set(values)))
    device_on = values.get(int(rdm.Pid.DEVICE_ON))
    power = values.get(int(rdm.Pid.PUMP_POWER))
    if device.pump_state is None and (device_on or power):
        return replace(
            device,
            supported_pids=supported_pids,
            pump_state=PumpState(
                device_on=_rdm_device_is_on(device_on),
                dimmer_value=power[0] if power else 0,
            ),
        )
    return replace(device, supported_pids=supported_pids)


def _inventory_with_local_states(
    inventory: Inventory, states: Mapping[str, LocalGatewayState]
) -> Inventory:
    """Overlay local reads on the gateways they were read from."""
    if not states:
        return inventory
    return replace(
        inventory,
        gateways=[
            _gateway_with_local_state(gateway, states[gateway.id]) if gateway.id in states else gateway
            for gateway in inventory.gateways
        ],
    )


def _gateway_with_local_state(
    gateway: Gateway, state: LocalGatewayState | tuple[LocalEgcDeviceState, ...]
) -> Gateway:
    """Overlay confirmed local EGC reads on a cloud inventory gateway."""
    if isinstance(state, tuple):
        state = LocalGatewayState(state)
    by_device_number = {item.device_number: item for item in state.egc_devices}
    sockets = (
        SocketsState(
            socket1=state.sockets.socket1,
            socket2=state.sockets.socket2,
            socket3=state.sockets.socket3,
            dimmer_on=state.sockets.dimmer_on,
            dimmer_value=state.sockets.dimmer_value,
        )
        if state.sockets is not None
        else gateway.sockets
    )
    return replace(
        gateway,
        is_online=True,
        sockets=sockets,
        devices=[
            _device_with_local_state(device, by_device_number.get(device.device_number))
            for device in gateway.devices
        ],
    )


def _device_with_local_state(device: Device, state: LocalEgcDeviceState | None) -> Device:
    """Overlay local parameters only when that device was found on the EGC bus."""
    if state is None:
        return replace(device, is_connected=False)
    supported_pids = set(device.supported_pids)
    if state.device_on_raw is not None:
        supported_pids.add(rdm.Pid.DEVICE_ON)
    if state.pump_power_raw is not None:
        supported_pids.add(rdm.Pid.PUMP_POWER)
    previous = device.pump_state or PumpState()
    return replace(
        device,
        is_connected=True,
        supported_pids=tuple(sorted(supported_pids)),
        pump_state=PumpState(
            device_on=_rdm_device_is_on(state.device_on_raw)
            if state.device_on_raw is not None
            else previous.device_on,
            dimmer_value=state.pump_power_raw[0]
            if state.pump_power_raw
            else previous.dimmer_value,
            fc_mode=previous.fc_mode,
            fc_status=previous.fc_status,
            timestamp=previous.timestamp,
        ),
        operating_hours=(
            state.operating_hours if state.operating_hours is not None else device.operating_hours
        ),
        product_name=state.model_description or device.product_name,
    )


def _inventory_with_operating_hours(
    inventory: Inventory, values: Mapping[tuple[str, int], int]
) -> Inventory:
    """Overlay cloud-relayed runtime counters that could not be read locally."""
    if not values:
        return inventory
    return replace(
        inventory,
        gateways=[
            replace(
                gateway,
                devices=[
                    replace(device, operating_hours=values.get((gateway.id, device.device_number)))
                    if device.device_number is not None and (gateway.id, device.device_number) in values
                    else device
                    for device in gateway.devices
                ],
            )
            for gateway in inventory.gateways
        ],
    )


def _rdm_device_is_on(value: bytes | None) -> bool:
    """Decode the EGC device-state value (0xFF=on, 0x02=off)."""
    return bool(value) and value[0] == _RDM_DEVICE_ON
