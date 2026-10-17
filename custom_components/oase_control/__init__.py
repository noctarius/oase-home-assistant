"""OASE Control cloud integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.typing import ConfigType

from pyoase import OaseAuth, OaseCloudClient

from .const import CONF_LOCAL_CREDENTIALS, DOMAIN, PLATFORMS
from .coordinator import OaseDataUpdateCoordinator
from .services import async_setup_services

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type OaseConfigEntry = ConfigEntry[OaseDataUpdateCoordinator]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the integration's services once."""
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: OaseConfigEntry) -> bool:
    """Set up OASE Control from a config entry."""
    session = async_get_clientsession(hass)
    auth = OaseAuth(session, entry.data[CONF_EMAIL], entry.data[CONF_PASSWORD])
    client = OaseCloudClient(session, auth)
    cached_credentials = entry.data.get(CONF_LOCAL_CREDENTIALS, {})
    coordinator = OaseDataUpdateCoordinator(
        hass,
        client,
        dict(cached_credentials) if isinstance(cached_credentials, dict) else None,
        config_entry=entry,
    )
    # Registered before the first refresh, which may already open local
    # sessions: Home Assistant also runs unload callbacks when setup fails.
    entry.async_on_unload(coordinator.async_close)

    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    coordinator.async_start_local_updates()
    _async_remove_invalid_fm_master_entities(hass, entry, coordinator)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


def _async_remove_invalid_fm_master_entities(
    hass: HomeAssistant, entry: OaseConfigEntry, coordinator: OaseDataUpdateCoordinator
) -> None:
    """Remove old socket and dimmer entities that never belonged to a gateway."""
    invalid_unique_ids = {
        f"{gateway.id}_{suffix}"
        for gateway in coordinator.data.gateways
        if not gateway.is_fm_master
        for suffix in ("dimmer", "socket_1", "socket_2", "socket_3")
    }
    if not invalid_unique_ids:
        return
    registry = er.async_get(hass)
    for registered in er.async_entries_for_config_entry(registry, entry.entry_id):
        if registered.unique_id in invalid_unique_ids:
            registry.async_remove(registered.entity_id)


async def async_unload_entry(hass: HomeAssistant, entry: OaseConfigEntry) -> bool:
    """Unload an OASE Control config entry; local sessions close via on_unload."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
