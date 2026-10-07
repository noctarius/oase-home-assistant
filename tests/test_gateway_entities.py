"""Tests for gateway-type-specific entities."""

from pyoase import Device, Gateway, Inventory

from custom_components.oase_control.binary_sensor import async_setup_entry as async_setup_binary_sensors
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


async def test_egc_device_is_registered_beneath_its_gateway(hass):
    """An EGC device gets its own HA device even without writable controls."""
    device = Device(
        id="pump-id",
        device_number=12345,
        article_number=75923,
        device_type="GardenPump",
        is_connected=True,
        is_active=True,
        pump_state=None,
        has_rdm=True,
        custom_attributes=None,
        product_name="AquaMax 5000",
    )
    gateway = Gateway(
        id="gateway-id",
        serial_number="606300063406",
        article_number=55317,
        gateway_type="GatewayCloudEsp",
        is_online=True,
        online_event_time=None,
        sockets=None,
        devices=[device],
    )

    class RuntimeData:
        data = Inventory(user=None, gateways=[gateway])

    class Entry:
        runtime_data = RuntimeData()

    entities = []
    await async_setup_binary_sensors(hass, Entry(), entities.extend)

    connected = next(entity for entity in entities if entity.unique_id.endswith("_connected"))
    assert connected.name == "Connected"
    assert connected.is_on is True
    assert connected.device_info["name"] == "AquaMax 5000"
    assert connected.device_info["via_device"] == ("oase_control", gateway.id)
