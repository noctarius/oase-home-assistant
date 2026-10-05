"""Tests for OASE pump power entities."""

from unittest.mock import AsyncMock

from pyoase import Device, Gateway, Inventory, PumpState

from custom_components.oase_control.coordinator import OaseDataUpdateCoordinator
from custom_components.oase_control.number import OasePumpPowerNumber


async def test_pump_power_number_converts_percent_to_onet_raw_value(hass):
    """Pump power is presented as a percentage and sent as an O-Net byte."""
    device = Device(
        id="pump-id",
        device_number=12345,
        article_number=70789,
        device_type="GardenPump",
        is_connected=True,
        is_active=True,
        pump_state=PumpState(device_on=True, dimmer_value=128),
        has_rdm=True,
        custom_attributes=None,
        supported_pids=(0x1010, 0x8039),
    )
    gateway = Gateway(
        id="gateway-id",
        serial_number="500000000000",
        article_number=70788,
        gateway_type="FmMasterWLanEgcCloudEsp",
        is_online=True,
        online_event_time=None,
        sockets=None,
        devices=[device],
    )
    client = AsyncMock()
    coordinator = OaseDataUpdateCoordinator(hass, client)
    coordinator.data = Inventory(user=None, gateways=[gateway])
    coordinator.async_request_refresh = AsyncMock()
    entity = OasePumpPowerNumber(coordinator, gateway.id, device)

    assert entity.native_value == 51

    await entity.async_set_native_value(50)

    client.async_set_pump_power.assert_awaited_once_with(gateway.id, device.device_number, 127)
    coordinator.async_request_refresh.assert_awaited_once()
