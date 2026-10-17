"""Tests for the OASE Control config flow."""

from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

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


def _entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id="user@example.com",
        data={CONF_EMAIL: "user@example.com", CONF_PASSWORD: "old"},
    )


async def test_reauth_updates_credentials_and_reloads_the_entry(hass, enable_custom_integrations):
    """Reauth also recovers an entry whose setup failed with old credentials."""
    entry = _entry()
    entry.add_to_hass(hass)
    with (
        patch(
            "custom_components.oase_control.config_flow.OaseCloudClient.async_get_inventory",
            new=AsyncMock(return_value=Inventory(user=None, gateways=[])),
        ),
        patch.object(hass.config_entries, "async_schedule_reload") as schedule_reload,
    ):
        flow = await entry.start_reauth_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            flow["flow_id"], {CONF_EMAIL: "User@example.com", CONF_PASSWORD: "new"}
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_PASSWORD] == "new"
    schedule_reload.assert_called_once_with(entry.entry_id)


async def test_reauth_rejects_a_different_account(hass, enable_custom_integrations):
    """Reauth cannot silently switch the entry to another OASE account."""
    entry = _entry()
    entry.add_to_hass(hass)
    with patch(
        "custom_components.oase_control.config_flow.OaseCloudClient.async_get_inventory",
        new=AsyncMock(return_value=Inventory(user=None, gateways=[])),
    ):
        flow = await entry.start_reauth_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            flow["flow_id"], {CONF_EMAIL: "other@example.com", CONF_PASSWORD: "new"}
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_account"
    assert entry.data[CONF_PASSWORD] == "old"


async def test_failed_setup_still_closes_local_sessions(hass, enable_custom_integrations):
    """Local sessions opened by the first refresh are released if setup fails."""
    entry = _entry()
    entry.add_to_hass(hass)
    with (
        patch(
            "custom_components.oase_control.OaseCloudClient.async_get_inventory_raw",
            new=AsyncMock(return_value={"gateways": []}),
        ),
        patch.object(
            hass.config_entries, "async_forward_entry_setups", side_effect=RuntimeError("boom")
        ),
        patch(
            "custom_components.oase_control.coordinator.OaseDataUpdateCoordinator.async_close",
            new=AsyncMock(),
        ) as close,
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR
    close.assert_awaited_once()
