"""OASE Control cloud integration."""

from __future__ import annotations

import json
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from pyoase import OaseAuth, OaseCloudClient

from .const import DOMAIN, PLATFORMS
from .coordinator import OaseDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

type OaseConfigEntry = ConfigEntry[OaseDataUpdateCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: OaseConfigEntry) -> bool:
    """Set up OASE Control from a config entry."""
    session = async_get_clientsession(hass)
    auth = OaseAuth(session, entry.data[CONF_EMAIL], entry.data[CONF_PASSWORD])
    client = OaseCloudClient(session, auth)

    # Temporary development diagnostic: required to identify the credential
    # supplied by the cloud for local-controller authentication. Remove after
    # collecting the response from this installation.
    raw_inventory = await client.async_get_inventory_raw()
    _LOGGER.warning("Temporary full OASE cloud inventory diagnostic: %s", json.dumps(raw_inventory))
    coordinator = OaseDataUpdateCoordinator(hass, client)

    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_update_listener(hass: HomeAssistant, entry: OaseConfigEntry) -> None:
    """Reload after credentials change during reauthentication."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: OaseConfigEntry) -> bool:
    """Unload an OASE Control config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
