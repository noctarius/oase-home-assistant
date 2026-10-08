"""Coordinator for one OASE cloud account."""

from __future__ import annotations

import base64
from dataclasses import replace
import logging
from typing import Any, Mapping

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from pyoase import Device, Gateway, Inventory, OaseAuthError, OaseError, PumpState, rdm

from .const import DOMAIN, UPDATE_INTERVAL
from .local import (
    LocalEgcDeviceState,
    OaseLocalTransportError,
    async_discover_controllers,
    async_read_local_egc_devices,
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
        if not self.local_credentials:
            return inventory
        try:
            discovered = await async_discover_controllers()
        except (OSError, OaseLocalTransportError):
            return inventory
        hosts = {result.info.serial_number: result.host for result in discovered}
        gateways: list[Gateway] = []
        for gateway in inventory.gateways:
            credential = self.local_credentials.get(gateway.id)
            host = hosts.get(gateway.serial_number)
            if not credential or not host:
                gateways.append(gateway)
                continue
            try:
                states = await async_read_local_egc_devices(host, credential)
            except OaseLocalTransportError:
                gateways.append(gateway)
                continue
            gateways.append(_gateway_with_local_state(gateway, states))
        return replace(inventory, gateways=gateways)


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
