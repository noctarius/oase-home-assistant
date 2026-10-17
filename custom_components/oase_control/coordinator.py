"""Coordinator for one OASE cloud account."""

from __future__ import annotations

import asyncio
import base64
from dataclasses import replace
import logging
from typing import Any, Awaitable, Mapping

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.helpers.event import async_track_time_interval

from pyoase import Device, Gateway, Inventory, OaseAuthError, OaseError, PumpState, onet, rdm

from .const import DOMAIN, LOCAL_UPDATE_INTERVAL, UPDATE_INTERVAL
from .local import (
    LocalEgcDeviceState,
    OaseLocalTransportError,
    OaseLocalSessionManager,
)
from .transport import OaseTransport

_LOGGER = logging.getLogger(__name__)
_RDM_DEVICE_ON = 0xFF


class OaseDataUpdateCoordinator(DataUpdateCoordinator[Inventory]):
    """Poll the OASE inventory and expose it to all entities."""

    def __init__(
        self, hass: HomeAssistant, client: OaseTransport, local_credentials: dict[str, str] | None = None
    ) -> None:
        super().__init__(
            hass,
            logger=_LOGGER,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
        )
        self.client = client
        self.local_credentials = local_credentials or {}
        self.local_sessions = OaseLocalSessionManager()
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
        """Switch an FM-Master outlet through the cloud relay."""
        await self._async_cloud_command(self.client.async_set_socket(gateway_id, socket, on))

    async def async_set_dimmer(self, gateway_id: str, on: bool, brightness: int | None = None) -> None:
        """Set an FM-Master dimmer's level, then its on/off state, through the cloud relay."""

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

    async def _async_update_data(self) -> Inventory:
        try:
            raw_inventory = await self._async_get_raw_inventory()
            if raw_inventory is not None:
                inventory = _inventory_with_rdm_capabilities(Inventory.from_dict(raw_inventory), raw_inventory)
            else:
                inventory = await self.client.async_get_inventory()
        except OaseAuthError as err:
            local = await self._async_local_fallback()
            if local is not None:
                return local
            raise ConfigEntryAuthFailed("OASE authentication failed") from err
        except OaseError as err:
            local = await self._async_local_fallback()
            if local is not None:
                return local
            raise UpdateFailed(f"Error communicating with OASE Cloud: {err}") from err
        return await self._async_apply_local_reads(inventory)

    async def _async_get_raw_inventory(self) -> dict[str, Any] | None:
        """Prefer the raw cloud response when the transport exposes it."""
        get_raw = getattr(self.client, "async_get_inventory_raw", None)
        if get_raw is None:
            return None
        raw_inventory = await get_raw()
        return raw_inventory if isinstance(raw_inventory, dict) else None

    async def _async_local_fallback(self) -> Inventory | None:
        """Keep an already loaded inventory working when only cloud is unavailable."""
        if self.data is None:
            return None
        return await self._async_apply_local_reads(self.data)

    async def _async_apply_local_reads(self, inventory: Inventory) -> Inventory:
        """Prefer confirmed local EGC state without making a local failure fatal."""
        return _inventory_with_local_states(inventory, await self._async_read_local_states(inventory))

    async def _async_read_local_states(
        self, inventory: Inventory
    ) -> dict[str, tuple[LocalEgcDeviceState, ...]]:
        """Read every reachable gateway's EGC bus, keyed by gateway id."""
        await self.local_sessions.async_prune({gateway.id for gateway in inventory.gateways})
        if not self.local_credentials:
            return {}
        # A periodic status refresh can overlap a cloud inventory refresh.  The
        # controller speaks a request/reply protocol over one TLS stream, so
        # serialise reads to prevent replies being consumed by the wrong caller.
        async with self._local_read_lock:
            states: dict[str, tuple[LocalEgcDeviceState, ...]] = {}
            for gateway in inventory.gateways:
                credential = self.local_credentials.get(gateway.id)
                if not credential or not gateway.serial_number:
                    continue
                try:
                    states[gateway.id] = await self.local_sessions.async_read_egc_devices(
                        gateway.id, gateway.serial_number, credential
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
    inventory: Inventory, states: Mapping[str, tuple[LocalEgcDeviceState, ...]]
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
    gateway: Gateway, states: tuple[LocalEgcDeviceState, ...]
) -> Gateway:
    """Overlay confirmed local EGC reads on a cloud inventory gateway."""
    by_device_number = {state.device_number: state for state in states}
    return replace(
        gateway,
        is_online=True,
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
    )


def _rdm_device_is_on(value: bytes | None) -> bool:
    """Decode the EGC device-state value (0xFF=on, 0x02=off)."""
    return bool(value) and value[0] == _RDM_DEVICE_ON
