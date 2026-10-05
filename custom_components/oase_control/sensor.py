"""Sensor entities for OASE cloud gateways."""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import OaseConfigEntry
from .entity import OaseEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: OaseConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Add gateway diagnostic sensors."""
    async_add_entities(
        OaseGatewayType(entry.runtime_data, gateway.id)
        for gateway in entry.runtime_data.data.gateways
    )


class OaseGatewayType(OaseEntity, SensorEntity):
    """Report the OASE cloud gateway type for diagnostics and automations."""

    _attr_name = "Gateway type"
    _attr_icon = "mdi:router-wireless"

    def __init__(self, coordinator, gateway_id: str) -> None:
        super().__init__(coordinator, gateway_id)
        self._attr_unique_id = f"{gateway_id}_gateway_type"

    @property
    def native_value(self) -> str | None:
        return self.gateway.gateway_type if self.gateway else None
