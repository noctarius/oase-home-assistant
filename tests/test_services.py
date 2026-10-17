"""Tests for the explicit local read diagnostic service."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from pyoase import onet

from homeassistant.exceptions import HomeAssistantError

from custom_components.oase_control import services
from custom_components.oase_control.const import CONF_LOCAL_CREDENTIALS, DOMAIN
from custom_components.oase_control.services import _cached_controller, async_setup_services


def _entry(credentials: dict[str, str]):
    gateways = {
        gateway_id: SimpleNamespace(serial_number=f"serial-{gateway_id}")
        for gateway_id in credentials
    }
    inventory = SimpleNamespace(gateway=gateways.get)
    return SimpleNamespace(
        data={CONF_LOCAL_CREDENTIALS: credentials},
        runtime_data=SimpleNamespace(data=inventory),
    )


def test_local_read_uses_the_only_cached_controller_credential(hass):
    """A single configured controller does not require its gateway id in the call."""
    entry = _entry({"gateway-id": "credential"})
    hass.config_entries.async_loaded_entries = lambda domain: [entry]

    assert _cached_controller(hass, None) == ("gateway-id", "credential", "serial-gateway-id")


def test_local_read_requires_a_gateway_id_for_multiple_cached_controllers(hass):
    """The diagnostic must never select a controller arbitrarily."""
    entry = _entry({"first": "first-credential", "second": "second-credential"})
    hass.config_entries.async_loaded_entries = lambda domain: [entry]

    with pytest.raises(HomeAssistantError, match="gateway_id is required"):
        _cached_controller(hass, None)


async def _call_local_read(hass, session):
    found = SimpleNamespace(host="192.0.2.1", info=SimpleNamespace(serial_number="serial"))
    async_setup_services(hass)
    with (
        patch.object(services, "_cached_controller", return_value=("gateway-id", "credential", "serial")),
        patch.object(services, "async_discover_controllers", AsyncMock(return_value=[found])),
        patch.object(services, "async_open_tls_session", AsyncMock(return_value=session)),
        patch.object(services, "async_read_egc_devices", AsyncMock(return_value=())),
    ):
        return await hass.services.async_call(
            DOMAIN, "local_read", {}, blocking=True, return_response=True
        )


async def test_local_read_reports_egc_devices_without_a_socket_scene(hass):
    """Controllers without an FM-Master scene still return their EGC bus."""
    session = AsyncMock()
    session.async_request.return_value = onet.OnetPacket(onet.PacketType.ALIVE, b"")

    response = await _call_local_read(hass, session)

    assert response == {"gateway_id": "gateway-id", "host": "192.0.2.1", "scene": None, "egc_devices": []}
    session.async_close.assert_awaited_once()


async def test_local_read_timeouts_are_service_errors(hass):
    """A controller that stops answering produces an actionable error."""
    session = AsyncMock()
    session.async_authenticate.side_effect = TimeoutError

    with pytest.raises(HomeAssistantError, match="OASE local read failed"):
        await _call_local_read(hass, session)
    session.async_close.assert_awaited_once()
