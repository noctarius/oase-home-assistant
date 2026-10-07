"""Light entity for an OASE FM-Master dimmable outlet."""

from __future__ import annotations

from homeassistant.components.light import ATTR_BRIGHTNESS, ColorMode, LightEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from pyoase import onet

from . import OaseConfigEntry
from .entity import OaseEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: OaseConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Add the dimmable outlet only for FM-Master gateways."""
    async_add_entities(
        OaseDimmerLight(entry.runtime_data, gateway.id)
        for gateway in entry.runtime_data.data.gateways
        if gateway.is_fm_master
    )


class OaseDimmerLight(OaseEntity, LightEntity):
    """The dimmable mains outlet exposed as a Home Assistant light."""

    _attr_name = "Dimmer"
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}
    _attr_color_mode = ColorMode.BRIGHTNESS

    def __init__(self, coordinator, gateway_id: str) -> None:
        super().__init__(coordinator, gateway_id)
        self._attr_unique_id = f"{gateway_id}_dimmer"

    @property
    def available(self) -> bool:
        return super().available and bool(self.gateway and self.gateway.is_online)

    @property
    def is_on(self) -> bool | None:
        return self.gateway.sockets.dimmer_on if self.gateway and self.gateway.sockets else None

    @property
    def brightness(self) -> int | None:
        return self.gateway.sockets.dimmer_value if self.gateway and self.gateway.sockets else None

    async def async_turn_on(self, **kwargs) -> None:
        brightness = kwargs.get(ATTR_BRIGHTNESS)
        if brightness is not None:
            await self.coordinator.client.async_set_dimmer_value(self.gateway_id, brightness)
        await self.coordinator.client.async_set_socket(self.gateway_id, onet.Socket.DIMMER_ONOFF, True)
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.client.async_set_socket(self.gateway_id, onet.Socket.DIMMER_ONOFF, False)
        await self.coordinator.async_request_refresh()
