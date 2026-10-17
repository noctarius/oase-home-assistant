"""Tests for persistent local-controller credential caching."""

from unittest.mock import AsyncMock

from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.oase_control.const import CONF_LOCAL_CREDENTIALS, DOMAIN
from custom_components.oase_control.coordinator import OaseDataUpdateCoordinator


def _raw_inventory(*gateways: dict) -> dict:
    return {"gateways": [{"gatewayType": "GatewayCloudEsp", **gateway} for gateway in gateways]}


def _credential(value: str) -> str:
    return f'[{{"Id": 101, "Value": {{"Value": "{value}"}}}}]'


async def _refresh(hass, entry: MockConfigEntry, raw_inventory: dict) -> OaseDataUpdateCoordinator:
    client = AsyncMock()
    client.async_get_inventory_raw.return_value = raw_inventory
    cached = entry.data.get(CONF_LOCAL_CREDENTIALS, {})
    coordinator = OaseDataUpdateCoordinator(hass, client, dict(cached), config_entry=entry)
    await coordinator._async_update_data()
    return coordinator


async def test_cloud_refresh_tracks_credentials_of_current_gateways(hass):
    """New credentials are cached and removed gateways' credentials dropped."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_EMAIL: "user@example.com",
            CONF_PASSWORD: "secret",
            CONF_LOCAL_CREDENTIALS: {"kept": "kept-value", "removed": "old-value"},
        },
    )
    entry.add_to_hass(hass)

    coordinator = await _refresh(
        hass,
        entry,
        _raw_inventory(
            {"id": "kept", "customAttributesJson": "[]"},
            {"id": "current", "customAttributesJson": _credential("new-value")},
        ),
    )

    expected = {"kept": "kept-value", "current": "new-value"}
    assert coordinator.local_credentials == expected
    assert entry.data[CONF_LOCAL_CREDENTIALS] == expected
    assert entry.data[CONF_PASSWORD] == "secret"


async def test_unchanged_credentials_do_not_rewrite_the_config_entry(hass):
    """An incomplete cloud response preserves an existing local fallback."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_EMAIL: "user@example.com", CONF_LOCAL_CREDENTIALS: {"current": "value"}},
    )
    entry.add_to_hass(hass)
    updates = []
    hass.config_entries.async_update_entry = lambda *args, **kwargs: updates.append(args)

    await _refresh(hass, entry, _raw_inventory({"id": "current"}))

    assert updates == []
