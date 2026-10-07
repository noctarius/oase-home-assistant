"""Shared entity helpers."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from pyoase import Device, Gateway

from .coordinator import OaseDataUpdateCoordinator


class OaseEntity(CoordinatorEntity[OaseDataUpdateCoordinator]):
    """Base entity for a gateway or its attached device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: OaseDataUpdateCoordinator, gateway_id: str) -> None:
        super().__init__(coordinator)
        self.gateway_id = gateway_id

    @property
    def gateway(self) -> Gateway | None:
        return self.coordinator.data.gateway(self.gateway_id) if self.coordinator.data else None

    @property
    def device_info(self) -> DeviceInfo:
        gateway = self.gateway
        serial = gateway.serial_number if gateway and gateway.serial_number else self.gateway_id
        name = (
            gateway.info.long_name
            if gateway and gateway.info
            else (gateway.gateway_type if gateway else "OASE gateway")
        )
        info = DeviceInfo(
            identifiers={("oase_control", self.gateway_id)},
            manufacturer="OASE",
            name=name,
            serial_number=serial,
        )
        if gateway:
            info["model"] = gateway.gateway_type
        return info


class OaseDeviceEntity(OaseEntity):
    """Base entity for one device connected to a gateway's EGC bus."""

    def __init__(self, coordinator: OaseDataUpdateCoordinator, gateway_id: str, device: Device) -> None:
        super().__init__(coordinator, gateway_id)
        self.device_number = device.device_number

    @property
    def device(self) -> Device | None:
        gateway = self.gateway
        if not gateway or self.device_number is None:
            return None
        return next(
            (item for item in gateway.devices if item.device_number == self.device_number), None
        )

    @property
    def device_info(self) -> DeviceInfo:
        device = self.device
        name = (
            device.product_name
            if device and device.product_name
            else (device.device_type if device else "OASE EGC device")
        )
        info = DeviceInfo(
            identifiers={("oase_control", self.gateway_id, str(self.device_number))},
            manufacturer="OASE",
            name=name,
            via_device=("oase_control", self.gateway_id),
        )
        if device:
            info["model"] = device.device_type
            if device.article_number is not None:
                info["model_id"] = str(device.article_number)
        return info
