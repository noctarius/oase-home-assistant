"""Tests for persistent local-controller credential caching."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from custom_components.oase_control import _async_cache_local_credentials
from custom_components.oase_control.const import CONF_LOCAL_CREDENTIALS


async def test_local_credentials_are_cached_without_discarding_existing_gateways():
    """A cloud refresh merges new values into the config entry's local cache."""
    entry = SimpleNamespace(data={CONF_LOCAL_CREDENTIALS: {"older": "old-value"}})
    config_entries = SimpleNamespace(async_update_entry=Mock())
    hass = SimpleNamespace(config_entries=config_entries)
    client = AsyncMock()
    client.async_get_inventory_raw.return_value = {
        "gateways": [
            {
                "id": "current",
                "customAttributesJson": '[{"Id": 101, "Value": {"Value": "new-value"}}]',
            }
        ]
    }

    await _async_cache_local_credentials(hass, entry, client)

    config_entries.async_update_entry.assert_called_once_with(
        entry,
        data={CONF_LOCAL_CREDENTIALS: {"older": "old-value", "current": "new-value"}},
    )


async def test_no_config_entry_update_occurs_when_the_cloud_has_no_credential():
    """An incomplete cloud response preserves an existing local fallback."""
    entry = SimpleNamespace(data={CONF_LOCAL_CREDENTIALS: {"older": "old-value"}})
    config_entries = SimpleNamespace(async_update_entry=Mock())
    hass = SimpleNamespace(config_entries=config_entries)
    client = AsyncMock()
    client.async_get_inventory_raw.return_value = {"gateways": [{"id": "current"}]}

    await _async_cache_local_credentials(hass, entry, client)

    config_entries.async_update_entry.assert_not_called()
