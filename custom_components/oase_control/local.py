"""Experimental local transport building blocks for OASE controllers.

The local setup is controller-initiated: after a UDP request on port 5959, the
controller opens a TLS connection back to a listener on port 5999. This module
is deliberately not wired into the config flow while local O-Net commands are
being validated against hardware.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import ipaddress
import json
from pathlib import Path
import socket
import ssl
import struct
import tempfile
import time
from typing import Any, Final, Mapping

from pyoase import onet, rdm

LOCAL_UDP_PORT = 5959
LOCAL_TCP_PORT = 5999
LOCAL_DISCOVERY_BROADCAST = "255.255.255.255"
LOCAL_DISCOVERY_MULTICAST = "224.0.0.251"
_CERTIFICATE_COMMON_NAME: Final = "com.oase.easycontrol"
_CERTIFICATE_VALIDITY: Final = timedelta(days=14)
_LOCAL_CREDENTIAL_ATTRIBUTE_ID: Final = 101


class OaseLocalTransportError(RuntimeError):
    """A local controller did not answer a probe as an O-Net device."""


class OaseLocalAuthenticationError(OaseLocalTransportError):
    """The controller rejected or malformed a local password check reply."""


class OaseLocalCredentialError(OaseLocalTransportError):
    """The cloud inventory did not contain a usable local credential."""


@dataclass(frozen=True)
class LocalDiscoveryResult:
    """Identity returned by a controller after a direct UDP probe."""

    host: str
    info: onet.DiscoveryInfo


@dataclass(frozen=True)
class LocalEgcDeviceState:
    """Read-only state returned for one device on the local EGC bus."""

    article_number: int
    device_number: int
    manufacturer_id: int
    subdevice_count: int
    device_on_raw: bytes | None
    pump_power_raw: bytes | None


@dataclass
class OaseLocalTlsSession:
    """An established controller-to-client TLS connection.

    The session owns the listener that accepted it. Close it when the local
    operation is complete to release TCP/5999 again.
    """

    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    server: asyncio.AbstractServer

    async def async_request(
        self,
        packet_type: int,
        payload: bytes = b"",
        *,
        transaction_number: int = 0,
        timeout: float = 5.0,
    ) -> onet.OnetPacket:
        """Send one O-Net request through TLS and return its complete reply."""
        self.writer.write(onet.encode_packet(packet_type, payload, transaction_number))
        await self.writer.drain()
        header = await asyncio.wait_for(self.reader.readexactly(16), timeout)
        payload_length = struct.unpack_from("<I", header, 4)[0]
        body = await asyncio.wait_for(self.reader.readexactly(payload_length), timeout)
        return onet.parse_packet(header + body)

    async def async_authenticate(self, password: str, *, timeout: float = 5.0) -> None:
        """Authenticate this local TLS session with its controller password.

        The credential is supplied by the caller and is never retained or
        included in errors. A successful password check is required before a
        controller accepts operational O-Net commands over this channel.
        """
        reply = await self.async_request(
            onet.PacketType.PASSWORD_CHECK,
            onet.encode_password_payload(password),
            timeout=timeout,
        )
        expected_type = onet.reply_type(onet.PacketType.PASSWORD_CHECK)
        if reply.packet_type != expected_type:
            raise OaseLocalAuthenticationError(
                f"unexpected password check reply type 0x{reply.packet_type:04x}"
            )
        if len(reply.payload) != 1:
            raise OaseLocalAuthenticationError("malformed password check reply")
        if reply.payload[0] == 1:
            return
        if reply.payload[0] == 2:
            raise OaseLocalAuthenticationError("controller rejected local credentials")
        raise OaseLocalAuthenticationError("unknown password check reply state")

    async def async_close(self) -> None:
        """Close the TLS connection and its temporary listener."""
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except (ConnectionError, OSError):
            pass
        self.server.close()
        await self.server.wait_closed()


class OaseLocalSessionManager:
    """Keep one authenticated reverse-TLS session open per local controller."""

    def __init__(self) -> None:
        self._sessions: dict[str, OaseLocalTlsSession] = {}

    async def async_read_egc_devices(
        self, gateway_id: str, serial_number: str, password: str
    ) -> tuple[LocalEgcDeviceState, ...]:
        """Read a gateway, reconnecting through discovery once if necessary."""
        session = self._sessions.get(gateway_id)
        if session is None:
            session = await self._async_connect(gateway_id, serial_number, password)
        try:
            return await async_read_egc_devices(session)
        except (ConnectionError, OSError, TimeoutError, ValueError, OaseLocalTransportError):
            await self._async_drop(gateway_id)
            session = await self._async_connect(gateway_id, serial_number, password)
            try:
                return await async_read_egc_devices(session)
            except (ConnectionError, OSError, TimeoutError, ValueError, OaseLocalTransportError):
                await self._async_drop(gateway_id)
                raise

    async def async_close(self) -> None:
        """Close every reverse-TLS listener and session owned by this manager."""
        await asyncio.gather(
            *(session.async_close() for session in self._sessions.values()), return_exceptions=True
        )
        self._sessions.clear()

    async def _async_connect(
        self, gateway_id: str, serial_number: str, password: str
    ) -> OaseLocalTlsSession:
        controllers = await async_discover_controllers()
        matches = [item for item in controllers if item.info.serial_number == serial_number]
        if len(matches) != 1:
            raise OaseLocalTransportError("controller was not uniquely found by local discovery")
        session = await async_open_tls_session(matches[0].host)
        try:
            await session.async_authenticate(password)
        except Exception:
            await session.async_close()
            raise
        self._sessions[gateway_id] = session
        return session

    async def _async_drop(self, gateway_id: str) -> None:
        """Forget and close one failed session."""
        session = self._sessions.pop(gateway_id, None)
        if session is not None:
            await session.async_close()


async def async_read_local_egc_devices(
    host: str, password: str, *, timeout: float = 10.0
) -> tuple[LocalEgcDeviceState, ...]:
    """Authenticate and read the EGC bus locally without changing controller state."""
    session = await async_open_tls_session(host, timeout=timeout)
    try:
        await session.async_authenticate(password, timeout=timeout)
        return await async_read_egc_devices(session, timeout=timeout)
    finally:
        await session.async_close()


async def async_read_egc_devices(
    session: OaseLocalTlsSession, *, timeout: float = 5.0
) -> tuple[LocalEgcDeviceState, ...]:
    """Read the connected EGC device list and basic pump parameters from a session."""
    discovery = await session.async_request(
        rdm.EGC_DISCOVERY, rdm.discovery_packet_payload(), timeout=timeout
    )
    if discovery.packet_type != onet.reply_type(rdm.EGC_DISCOVERY):
        raise OaseLocalTransportError("controller returned an unexpected EGC discovery reply")

    devices: list[LocalEgcDeviceState] = []
    for transaction_number, device in enumerate(rdm.parse_discovery_reply(discovery.payload), start=1):
        devices.append(
            LocalEgcDeviceState(
                article_number=device.article_number,
                device_number=device.device_number,
                manufacturer_id=device.manufacturer_id,
                subdevice_count=device.subdevice_count,
                device_on_raw=await _async_rdm_get(
                    session, device.device_number, rdm.Pid.DEVICE_ON, transaction_number, timeout
                ),
                pump_power_raw=await _async_rdm_get(
                    session, device.device_number, rdm.Pid.PUMP_POWER, transaction_number, timeout
                ),
            )
        )
    return tuple(devices)


async def _async_rdm_get(
    session: OaseLocalTlsSession,
    device_number: int,
    pid: int,
    transaction_number: int,
    timeout: float,
) -> bytes | None:
    """Issue one acknowledged local RDM GET request."""
    frame = rdm.build_frame(
        rdm.Uid.for_device(device_number), rdm.CommandClass.GET_COMMAND, pid
    )
    reply = await session.async_request(
        rdm.RDM_REQUEST, frame, transaction_number=transaction_number, timeout=timeout
    )
    if reply.packet_type != onet.reply_type(rdm.RDM_REQUEST):
        return None
    response = rdm.parse_frame(reply.payload)
    return response.data if response.checksum_valid and response.is_ack else None


def local_credential_from_inventory(inventory: Mapping[str, Any], gateway_id: str) -> str:
    """Return one gateway's local credential from a cloud inventory response.

    The credential remains in memory only. Callers must neither log the return
    value nor place it in a config entry or other persistent Home Assistant
    storage.
    """
    gateways = inventory.get("gateways")
    if not isinstance(gateways, list):
        raise OaseLocalCredentialError("cloud inventory contains no gateways")

    gateway = next(
        (
            candidate
            for candidate in gateways
            if isinstance(candidate, Mapping) and candidate.get("id") == gateway_id
        ),
        None,
    )
    if gateway is None:
        raise OaseLocalCredentialError("gateway is not present in cloud inventory")

    attributes = gateway.get("customAttributesJson")
    try:
        parsed_attributes = json.loads(attributes) if isinstance(attributes, str) else attributes
    except json.JSONDecodeError as err:
        raise OaseLocalCredentialError("gateway local credential data is invalid") from err
    if not isinstance(parsed_attributes, list):
        raise OaseLocalCredentialError("gateway local credential data is missing")

    for attribute in parsed_attributes:
        if not isinstance(attribute, Mapping) or attribute.get("Id") != _LOCAL_CREDENTIAL_ATTRIBUTE_ID:
            continue
        value = attribute.get("Value")
        credential = value.get("Value") if isinstance(value, Mapping) else None
        if isinstance(credential, str) and credential:
            return credential
        break
    raise OaseLocalCredentialError("gateway local credential is unavailable")


def local_credentials_from_inventory(inventory: Mapping[str, Any]) -> dict[str, str]:
    """Return every usable gateway credential from a cloud inventory response."""
    gateways = inventory.get("gateways")
    if not isinstance(gateways, list):
        return {}
    credentials: dict[str, str] = {}
    for gateway in gateways:
        if not isinstance(gateway, Mapping) or not isinstance(gateway.get("id"), str):
            continue
        try:
            credentials[gateway["id"]] = local_credential_from_inventory(inventory, gateway["id"])
        except OaseLocalCredentialError:
            continue
    return credentials


def device_info_probe_packet() -> bytes:
    """Build the read-only O-Net DEVICE_INFO local probe."""
    return onet.encode_packet(onet.PacketType.DEVICE_INFO)


async def async_discover_controllers(*, timeout: float = 3.0) -> list[LocalDiscoveryResult]:
    """Discover local controllers using O-Net UDP broadcast and multicast.

    This is O-Net discovery, not DNS-SD: DEVICE_INFO is sent to UDP/5959 on
    both the IPv4 limited broadcast address and OASE's multicast group. Replies
    are deduplicated by controller serial number.
    """
    loop = asyncio.get_running_loop()
    replies: asyncio.Queue[tuple[bytes, tuple[str, int]]] = asyncio.Queue()
    udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    udp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    udp_socket.bind(("0.0.0.0", LOCAL_UDP_PORT))
    transport, _protocol = await loop.create_datagram_endpoint(
        lambda: _DiscoveryProtocol(replies), sock=udp_socket
    )
    try:
        packet = device_info_probe_packet()
        transport.sendto(packet, (LOCAL_DISCOVERY_BROADCAST, LOCAL_UDP_PORT))
        transport.sendto(packet, (LOCAL_DISCOVERY_MULTICAST, LOCAL_UDP_PORT))

        results: dict[str, LocalDiscoveryResult] = {}
        deadline = loop.time() + timeout
        while (remaining := deadline - loop.time()) > 0:
            try:
                data, address = await asyncio.wait_for(replies.get(), remaining)
            except TimeoutError:
                break
            result = _parse_discovery_result(data, address[0])
            if result is not None:
                results.setdefault(result.info.serial_number or result.host, result)
        return sorted(results.values(), key=lambda result: (result.info.serial_number, result.host))
    finally:
        transport.close()


def tcp_connection_request_packet(
    listener_port: int = LOCAL_TCP_PORT,
    *,
    encryption: bool = True,
    timestamp: int | None = None,
    transaction_number: int = 2,
) -> bytes:
    """Build the UDP request that asks a controller to establish reverse TLS."""
    if not 0 <= listener_port <= 0xFFFF:
        raise ValueError("listener port must fit in an unsigned 16-bit integer")
    unix_time = int(time.time()) if timestamp is None else int(timestamp)
    if not 0 <= unix_time <= 0xFFFFFFFF:
        raise ValueError("timestamp must fit in an unsigned 32-bit integer")
    payload = struct.pack("<BHI", int(encryption), listener_port, unix_time)
    return onet.encode_packet(onet.PacketType.TCP_REQ, payload, transaction_number)


async def async_probe_controller(host: str, *, timeout: float = 5.0) -> LocalDiscoveryResult:
    """Probe one controller's UDP/5959 endpoint for its O-Net identity.

    A target address is deliberately mandatory: this does not broadcast on the
    user's LAN. It is intended for controlled verification against a controller
    whose IP address is already known.
    """
    loop = asyncio.get_running_loop()
    reply: asyncio.Future[bytes] = loop.create_future()
    transport, _protocol = await loop.create_datagram_endpoint(
        lambda: _ProbeProtocol(
            reply,
            device_info_probe_packet(),
            expected_packet_type=onet.reply_type(onet.PacketType.DEVICE_INFO),
        ),
        local_addr=("0.0.0.0", LOCAL_UDP_PORT),
        remote_addr=(host, LOCAL_UDP_PORT),
    )
    try:
        data = await asyncio.wait_for(reply, timeout)
    except TimeoutError as err:
        raise OaseLocalTransportError(f"no UDP reply from {host}:{LOCAL_UDP_PORT}") from err
    finally:
        transport.close()

    result = _parse_discovery_result(data, host)
    if result is None:
        raise OaseLocalTransportError(f"invalid O-Net discovery reply from {host}")
    return result


def _parse_discovery_result(data: bytes, host: str) -> LocalDiscoveryResult | None:
    """Decode one matching UDP DEVICE_INFO response, ignoring unrelated frames."""
    try:
        packet = onet.parse_packet(data)
        if packet.packet_type != onet.reply_type(onet.PacketType.DEVICE_INFO):
            return None
        return LocalDiscoveryResult(host=host, info=onet.parse_discovery_reply(packet.payload))
    except ValueError:
        return None


async def async_open_tls_session(
    host: str,
    *,
    listener_port: int = LOCAL_TCP_PORT,
    timeout: float = 10.0,
) -> OaseLocalTlsSession:
    """Ask a controller to open an encrypted local connection to this host.

    The caller must ensure that ``listener_port`` is reachable from the
    controller. No O-Net command is sent through the resulting session; use
    :meth:`OaseLocalTlsSession.async_request` for controlled read-only testing.
    """
    controller_ip = str(ipaddress.ip_address(host))
    loop = asyncio.get_running_loop()
    connection: asyncio.Future[tuple[asyncio.StreamReader, asyncio.StreamWriter]] = (
        loop.create_future()
    )
    context = _create_server_tls_context()

    async def _on_connection(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        if not peer or peer[0] != controller_ip or connection.done():
            writer.close()
            await writer.wait_closed()
            return
        connection.set_result((reader, writer))

    server = await asyncio.start_server(
        _on_connection,
        host="0.0.0.0",
        port=listener_port,
        ssl=context,
    )
    try:
        # A controller only accepts the reverse-connection request after the
        # client has performed local discovery from the same UDP source port.
        discovery_reply: asyncio.Future[bytes] = loop.create_future()
        transport, protocol = await loop.create_datagram_endpoint(
            lambda: _ProbeProtocol(
                discovery_reply,
                device_info_probe_packet(),
                expected_packet_type=onet.reply_type(onet.PacketType.DEVICE_INFO),
            ),
            local_addr=("0.0.0.0", LOCAL_UDP_PORT),
            remote_addr=(controller_ip, LOCAL_UDP_PORT),
        )
        discovery = await asyncio.wait_for(discovery_reply, timeout)
        discovery_packet = onet.parse_packet(discovery)
        onet.parse_discovery_reply(discovery_packet.payload)

        reply: asyncio.Future[bytes] = loop.create_future()
        protocol.send_request(
            reply,
            tcp_connection_request_packet(listener_port),
            expected_packet_type=onet.reply_type(onet.PacketType.TCP_REQ),
        )
        response = await asyncio.wait_for(reply, timeout)
        _validate_tcp_connection_reply(response)
        reader, writer = await asyncio.wait_for(connection, timeout)
        return OaseLocalTlsSession(reader, writer, server)
    except (ConnectionError, OSError, TimeoutError, ValueError) as err:
        server.close()
        await server.wait_closed()
        raise OaseLocalTransportError(
            f"controller did not establish TLS on port {listener_port}"
        ) from err
    finally:
        if "transport" in locals():
            transport.close()


def _validate_tcp_connection_reply(data: bytes) -> None:
    """Validate the controller's acknowledgement of a reverse-TLS request."""
    packet = onet.parse_packet(data)
    if packet.packet_type != onet.reply_type(onet.PacketType.TCP_REQ):
        raise ValueError(f"unexpected packet type 0x{packet.packet_type:04x}")
    if len(packet.payload) < 2 or packet.payload[0] == 0:
        raise ValueError("controller rejected TCP connection request")


