"""Tests for OASE cloud inventory refreshes."""

from unittest.mock import AsyncMock

import pytest

from homeassistant.exceptions import ConfigEntryAuthFailed

from pyoase import Inventory, OaseAuthError

from custom_components.oase_control.coordinator import OaseDataUpdateCoordinator


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
