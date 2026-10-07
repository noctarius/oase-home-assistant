"""Transport contract shared by cloud and future local OASE implementations."""

from __future__ import annotations

from typing import Protocol

from pyoase import Inventory, onet


class OaseTransport(Protocol):
    """Operations needed by the currently supported Home Assistant entities.

    ``OaseCloudClient`` already satisfies this protocol. A local transport can
    implement the same operations while reusing pyoase's O-Net codec, without
    changing entity or coordinator code.
    """

    async def async_get_inventory(self) -> Inventory:
        """Return the currently known gateways and connected devices."""

    async def async_set_socket(self, gateway_id: str, socket: onet.Socket, on: bool) -> bool:
        """Set an FM-Master switched outlet's state."""

    async def async_set_dimmer_value(self, gateway_id: str, value: int) -> bool:
        """Set an FM-Master dimmer level (0-255)."""

    async def async_set_device_on(self, gateway_id: str, device_number: int, on: bool) -> None:
        """Set an attached EGC device's own power state."""

    async def async_set_pump_power(self, gateway_id: str, device_number: int, raw: int) -> None:
        """Set an attached pump's raw 0-255 power value."""
