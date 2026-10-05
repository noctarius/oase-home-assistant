"""Switch entities for OASE outlets and attached EGC devices."""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from pyoase import Device, onet

from . import OaseConfigEntry
from .entity import OaseEntity

_SOCKETS = (
    (onet.Socket.SOCKET_1, "Socket 1"),
    (onet.Socket.SOCKET_2, "Socket 2"),
    (onet.Socket.SOCKET_3, "Socket 3"),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: OaseConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Create entities for the inventory present at startup."""
    entities: list[SwitchEntity] = []
    for gateway in entry.runtime_data.data.gateways:
        entities.extend(
            OaseSocketSwitch(entry.runtime_data, gateway.id, socket, name)
            for socket, name in _SOCKETS
        )
        entities.extend(
            OaseDeviceSwitch(entry.runtime_data, gateway.id, device)
            for device in gateway.devices
            if device.device_number is not None and device.can_switch
        )
    async_add_entities(entities)


class OaseSocketSwitch(OaseEntity, SwitchEntity):
    """A mains outlet on an OASE FM-Master."""

    def __init__(self, coordinator, gateway_id: str, socket: onet.Socket, name: str) -> None:
        super().__init__(coordinator, gateway_id)
        self.socket = socket
        self._attr_name = name
        self._attr_unique_id = f"{gateway_id}_socket_{socket.value + 1}"

    @property
    def is_on(self) -> bool | None:
        sockets = self.gateway.sockets if self.gateway else None
        if sockets is None:
            return None
        return (sockets.socket1, sockets.socket2, sockets.socket3)[self.socket.value]

    @property
    def available(self) -> bool:
        return super().available and bool(self.gateway and self.gateway.is_online)

    async def async_turn_on(self, **kwargs) -> None:
        await self.coordinator.client.async_set_socket(self.gateway_id, self.socket, True)
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.client.async_set_socket(self.gateway_id, self.socket, False)
        await self.coordinator.async_request_refresh()


class OaseDeviceSwitch(OaseEntity, SwitchEntity):
    """The EGC-level on/off state of an attached device, such as a pump."""

    def __init__(self, coordinator, gateway_id: str, device: Device) -> None:
        super().__init__(coordinator, gateway_id)
        self.device_number = device.device_number
        self._attr_name = device.product_name or f"{device.device_type} {device.device_number}"
        self._attr_unique_id = f"{gateway_id}_device_{device.device_number}_on"

    @property
    def _device(self) -> Device | None:
        gateway = self.gateway
        if not gateway:
            return None
        return next(
            (item for item in gateway.devices if item.device_number == self.device_number), None
        )

    @property
    def is_on(self) -> bool | None:
        device = self._device
        return device.pump_state.device_on if device and device.pump_state else None

    async def async_turn_on(self, **kwargs) -> None:
        await self.coordinator.client.async_set_device_on(self.gateway_id, self.device_number, True)
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.client.async_set_device_on(self.gateway_id, self.device_number, False)
        await self.coordinator.async_request_refresh()
