"""Binary sensors for OASE cloud gateways."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from pyoase import Device

from . import OaseConfigEntry
from .entity import OaseDeviceEntity, OaseEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: OaseConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Add connectivity indicators for gateways and their EGC devices."""
    entities: list[BinarySensorEntity] = []
    for gateway in entry.runtime_data.data.gateways:
        entities.append(OaseGatewayOnline(entry.runtime_data, gateway.id))
        entities.extend(
            OaseDeviceConnected(entry.runtime_data, gateway.id, device)
            for device in gateway.devices
            if device.device_number is not None
        )
    async_add_entities(entities)


class OaseGatewayOnline(OaseEntity, BinarySensorEntity):
    """Whether a gateway is reachable through OASE Cloud."""

    _attr_name = "Online"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator, gateway_id: str) -> None:
        super().__init__(coordinator, gateway_id)
        self._attr_unique_id = f"{gateway_id}_online"

    @property
    def is_on(self) -> bool | None:
        return self.gateway.is_online if self.gateway else None


class OaseDeviceConnected(OaseDeviceEntity, BinarySensorEntity):
    """Whether one EGC-bus device is currently seen by its gateway."""

    _attr_name = "Connected"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = "diagnostic"

    def __init__(self, coordinator, gateway_id: str, device: Device) -> None:
        super().__init__(coordinator, gateway_id, device)
        self._attr_unique_id = f"{gateway_id}_device_{device.device_number}_connected"

    @property
    def is_on(self) -> bool | None:
        device = self.device
        return device.is_connected if device else None
