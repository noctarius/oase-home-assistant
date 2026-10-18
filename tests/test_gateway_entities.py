"""Tests for gateway-type-specific entities."""

from unittest.mock import AsyncMock

import pytest

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from pyoase import Device, Gateway, Inventory, OaseConnectionError, onet

from custom_components.oase_control.coordinator import OaseDataUpdateCoordinator

from custom_components.oase_control.binary_sensor import (
    OaseGatewayCloudConnection,
    async_setup_entry as async_setup_binary_sensors,
)
from custom_components.oase_control.light import async_setup_entry as async_setup_lights
from custom_components.oase_control.switch import (
    OaseDeviceSwitch,
    OaseSocketSwitch,
    async_setup_entry as async_setup_switches,
)


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

    assert connected.entity_category == "diagnostic"
    # The entity registry rejects plain strings, which would drop the entity.
    er.async_get(hass).async_get_or_create(
        "binary_sensor",
        "oase_control",
        connected.unique_id,
        entity_category=connected.entity_category,
    )


async def test_gateway_cloud_connection_sensor_reports_the_local_read(hass):
    """The cloud setting is a read-only diagnostic, not a cloud availability signal."""
    coordinator = OaseDataUpdateCoordinator(hass, AsyncMock())
    coordinator.data = Inventory(user=None, gateways=[_fm_master()])
    coordinator.local_cloud_connection_enabled["gateway-id"] = True

    entity = OaseGatewayCloudConnection(coordinator, "gateway-id")

    assert entity.name == "Cloud connection enabled"
    assert entity.is_on is True
    assert entity.entity_category == "diagnostic"


def _fm_master(*devices: Device) -> Gateway:
    return Gateway(
        id="gateway-id",
        serial_number="500000000000",
        article_number=70788,
        gateway_type="FmMasterWLanEgcCloudEsp",
        is_online=True,
        online_event_time=None,
        sockets=None,
        devices=list(devices),
    )


async def test_cloud_command_failures_are_reported_to_home_assistant(hass):
    """A failed cloud relay command surfaces as an actionable service error."""
    client = AsyncMock()
    client.async_set_socket.side_effect = OaseConnectionError("offline")
    coordinator = OaseDataUpdateCoordinator(hass, client)
    coordinator.data = Inventory(user=None, gateways=[_fm_master()])
    entity = OaseSocketSwitch(coordinator, "gateway-id", onet.Socket.SOCKET_1, "Socket 1")

    with pytest.raises(HomeAssistantError, match="OASE Cloud command failed"):
        await entity.async_turn_on()


async def test_device_switch_is_unavailable_when_the_device_is_disconnected(hass):
    """An EGC switch cannot be operated while its gateway no longer sees it."""
    device = Device(
        id="pump-id",
        device_number=12345,
        article_number=75923,
        device_type="GardenPump",
        is_connected=False,
        is_active=True,
        pump_state=None,
        has_rdm=True,
        custom_attributes=None,
    )
    coordinator = OaseDataUpdateCoordinator(hass, AsyncMock())
    coordinator.data = Inventory(user=None, gateways=[_fm_master(device)])

    assert OaseDeviceSwitch(coordinator, "gateway-id", device).available is False


async def test_device_switch_is_named_after_its_device(hass):
    """The pump switch does not repeat the device name in its friendly name."""
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
    coordinator = OaseDataUpdateCoordinator(hass, AsyncMock())
    coordinator.data = Inventory(user=None, gateways=[_fm_master(device)])

    switch = OaseDeviceSwitch(coordinator, "gateway-id", device)

    assert switch.name is None
    assert switch.device_info["name"] == "AquaMax 5000"
