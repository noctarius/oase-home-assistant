"""Tests for the OASE Control config flow."""

from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.data_entry_flow import FlowResultType

from pyoase import Gateway, Inventory

from custom_components.oase_control.const import DOMAIN


async def test_user_flow_validates_credentials_and_creates_entry(
    hass, enable_custom_integrations
):
    """A verified OASE account can be configured."""
    inventory = Inventory(
        user=None,
        gateways=[
            Gateway(
                id="gateway-id",
                serial_number="500000000000",
                article_number=70788,
                gateway_type="FmMasterWLanEgcCloudEsp",
                is_online=True,
                online_event_time=None,
                sockets=None,
            )
        ],
    )
    with patch(
        "custom_components.oase_control.config_flow.OaseCloudClient.async_get_inventory",
        new=AsyncMock(return_value=inventory),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_USER},
            data={CONF_EMAIL: "user@example.com", CONF_PASSWORD: "secret"},
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "OASE Control (user@example.com)"
    assert result["data"] == {CONF_EMAIL: "user@example.com", CONF_PASSWORD: "secret"}
