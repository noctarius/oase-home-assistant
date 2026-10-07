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
from .transport import OaseTransport

_LOGGER = logging.getLogger(__name__)


class OaseDataUpdateCoordinator(DataUpdateCoordinator[Inventory]):
    """Poll the OASE inventory and expose it to all entities."""

    def __init__(self, hass: HomeAssistant, client: OaseTransport) -> None:
        super().__init__(
            hass,
            logger=_LOGGER,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
        )
        self.client = client

    async def _async_update_data(self) -> Inventory:
        try:
            raw_inventory = await self._async_get_raw_inventory()
            if raw_inventory is not None:
                return _inventory_with_rdm_capabilities(Inventory.from_dict(raw_inventory), raw_inventory)
            return await self.client.async_get_inventory()
        except OaseAuthError as err:
            raise ConfigEntryAuthFailed("OASE authentication failed") from err
        except OaseError as err:
            raise UpdateFailed(f"Error communicating with OASE Cloud: {err}") from err

    async def _async_get_raw_inventory(self) -> dict[str, Any] | None:
        """Prefer the raw cloud response when the transport exposes it."""
        get_raw = getattr(self.client, "async_get_inventory_raw", None)
        if get_raw is None:
            return None
        raw_inventory = await get_raw()
        return raw_inventory if isinstance(raw_inventory, dict) else None


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
                device_on=bool(device_on and device_on[0]),
                dimmer_value=power[0] if power else 0,
            ),
        )
    return replace(device, supported_pids=supported_pids)
