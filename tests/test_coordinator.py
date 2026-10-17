"""Tests for OASE cloud inventory refreshes."""

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest

from homeassistant.core import callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from pyoase import Device, Gateway, Inventory, OaseAuthError, OaseConnectionError, rdm
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.oase_control.coordinator import (
    OaseDataUpdateCoordinator,
    _inventory_with_rdm_capabilities,
    _gateway_with_local_state,
)
from custom_components.oase_control.local import LocalEgcDeviceState, OaseLocalTransportError


def _local_gateway(gateway_id: str = "gateway-id", *, is_online: bool = False) -> Gateway:
    return Gateway(
        id=gateway_id,
        serial_number="606300063406",
        article_number=55317,
        gateway_type="GatewayCloudEsp",
        is_online=is_online,
        online_event_time=None,
        sockets=None,
    )


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


async def test_device_write_prefers_the_local_session(hass):
    """A reachable controller receives EGC writes without waiting for the cloud."""
    gateway = Gateway(
        id="gateway-id",
        serial_number="606300063406",
        article_number=55317,
        gateway_type="GatewayCloudEsp",
        is_online=True,
        online_event_time=None,
        sockets=None,
    )
    client = AsyncMock()
    coordinator = OaseDataUpdateCoordinator(
        hass, client, local_credentials={"gateway-id": "credential"}
    )
    coordinator.data = Inventory(user=None, gateways=[gateway])
    coordinator.local_sessions.async_set_egc_parameter = AsyncMock()
    coordinator.async_refresh_local_state = AsyncMock()

    await coordinator.async_set_device_on("gateway-id", 2148503843, True)

    coordinator.local_sessions.async_set_egc_parameter.assert_awaited_once_with(
        "gateway-id", "606300063406", "credential", 2148503843, rdm.Pid.DEVICE_ON, b"\xff"
    )
    client.async_set_device_on.assert_not_awaited()
    coordinator.async_refresh_local_state.assert_awaited_once()


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


async def test_local_status_refreshes_do_not_postpone_cloud_polling(hass):
    """The 30 s cloud poll keeps running while 5 s local refreshes publish state."""
    client = AsyncMock()
    del client.async_get_inventory_raw
    client.async_get_inventory.return_value = Inventory(user=None, gateways=[_local_gateway()])
    coordinator = OaseDataUpdateCoordinator(
        hass, client, local_credentials={"gateway-id": "credential"}
    )
    coordinator.local_sessions.async_read_egc_devices = AsyncMock(return_value=())
    await coordinator.async_refresh()
    unsubscribe = coordinator.async_add_listener(callback(lambda: None))
    coordinator.async_start_local_updates()
    await hass.async_block_till_done()
    polls_before = client.async_get_inventory.await_count

    now = dt_util.utcnow()
    for step in range(1, 25):
        async_fire_time_changed(hass, now + timedelta(seconds=5 * step))
        await hass.async_block_till_done()

    unsubscribe()
    await coordinator.async_close()
    assert client.async_get_inventory.await_count - polls_before >= 3


async def test_local_status_refresh_keeps_inventory_updated_during_the_read(hass):
    """A cloud refresh finishing mid-read is not reverted by the local overlay."""
    coordinator = OaseDataUpdateCoordinator(
        hass, AsyncMock(), local_credentials={"gateway-id": "credential"}
    )
    coordinator.data = Inventory(user=None, gateways=[_local_gateway()])
    newer = Inventory(user=None, gateways=[_local_gateway(), _local_gateway("new-gateway")])

    async def _read(*_args):
        coordinator.data = newer
        return ()

    coordinator.local_sessions.async_read_egc_devices = _read

    await coordinator.async_refresh_local_state()

    assert [gateway.id for gateway in coordinator.data.gateways] == ["gateway-id", "new-gateway"]
    assert coordinator.data.gateways[0].is_online is True


async def test_local_write_refresh_runs_after_an_in_flight_read(hass):
    """A refresh requested by a write is not dropped when a poll is running."""
    coordinator = OaseDataUpdateCoordinator(
        hass, AsyncMock(), local_credentials={"gateway-id": "credential"}
    )
    coordinator.data = Inventory(user=None, gateways=[_local_gateway()])
    release = asyncio.Event()
    reads = 0

    async def _read(*_args):
        nonlocal reads
        reads += 1
        if reads == 1:
            await release.wait()
        return ()

    coordinator.local_sessions.async_read_egc_devices = _read
    poll = hass.async_create_task(coordinator.async_refresh_local_state())
    await asyncio.sleep(0)
    await coordinator.async_refresh_local_state(follow_up=True)
    release.set()
    await poll

    assert reads == 2


def _cloud_down_coordinator(hass, error, **kwargs) -> OaseDataUpdateCoordinator:
    client = AsyncMock()
    client.async_get_inventory_raw.side_effect = error
    coordinator = OaseDataUpdateCoordinator(
        hass, client, local_credentials={"gateway-id": "credential", "other": "credential"}, **kwargs
    )
    coordinator.data = Inventory(
        user=None,
        gateways=[_local_gateway(is_online=True), _local_gateway("other", is_online=True)],
    )
    return coordinator


async def test_cloud_and_local_outage_is_reported(hass):
    """The last inventory is not presented as current when nothing answers."""
    coordinator = _cloud_down_coordinator(hass, OaseConnectionError("offline"))
    coordinator.local_sessions.async_read_egc_devices = AsyncMock(
        side_effect=OaseLocalTransportError("unreachable")
    )

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


async def test_cloud_outage_marks_locally_unreachable_gateways_offline(hass):
    """Only gateways that answered locally keep reporting live state."""
    coordinator = _cloud_down_coordinator(hass, OaseConnectionError("offline"))

    async def _read(gateway_id, *_args):
        if gateway_id == "other":
            raise OaseLocalTransportError("unreachable")
        return ()

    coordinator.local_sessions.async_read_egc_devices = _read

    inventory = await coordinator._async_update_data()

    assert [gateway.is_online for gateway in inventory.gateways] == [True, False]


async def test_expired_cloud_credentials_still_start_reauth_during_local_fallback(hass):
    """Local control does not hide that the cloud account needs attention."""
    entry = MockConfigEntry(domain="oase_control", data={})
    entry.add_to_hass(hass)
    coordinator = _cloud_down_coordinator(hass, OaseAuthError("expired"), config_entry=entry)
    coordinator.local_sessions.async_read_egc_devices = AsyncMock(return_value=())

    with patch.object(entry, "async_start_reauth") as start_reauth:
        inventory = await coordinator._async_update_data()

    assert inventory.gateways[0].is_online is True
    start_reauth.assert_called_once_with(hass)
