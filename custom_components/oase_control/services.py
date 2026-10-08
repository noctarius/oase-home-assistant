"""Explicit, read-only diagnostics for the experimental local transport."""

from __future__ import annotations

import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv

from pyoase import onet

from .const import CONF_LOCAL_CREDENTIALS, DOMAIN
from .local import OaseLocalTransportError, async_discover_controllers, async_open_tls_session

SERVICE_LOCAL_READ = "local_read"
ATTR_GATEWAY_ID = "gateway_id"

_SERVICE_LOCAL_READ_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_GATEWAY_ID): cv.string,
    }
)


def async_setup_services(hass: HomeAssistant) -> None:
    """Register the local diagnostic service once for all config entries."""
    if hass.services.has_service(DOMAIN, SERVICE_LOCAL_READ):
        return

    async def _async_local_read(call: ServiceCall) -> ServiceResponse:
        gateway_id, credential, serial_number = _cached_controller(
            hass, call.data.get(ATTR_GATEWAY_ID)
        )
        try:
            discovered = await async_discover_controllers()
            matches = [result for result in discovered if result.info.serial_number == serial_number]
            if not matches:
                raise HomeAssistantError("local controller was not found by O-Net discovery")
            if len(matches) > 1:
                raise HomeAssistantError("O-Net discovery found multiple controllers with this serial number")
            session = await async_open_tls_session(matches[0].host)
            try:
                await session.async_authenticate(credential)
                reply = await session.async_request(onet.PacketType.GET_LIVE_SCENE)
            finally:
                await session.async_close()
        except OaseLocalTransportError as err:
            raise HomeAssistantError(f"OASE local read failed: {err}") from err

        scene = onet.parse_live_scene_reply(reply.payload)
        return {
            "gateway_id": gateway_id,
            "host": matches[0].host,
            "packet_type": f"0x{reply.packet_type:04x}",
            "transaction_number": reply.transaction_number,
            "scene": {
                "type": scene.type,
                "id": scene.id,
                "count": scene.count,
                "scene_type": scene.scene_type,
                "data_hex": scene.data.hex(),
            },
        }

    hass.services.async_register(
        DOMAIN,
        SERVICE_LOCAL_READ,
        _async_local_read,
        schema=_SERVICE_LOCAL_READ_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )


def _cached_controller(hass: HomeAssistant, gateway_id: str | None) -> tuple[str, str, str]:
    """Find one credential and its verified gateway serial without exposing either."""
    controllers: dict[str, tuple[str, str]] = {}
    for entry in hass.config_entries.async_loaded_entries(DOMAIN):
        cached = entry.data.get(CONF_LOCAL_CREDENTIALS, {})
        coordinator = getattr(entry, "runtime_data", None)
        inventory = getattr(coordinator, "data", None)
        if not isinstance(cached, dict) or inventory is None:
            continue
        for candidate_id, credential in cached.items():
            gateway = inventory.gateway(candidate_id) if isinstance(candidate_id, str) else None
            if (
                isinstance(credential, str)
                and credential
                and gateway is not None
                and gateway.serial_number
            ):
                controllers[candidate_id] = (credential, gateway.serial_number)
    if gateway_id:
        controller = controllers.get(gateway_id)
        if controller:
            return gateway_id, *controller
        raise HomeAssistantError("No cached local credential and serial exist for this gateway")
    if len(controllers) == 1:
        candidate_id, controller = next(iter(controllers.items()))
        return candidate_id, *controller
    if not controllers:
        raise HomeAssistantError("No cached local controller credential is available")
    raise HomeAssistantError("gateway_id is required when multiple local controllers are configured")
