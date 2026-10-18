"""Sensor entities for OASE cloud gateways."""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import OaseConfigEntry
from .entity import OaseEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: OaseConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Add gateway diagnostic sensors."""
    async_add_entities(
        entity
        for gateway in entry.runtime_data.data.gateways
        for entity in (
            OaseGatewayType(entry.runtime_data, gateway.id),
            OaseGatewayFirmwareVersion(entry.runtime_data, gateway.id),
            OaseGatewayWifiFirmwareVersion(entry.runtime_data, gateway.id),
        )
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


class OaseGatewayFirmwareVersion(OaseEntity, SensorEntity):
    """Report the controller firmware supplied by the cloud inventory."""

    _attr_name = "Controller firmware"
    _attr_icon = "mdi:chip"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, gateway_id: str) -> None:
        super().__init__(coordinator, gateway_id)
        self._attr_unique_id = f"{gateway_id}_controller_firmware"

    @property
    def native_value(self) -> str | None:
        version = self.coordinator.gateway_firmware_versions.get(self.gateway_id)
        return version.controller if version else None


class OaseGatewayWifiFirmwareVersion(OaseEntity, SensorEntity):
    """Report the Wi-Fi module firmware supplied by the cloud inventory."""

    _attr_name = "Wi-Fi firmware"
    _attr_icon = "mdi:wifi"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, gateway_id: str) -> None:
        super().__init__(coordinator, gateway_id)
        self._attr_unique_id = f"{gateway_id}_wifi_firmware"

    @property
    def native_value(self) -> str | None:
        version = self.coordinator.gateway_firmware_versions.get(self.gateway_id)
        return version.wifi_module if version else None
