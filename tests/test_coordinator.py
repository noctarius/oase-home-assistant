"""Tests for OASE cloud inventory refreshes."""

from unittest.mock import AsyncMock

import pytest

from homeassistant.exceptions import ConfigEntryAuthFailed

from pyoase import Inventory, OaseAuthError

from custom_components.oase_control.coordinator import (
    OaseDataUpdateCoordinator,
    _inventory_with_rdm_capabilities,
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
