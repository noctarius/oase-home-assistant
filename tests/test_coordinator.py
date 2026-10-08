"""Tests for OASE cloud inventory refreshes."""

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
                device_on_raw=b"\x02",
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
