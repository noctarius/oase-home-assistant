"""Number entities for controllable OASE EGC pumps."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from pyoase import Device, rdm

from . import OaseConfigEntry
from .entity import OaseEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: OaseConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Add power controls for pumps exposed by the OASE cloud inventory."""
    entities: list[NumberEntity] = []
    for gateway in entry.runtime_data.data.gateways:
        entities.extend(
            OasePumpPowerNumber(entry.runtime_data, gateway.id, device)
            for device in gateway.devices
            if device.device_number is not None and device.can_set_power
        )
    async_add_entities(entities)


class OasePumpPowerNumber(OaseEntity, NumberEntity):
    """Set an attached EGC pump's power level as a percentage."""

    _attr_name = "Power"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_icon = "mdi:pump"
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_mode = "slider"

    def __init__(self, coordinator, gateway_id: str, device: Device) -> None:
        super().__init__(coordinator, gateway_id)
        self.device_number = device.device_number
        self._attr_unique_id = f"{gateway_id}_device_{device.device_number}_power"

    @property
    def _device(self) -> Device | None:
        gateway = self.gateway
        if not gateway:
            return None
        return next(
            (item for item in gateway.devices if item.device_number == self.device_number), None
        )

    @property
    def native_value(self) -> int | None:
        device = self._device
        if not device or not device.pump_state:
            return None
        return rdm.raw_to_percent(device.pump_state.dimmer_value)

    @property
    def available(self) -> bool:
        device = self._device
        return (
            super().available
            and bool(self.gateway and self.gateway.is_online)
            and bool(device and device.is_connected)
        )

    async def async_set_native_value(self, value: float) -> None:
        """Write the pump's raw RDM power parameter through the O-Net relay."""
        await self.coordinator.client.async_set_pump_power(
            self.gateway_id,
            self.device_number,
            rdm.percent_to_raw(value),
        )
        await self.coordinator.async_request_refresh()
