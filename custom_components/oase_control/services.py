"""Explicit, read-only diagnostics for the experimental local transport."""

from __future__ import annotations

import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv

from pyoase import onet, rdm

from .const import CONF_LOCAL_CREDENTIALS, DOMAIN
from .local import (
    OaseLocalTransportError,
    async_discover_controllers,
    async_open_tls_session,
    async_read_egc_devices,
)

SERVICE_LOCAL_READ = "local_read"
_LOCAL_ERRORS = (ConnectionError, OSError, TimeoutError, ValueError)
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
            discovered = await async_discover_controllers(serial_number=serial_number)
            matches = [result for result in discovered if result.info.serial_number == serial_number]
            if not matches:
                raise HomeAssistantError("local controller was not found by O-Net discovery")
            host = matches[0].host
            session = await async_open_tls_session(host)
            try:
                await session.async_authenticate(credential)
                egc_devices = await async_read_egc_devices(session)
                # Only FM-Master controllers have a socket scene; read it last
                # so its absence cannot hide the EGC result.
                scene = await _async_read_live_scene(session)
            finally:
                await session.async_close()
        except (OaseLocalTransportError, *_LOCAL_ERRORS) as err:
            raise HomeAssistantError(f"OASE local read failed: {err}") from err

        return {
            "gateway_id": gateway_id,
            "host": host,
            "scene": scene,
            "egc_devices": [_egc_device_response(device) for device in egc_devices],
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


async def _async_read_live_scene(session) -> dict[str, int | str] | None:
    """Read the controller's live socket scene, or None if it has none."""
    try:
        reply = await session.async_request(onet.PacketType.GET_LIVE_SCENE)
        if reply.packet_type != onet.reply_type(onet.PacketType.GET_LIVE_SCENE):
            return None
        scene = onet.parse_live_scene_reply(reply.payload)
    except (OaseLocalTransportError, *_LOCAL_ERRORS):
        return None
    return {
        "packet_type": f"0x{reply.packet_type:04x}",
        "transaction_number": reply.transaction_number,
        "type": scene.type,
        "id": scene.id,
        "count": scene.count,
        "scene_type": scene.scene_type,
        "data_hex": scene.data.hex(),
    }


def _egc_device_response(device) -> dict[str, int | str]:
    """Format one local EGC read for Home Assistant's service response."""
    item: dict[str, int | str] = {
        "article_number": device.article_number,
        "device_number": device.device_number,
        "manufacturer_id": device.manufacturer_id,
        "subdevice_count": device.subdevice_count,
    }
    if device.device_on_raw:
        item["device_on_raw"] = device.device_on_raw.hex()
    if device.pump_power_raw:
        item["pump_power_raw"] = device.pump_power_raw.hex()
        item["pump_power_percent"] = rdm.raw_to_percent(device.pump_power_raw[0])
    return item
