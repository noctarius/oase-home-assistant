"""Coordinator for one OASE cloud account."""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from pyoase import Inventory, OaseAuthError, OaseCloudClient, OaseError

from .const import DOMAIN, UPDATE_INTERVAL

_LOGGER = logging.getLogger(__name__)


class OaseDataUpdateCoordinator(DataUpdateCoordinator[Inventory]):
    """Poll the OASE inventory and expose it to all entities."""

    def __init__(self, hass: HomeAssistant, client: OaseCloudClient) -> None:
        super().__init__(
            hass,
            logger=_LOGGER,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
        )
        self.client = client

    async def _async_update_data(self) -> Inventory:
        try:
            return await self.client.async_get_inventory()
        except OaseAuthError as err:
            raise ConfigEntryAuthFailed("OASE authentication failed") from err
        except OaseError as err:
            raise UpdateFailed(f"Error communicating with OASE Cloud: {err}") from err
