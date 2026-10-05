"""Binary sensors for OASE cloud gateways."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import OaseConfigEntry
from .entity import OaseEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: OaseConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Add gateway connectivity indicators."""
    async_add_entities(
        OaseGatewayOnline(entry.runtime_data, gateway.id)
        for gateway in entry.runtime_data.data.gateways
    )


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
