"""Sensor entities for OASE cloud gateways."""

from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import EntityCategory, SIGNAL_STRENGTH_DECIBELS_MILLIWATT, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import OaseConfigEntry
from .entity import OaseDeviceEntity, OaseEntity


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
            OaseGatewayWifiNetwork(entry.runtime_data, gateway.id),
            OaseGatewayWifiSignalStrength(entry.runtime_data, gateway.id),
            OaseGatewayWifiSignalQuality(entry.runtime_data, gateway.id),
            *(
                OaseOecNetworkId(entry.runtime_data, gateway.id, device)
                for device in gateway.devices
                if device.device_number is not None
            ),
            *(
                OaseOperatingHours(entry.runtime_data, gateway.id, device)
                for device in gateway.devices
                if device.device_number is not None
            ),
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


class OaseGatewayWifiNetwork(OaseEntity, SensorEntity):
    """Report the locally configured router SSID without its password."""

    _attr_name = "Wi-Fi network"
    _attr_icon = "mdi:wifi"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, gateway_id: str) -> None:
        super().__init__(coordinator, gateway_id)
        self._attr_unique_id = f"{gateway_id}_wifi_network"

    @property
    def native_value(self) -> str | None:
        return self.coordinator.local_wifi_networks.get(self.gateway_id)


class OaseGatewayWifiSignalStrength(OaseEntity, SensorEntity):
    """Report the controller Wi-Fi RSSI supplied by its local ALIVE reply."""

    _attr_name = "Wi-Fi signal strength"
    _attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
    _attr_native_unit_of_measurement = SIGNAL_STRENGTH_DECIBELS_MILLIWATT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:wifi"

    def __init__(self, coordinator, gateway_id: str) -> None:
        super().__init__(coordinator, gateway_id)
        self._attr_unique_id = f"{gateway_id}_wifi_signal_strength"

    @property
    def native_value(self) -> int | None:
        return self.coordinator.local_wifi_rssi.get(self.gateway_id)


class OaseGatewayWifiSignalQuality(OaseEntity, SensorEntity):
    """Report the app-compatible coarse Wi-Fi signal label."""

    _attr_name = "Wi-Fi signal quality"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:wifi"

    def __init__(self, coordinator, gateway_id: str) -> None:
        super().__init__(coordinator, gateway_id)
        self._attr_unique_id = f"{gateway_id}_wifi_signal_quality"

    @property
    def native_value(self) -> str | None:
        rssi = self.coordinator.local_wifi_rssi.get(self.gateway_id)
        if rssi is None:
            return None
        if rssi <= -80:
            return "Weak"
        if rssi > -60:
            return "Strong"
        return "Medium"


class OaseOperatingHours(OaseDeviceEntity, SensorEntity):
    """Report an EGC device's accumulated operating hours."""

    _attr_name = "Operating hours"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.HOURS
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:timer-outline"

    def __init__(self, coordinator, gateway_id: str, device) -> None:
        super().__init__(coordinator, gateway_id, device)
        self._attr_unique_id = f"{gateway_id}_device_{device.device_number}_operating_hours"

    @property
    def native_value(self) -> int | None:
        device = self.device
        return device.operating_hours if device else None


class OaseOecNetworkId(OaseDeviceEntity, SensorEntity):
    """Report the OASE Easy Control identifier of an EGC device."""

    _attr_name = "OEC network ID"
    _attr_icon = "mdi:identifier"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, gateway_id: str, device) -> None:
        super().__init__(coordinator, gateway_id, device)
        self._attr_unique_id = f"{gateway_id}_device_{device.device_number}_oec_network_id"

    @property
    def native_value(self) -> int | None:
        device = self.device
        return device.device_number if device else None
