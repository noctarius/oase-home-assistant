"""Tests for OASE cloud inventory refreshes."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from homeassistant.exceptions import ConfigEntryAuthFailed

from pyoase import Device, Gateway, Inventory, OaseAuthError

from custom_components.oase_control.coordinator import (
    OaseDataUpdateCoordinator,
    _inventory_with_rdm_capabilities,
    _gateway_with_local_state,
)
from custom_components.oase_control.local import LocalEgcDeviceState


async def test_coordinator_returns_inventory(hass):
    """The coordinator exposes the inventory returned by pyoase."""
    inventory = Inventory(user=None, gateways=[])
    client = AsyncMock()
    client.async_get_inventory.return_value = inventory

    coordinator = OaseDataUpdateCoordinator(hass, client)

    assert await coordinator._async_update_data() is inventory


async def test_coordinator_raises_reauth_on_invalid_credentials(hass):
    """Authentication failures initiate Home Assistant reauthentication."""
    client = AsyncMock()
    client.async_get_inventory.side_effect = OaseAuthError("expired")
    coordinator = OaseDataUpdateCoordinator(hass, client)

    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()


async def test_local_status_refresh_never_calls_the_cloud_client(hass):
    """Frequent local polling remains independent from slow cloud inventory reads."""
    inventory = Inventory(user=None, gateways=[])
    client = AsyncMock()
    coordinator = OaseDataUpdateCoordinator(hass, client)
    coordinator.data = inventory

    await coordinator.async_refresh_local_state()

    client.async_get_inventory.assert_not_awaited()
    assert coordinator.data is inventory


async def test_local_reads_are_serialized_across_inventory_and_status_updates(hass):
    """Two refresh paths cannot interleave packets on the controller TLS stream."""
    gateway = Gateway(
        id="gateway-id",
        serial_number="606300063406",
        article_number=55317,
        gateway_type="GatewayCloudEsp",
        is_online=True,
        online_event_time=None,
        sockets=None,
    )
    coordinator = OaseDataUpdateCoordinator(
        hass, AsyncMock(), local_credentials={"gateway-id": "credential"}
    )
    first_read_started = asyncio.Event()
    release_first_read = asyncio.Event()
    active_reads = 0
    max_active_reads = 0

    async def _read(*_args):
        nonlocal active_reads, max_active_reads
        active_reads += 1
        max_active_reads = max(max_active_reads, active_reads)
        first_read_started.set()
        await release_first_read.wait()
        active_reads -= 1
        return ()

    coordinator.local_sessions.async_read_egc_devices = _read
    inventory = Inventory(user=None, gateways=[gateway])
    first = hass.async_create_task(coordinator._async_apply_local_reads(inventory))
    await first_read_started.wait()
    second = hass.async_create_task(coordinator._async_apply_local_reads(inventory))
    await asyncio.sleep(0)
    assert max_active_reads == 1

    release_first_read.set()
    await asyncio.gather(first, second)


def test_cached_rdm_data_enables_only_confirmed_pump_controls():
    """The cloud's successful RDM reads establish EGC control capabilities."""
    raw_inventory = {
        "gateways": [
            {
                "id": "gateway-id",
                "devices": [
                    {
                        "deviceNumber": 12345,
                        "rdmData": [
                            {"key": {"parameterId": 4112}, "value": {"value": "/w=="}},
                            {"key": {"parameterId": 32825}, "value": {"value": "gA=="}},
                        ],
                    }
                ],
            }
        ]
    }
    inventory = Inventory.from_dict(
        {
            "gateways": [
                {
                    "id": "gateway-id",
                    "gatewayType": "GatewayCloudEsp",
                    "devices": [{"deviceNumber": 12345, "deviceType": "GardenPump"}],
                }
            ]
        }
    )

    device = _inventory_with_rdm_capabilities(inventory, raw_inventory).gateways[0].devices[0]

    assert device.can_switch is True
    assert device.can_set_power is True
    assert device.pump_state is not None
    assert device.pump_state.device_on is True
    assert device.pump_state.dimmer_value == 128


def test_local_egc_read_makes_the_matching_gateway_and_pump_available():
    """Local data supersedes a cloud-disconnected gateway's stale pump state."""
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
    gateway = Gateway(
        id="gateway-id",
        serial_number="606300063406",
        article_number=55317,
        gateway_type="GatewayCloudEsp",
        is_online=False,
        online_event_time=None,
        sockets=None,
        devices=[device],
    )

    updated = _gateway_with_local_state(
        gateway,
        (
            LocalEgcDeviceState(
                article_number=75923,
                device_number=12345,
                manufacturer_id=20289,
                subdevice_count=0,
                device_on_raw=b"\xff",
                pump_power_raw=b"\x01",
            ),
        ),
    )

    assert updated.is_online is True
    assert updated.devices[0].is_connected is True
    assert updated.devices[0].can_switch is True
    assert updated.devices[0].can_set_power is True
    assert updated.devices[0].pump_state.device_on is True
    assert updated.devices[0].pump_state.dimmer_value == 1


def test_local_egc_device_state_two_means_off():
    """The AquaMax reports its inactive EGC state as enum value 2."""
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
    )
    gateway = Gateway(
        id="gateway-id",
        serial_number="606300063406",
        article_number=55317,
        gateway_type="GatewayCloudEsp",
        is_online=False,
        online_event_time=None,
        sockets=None,
        devices=[device],
    )

    updated = _gateway_with_local_state(
        gateway,
        (
            LocalEgcDeviceState(75923, 12345, 20289, 0, b"\x02", b"\x01"),
        ),
    )

    assert updated.devices[0].pump_state.device_on is False
