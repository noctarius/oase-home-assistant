"""Tests for the explicit local read diagnostic service."""

from types import SimpleNamespace

import pytest

from homeassistant.exceptions import HomeAssistantError

from custom_components.oase_control.const import CONF_LOCAL_CREDENTIALS
from custom_components.oase_control.services import _cached_controller


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
