"""OASE Control cloud integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers import entity_registry as er

from pyoase import OaseAuth, OaseCloudClient, OaseError

from .const import CONF_LOCAL_CREDENTIALS, DOMAIN, PLATFORMS
from .coordinator import OaseDataUpdateCoordinator
from .local import local_credentials_from_inventory
from .services import async_setup_services

_LOGGER = logging.getLogger(__name__)

type OaseConfigEntry = ConfigEntry[OaseDataUpdateCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: OaseConfigEntry) -> bool:
    """Set up OASE Control from a config entry."""
    async_setup_services(hass)
    session = async_get_clientsession(hass)
    auth = OaseAuth(session, entry.data[CONF_EMAIL], entry.data[CONF_PASSWORD])
    client = OaseCloudClient(session, auth)
    coordinator = OaseDataUpdateCoordinator(hass, client)

    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await _async_cache_local_credentials(hass, entry, client)
    _async_remove_invalid_fm_master_entities(hass, entry, coordinator)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_cache_local_credentials(
    hass: HomeAssistant, entry: OaseConfigEntry, client: OaseCloudClient
) -> None:
    """Cache cloud-provided local credentials without logging their values."""
    try:
        fetched = local_credentials_from_inventory(await client.async_get_inventory_raw())
    except OaseError:
        _LOGGER.debug("Unable to refresh local controller credentials from the cloud")
        return
    if not fetched:
        return

    existing = entry.data.get(CONF_LOCAL_CREDENTIALS, {})
    cached = dict(existing) if isinstance(existing, dict) else {}
    updated = {**cached, **fetched}
    if updated != cached:
        hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_LOCAL_CREDENTIALS: updated})


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


async def _async_update_listener(hass: HomeAssistant, entry: OaseConfigEntry) -> None:
    """Reload after credentials change during reauthentication."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: OaseConfigEntry) -> bool:
    """Unload an OASE Control config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
