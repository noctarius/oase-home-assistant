"""Read-only building blocks for the OASE controller's local transport.

This module is intentionally not wired into the config flow yet. It contains a
candidate, read-only first probe: send an O-Net DEVICE_INFO frame to a *known
controller address* over UDP/5959 and decode a matching reply. The UDP request
itself still needs hardware verification. The next step is a reverse TLS
connection from the controller to the client on TCP/5999; that handshake also
needs a packet capture before it can be implemented safely.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from pyoase import onet

LOCAL_UDP_PORT = 5959
LOCAL_TCP_PORT = 5999


class OaseLocalTransportError(RuntimeError):
    """A local controller did not answer a probe as an O-Net device."""


@dataclass(frozen=True)
class LocalDiscoveryResult:
    """Identity returned by a controller after a direct UDP probe."""

    host: str
    info: onet.DiscoveryInfo


def device_info_probe_packet() -> bytes:
    """Build the candidate read-only O-Net DEVICE_INFO local probe."""
    return onet.encode_packet(onet.PacketType.DEVICE_INFO)


async def async_probe_controller(host: str, *, timeout: float = 5.0) -> LocalDiscoveryResult:
    """Probe one controller's UDP/5959 endpoint for its O-Net identity.

    A target address is deliberately mandatory: this does not broadcast on the
    user's LAN. It is intended for controlled verification against a controller
    whose IP address is already known.
    """
    loop = asyncio.get_running_loop()
    reply: asyncio.Future[bytes] = loop.create_future()
    transport, _protocol = await loop.create_datagram_endpoint(
        lambda: _ProbeProtocol(reply, device_info_probe_packet()),
        remote_addr=(host, LOCAL_UDP_PORT),
    )
    try:
        data = await asyncio.wait_for(reply, timeout)
    except TimeoutError as err:
        raise OaseLocalTransportError(f"no UDP reply from {host}:{LOCAL_UDP_PORT}") from err
    finally:
        transport.close()

    try:
        packet = onet.parse_packet(data)
        expected_type = onet.reply_type(onet.PacketType.DEVICE_INFO)
        if packet.packet_type != expected_type:
            raise ValueError(f"unexpected packet type 0x{packet.packet_type:04x}")
        return LocalDiscoveryResult(host=host, info=onet.parse_discovery_reply(packet.payload))
    except ValueError as err:
        raise OaseLocalTransportError(f"invalid O-Net discovery reply from {host}") from err


class _ProbeProtocol(asyncio.DatagramProtocol):
    """Send one datagram and resolve with the first received reply."""

    def __init__(self, reply: asyncio.Future[bytes], request: bytes) -> None:
        self._reply = reply
        self._request = request

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        assert isinstance(transport, asyncio.DatagramTransport)
        transport.sendto(self._request)

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        if not self._reply.done():
            self._reply.set_result(data)

    def error_received(self, exc: Exception) -> None:
        if not self._reply.done():
            self._reply.set_exception(exc)
