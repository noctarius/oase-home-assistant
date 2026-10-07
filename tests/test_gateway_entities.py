"""Tests for gateway-type-specific entities."""

from pyoase import Gateway, Inventory

from custom_components.oase_control.light import async_setup_entry as async_setup_lights
from custom_components.oase_control.switch import async_setup_entry as async_setup_switches


async def test_cloud_esp_does_not_create_fm_master_outlets(hass):
    """GatewayCloudEsp has no FM-Master socket or dimmer scene entities."""
    gateway = Gateway(
        id="gateway-id",
        serial_number="606300063406",
        article_number=None,
        gateway_type="GatewayCloudEsp",
        is_online=True,
        online_event_time=None,
        sockets=None,
    )

    class RuntimeData:
        data = Inventory(user=None, gateways=[gateway])

    class Entry:
        runtime_data = RuntimeData()

    switches = []
    lights = []

    await async_setup_switches(hass, Entry(), switches.extend)
    await async_setup_lights(hass, Entry(), lights.extend)

    assert switches == []
    assert lights == []
