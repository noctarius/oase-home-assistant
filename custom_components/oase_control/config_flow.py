"""Config flow for OASE Control."""

from __future__ import annotations

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from pyoase import OaseAuth, OaseAuthError, OaseCloudClient, OaseConnectionError, OaseError

from .const import DOMAIN


class OaseControlConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle setup of OASE Control using the account's cloud credentials."""

    VERSION = 1

    async def async_step_reauth(self, entry_data: dict[str, str]) -> FlowResult:
        """Ask for updated credentials after an OAuth refresh fails."""
        self._reauth_entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, str] | None = None) -> FlowResult:
        """Validate and save the replacement credentials."""
        errors: dict[str, str] = {}
        if user_input is not None:
            session = async_get_clientsession(self.hass)
            client = OaseCloudClient(
                session,
                OaseAuth(session, user_input[CONF_EMAIL], user_input[CONF_PASSWORD]),
            )
            try:
                await client.async_get_inventory()
            except OaseAuthError:
                errors["base"] = "invalid_auth"
            except OaseConnectionError:
                errors["base"] = "cannot_connect"
            except OaseError:
                errors["base"] = "unknown"
            else:
                assert self._reauth_entry is not None
                self.hass.config_entries.async_update_entry(
                    self._reauth_entry, data={**self._reauth_entry.data, **user_input}
                )
                await self.async_set_unique_id(user_input[CONF_EMAIL].lower())
                return self.async_abort(reason="reauth_successful")

        current_email = self._reauth_entry.data[CONF_EMAIL] if self._reauth_entry else ""
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_EMAIL, default=current_email): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
        )

    async def async_step_user(self, user_input: dict[str, str] | None = None) -> FlowResult:
        """Authenticate and verify the supplied OASE account."""
        errors: dict[str, str] = {}
        if user_input is not None:
            session = async_get_clientsession(self.hass)
            client = OaseCloudClient(
                session, OaseAuth(session, user_input[CONF_EMAIL], user_input[CONF_PASSWORD])
            )
            try:
                inventory = await client.async_get_inventory()
            except OaseAuthError:
                errors["base"] = "invalid_auth"
            except OaseConnectionError:
                errors["base"] = "cannot_connect"
            except OaseError:
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(user_input[CONF_EMAIL].lower())
                self._abort_if_unique_id_configured()
                title = (
                    inventory.user.given_name
                    if inventory.user and inventory.user.given_name
                    else user_input[CONF_EMAIL]
                )
                return self.async_create_entry(title=f"OASE Control ({title})", data=user_input)

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {vol.Required(CONF_EMAIL): str, vol.Required(CONF_PASSWORD): str}
            ),
            errors=errors,
        )