def _create_server_tls_context() -> ssl.SSLContext:
    """Create the self-signed TLS 1.2 server context accepted by controllers."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(UTC)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, _CERTIFICATE_COMMON_NAME)])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + _CERTIFICATE_VALIDITY)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(private_key, hashes.SHA256())
    )
    with tempfile.TemporaryDirectory(prefix="oase-control-") as directory:
        certificate_path = Path(directory) / "certificate.pem"
        key_path = Path(directory) / "private-key.pem"
        certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(
            private_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.maximum_version = ssl.TLSVersion.TLSv1_2
        context.set_ciphers("ECDHE-RSA-AES128-GCM-SHA256")
        context.load_cert_chain(certificate_path, key_path)
    return context


class _ProbeProtocol(asyncio.DatagramProtocol):
    """Send one datagram and resolve with the first received reply."""

    def __init__(
        self,
        reply: asyncio.Future[bytes],
        request: bytes,
        *,
        expected_packet_type: int | None = None,
    ) -> None:
        self._reply = reply
        self._request = request
        self._expected_packet_type = expected_packet_type
        self._transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        assert isinstance(transport, asyncio.DatagramTransport)
        self._transport = transport
        transport.sendto(self._request)

    def send_request(
        self,
        reply: asyncio.Future[bytes],
        request: bytes,
        *,
        expected_packet_type: int | None = None,
    ) -> None:
        """Send another request using the same source UDP port."""
        if self._transport is None:
            raise RuntimeError("UDP transport is not connected")
        self._reply = reply
        self._expected_packet_type = expected_packet_type
        self._transport.sendto(request)

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        if self._expected_packet_type is not None:
            try:
                if onet.parse_packet(data).packet_type != self._expected_packet_type:
                    return
            except ValueError:
                return
        if not self._reply.done():
            self._reply.set_result(data)

    def error_received(self, exc: Exception) -> None:
        if not self._reply.done():
            self._reply.set_exception(exc)


class _DiscoveryProtocol(asyncio.DatagramProtocol):
    """Collect UDP datagrams received during one O-Net discovery window."""

    def __init__(self, replies: asyncio.Queue[tuple[bytes, tuple[str, int]]]) -> None:
        self._replies = replies

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self._replies.put_nowait((data, addr))
