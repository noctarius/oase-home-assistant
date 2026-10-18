"""Local transport primitives for OASE controllers.

The local setup is controller-initiated: after a UDP request on port 5959, the
controller opens a TLS connection back to a listener on port 5999.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import ipaddress
import json
from pathlib import Path
import socket
import ssl
import struct
import tempfile
import time
from typing import Any, Final, Mapping, TypeVar
import weakref

from pyoase import onet, rdm

LOCAL_UDP_PORT = 5959
LOCAL_TCP_PORT = 5999
LOCAL_DISCOVERY_BROADCAST = "255.255.255.255"
LOCAL_DISCOVERY_MULTICAST = "224.0.0.251"
_CERTIFICATE_COMMON_NAME: Final = "com.oase.easycontrol"
_CERTIFICATE_VALIDITY: Final = timedelta(days=14)
# Regenerate well before expiry; a listener may outlive one certificate.
_CERTIFICATE_ROTATION: Final = timedelta(days=7)
_LOCAL_CREDENTIAL_ATTRIBUTE_ID: Final = 101
# Replies are small; anything larger means the stream is out of sync.
_MAX_PAYLOAD_LENGTH: Final = 64 * 1024
# A dead controller never answers TLS close_notify; don't wait 30 s for it.
_CLOSE_TIMEOUT: Final = 2.0
_SESSION_ERRORS: Final = (ConnectionError, OSError, TimeoutError, ValueError)
# Reconnect delays after failed connects; a rejected credential waits longest.
_RECONNECT_BACKOFF_MIN: Final = 5.0
_RECONNECT_BACKOFF_MAX: Final = 300.0
# Hardware-verified local cloud-state request/reply pair.  The first response
# byte is 0/1 for cloud connection disabled/enabled; the remaining bytes are
# retained by the controller protocol but are not exposed until understood.
_GET_CLOUD_STATE: Final = 0xA400
_GET_CLOUD_STATE_REPLY: Final = 0xA4FF
_CLOUD_STATE_LENGTH: Final = 5

_T = TypeVar("_T")


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
    operating_hours: int | None = None


@dataclass(frozen=True)
class LocalCloudState:
    """Validated local cloud state; only ``connection_enabled`` is decoded."""

    connection_enabled: bool
    raw: bytes


@dataclass(frozen=True)
class LocalGatewayState:
    """State available from one authenticated local controller session."""

    egc_devices: tuple[LocalEgcDeviceState, ...]
    sockets: onet.SocketState | None = None
    cloud_connection_enabled: bool | None = None


@dataclass
class OaseLocalTlsSession:
    """An established controller-to-client TLS connection.

    The session holds a lease on the shared reverse-TLS listener. Close it when
    the local operation is complete so the listener can release TCP/5999.
    """

    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    listener: _ReverseTlsListener
    _closed: bool = field(default=False, init=False, repr=False)

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
        await asyncio.wait_for(self.writer.drain(), timeout)
        header = await asyncio.wait_for(self.reader.readexactly(16), timeout)
        payload_length = struct.unpack_from("<I", header, 4)[0]
        if payload_length > _MAX_PAYLOAD_LENGTH:
            raise OaseLocalTransportError("controller reply exceeds the maximum O-Net payload size")
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

    async def async_close(self, *, abort: bool = False) -> None:
        """Close the TLS connection and release the shared listener.

        ``abort`` drops a connection whose state is unknown without the TLS
        close handshake. A graceful close falls back to aborting when the
        controller does not complete it promptly.
        """
        if self._closed:
            return
        self._closed = True
        try:
            if abort:
                self.writer.transport.abort()
                return
            self.writer.close()
            try:
                await asyncio.wait_for(self.writer.wait_closed(), _CLOSE_TIMEOUT)
            except (ConnectionError, OSError, TimeoutError):
                self.writer.transport.abort()
        except BaseException:
            self.writer.transport.abort()
            raise
        finally:
            self.listener.release()


class _ReverseTlsListener:
    """One TCP listener shared by every reverse-TLS session on a port.

    Controllers always connect back to the same port, so per-session listeners
    cannot coexist. Accepted connections are routed to the waiter registered
    for the controller's address; anything else is dropped. The listener is
    open only while at least one session or connection attempt holds a lease.
    """

    def __init__(self, port: int) -> None:
        self.port = port
        self._server: asyncio.Server | None = None
        self._context: ssl.SSLContext | None = None
        self._context_created = 0.0
        self._users = 0
        self._lock = asyncio.Lock()
        self._waiters: dict[
            str, asyncio.Future[tuple[asyncio.StreamReader, asyncio.StreamWriter]]
        ] = {}

    async def async_acquire(self) -> None:
        """Take a lease, starting the listener if it is not running."""
        async with self._lock:
            loop = asyncio.get_running_loop()
            if (
                self._context is None
                or loop.time() - self._context_created > _CERTIFICATE_ROTATION.total_seconds()
            ):
                # RSA key generation and loading the PEM files block.
                self._context = await loop.run_in_executor(None, _create_server_tls_context)
                self._context_created = loop.time()
                if self._server is not None:
                    # Stop accepting with the old certificate. Established
                    # sessions keep their connections.
                    self._server.close()
                    self._server = None
            if self._server is None:
                self._server = await asyncio.start_server(
                    self._async_on_connection, host="0.0.0.0", port=self.port, ssl=self._context
                )
            self._users += 1

    def release(self) -> None:
        """Return a lease, closing the listener once nobody needs it."""
        self._users -= 1
        if self._users == 0 and self._server is not None:
            server, self._server = self._server, None
            server.close()
            server.abort_clients()

    def expect(
        self, host: str
    ) -> asyncio.Future[tuple[asyncio.StreamReader, asyncio.StreamWriter]]:
        """Register interest in the next connection from one controller."""
        if host in self._waiters:
            raise OaseLocalTransportError("a connection from this controller is already pending")
        waiter = asyncio.get_running_loop().create_future()
        self._waiters[host] = waiter
        return waiter

    def forget(self, host: str, waiter: asyncio.Future[Any]) -> None:
        """Stop routing connections from a controller to an expired waiter."""
        if self._waiters.get(host) is waiter:
            del self._waiters[host]

    async def _async_on_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername")
        waiter = self._waiters.get(peer[0]) if peer else None
        if waiter is None or waiter.done():
            writer.transport.abort()
            return
        waiter.set_result((reader, writer))


@dataclass
class _LocalNetwork:
    """Local sockets shared by every config entry running on one event loop."""

    udp_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    listeners: dict[int, _ReverseTlsListener] = field(default_factory=dict)


_NETWORKS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, _LocalNetwork] = (
    weakref.WeakKeyDictionary()
)


def _local_network() -> _LocalNetwork:
    """Return the running loop's shared local network state."""
    loop = asyncio.get_running_loop()
    network = _NETWORKS.get(loop)
    if network is None:
        network = _NETWORKS[loop] = _LocalNetwork()
    return network


async def _async_close_datagram(transport: asyncio.BaseTransport) -> None:
    """Close a UDP transport and let its socket be released before returning.

    The socket is closed on the next loop iteration; yielding once ensures the
    next user of UDP/5959 can bind it.
    """
    transport.close()
    await asyncio.sleep(0)


@dataclass(frozen=True)
class _ReconnectBackoff:
    """When a gateway may be connected again after consecutive failures."""

    retry_at: float
    failures: int
    credential_hash: int


class OaseLocalSessionManager:
    """Keep one authenticated reverse-TLS session open per local controller."""

    def __init__(self) -> None:
        self._sessions: dict[str, OaseLocalTlsSession] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._backoff: dict[str, _ReconnectBackoff] = {}
        self._closed = False

    async def async_read_egc_devices(
        self, gateway_id: str, serial_number: str, password: str
    ) -> tuple[LocalEgcDeviceState, ...]:
        """Read a gateway, reconnecting through discovery once if necessary."""
        return await self._async_with_session(
            gateway_id, serial_number, password, async_read_egc_devices
        )

    async def async_read_gateway_state(
        self,
        gateway_id: str,
        serial_number: str,
        password: str,
        *,
        read_sockets: bool,
    ) -> LocalGatewayState:
        """Read EGC state and, for an FM-Master, its live socket scene."""
        return await self._async_with_session(
            gateway_id,
            serial_number,
            password,
            lambda session: async_read_gateway_state(session, read_sockets=read_sockets),
        )

    async def async_set_egc_parameter(
        self,
        gateway_id: str,
        serial_number: str,
        password: str,
        device_number: int,
        pid: int,
        value: bytes,
    ) -> None:
        """Set one EGC RDM parameter, reconnecting once if the session failed."""
        await self._async_with_session(
            gateway_id,
            serial_number,
            password,
            lambda session: async_set_egc_parameter(session, device_number, pid, value),
        )

    async def async_set_socket(
        self,
        gateway_id: str,
        serial_number: str,
        password: str,
        socket: onet.Socket,
        value: int,
    ) -> None:
        """Set one FM-Master socket channel on the retained local session."""
        await self._async_with_session(
            gateway_id,
            serial_number,
            password,
            lambda session: async_set_socket(session, socket, value),
        )

    async def async_set_dimmer(
        self,
        gateway_id: str,
        serial_number: str,
        password: str,
        on: bool,
        brightness: int | None,
    ) -> None:
        """Set a dimmer level, then its on/off channel, on one local session."""
        await self._async_with_session(
            gateway_id,
            serial_number,
            password,
            lambda session: async_set_dimmer(session, on, brightness),
        )

    async def async_close(self) -> None:
        """Close every reverse-TLS listener and session owned by this manager."""
        self._closed = True
        locks = tuple(self._locks.values())
        for lock in locks:
            await lock.acquire()
        try:
            await asyncio.gather(
                *(session.async_close() for session in self._sessions.values()), return_exceptions=True
            )
            self._sessions.clear()
            self._locks.clear()
            self._backoff.clear()
        finally:
            for lock in locks:
                lock.release()

    async def async_prune(self, gateway_ids: set[str]) -> None:
        """Release sessions and locks belonging to gateways no longer in inventory."""
        for gateway_id in tuple(self._locks):
            if gateway_id in gateway_ids:
                continue
            lock = self._locks.get(gateway_id)
            if lock is None:
                continue
            async with lock:
                await self._async_drop(gateway_id)
                self._backoff.pop(gateway_id, None)
                if self._locks.get(gateway_id) is lock:
                    del self._locks[gateway_id]

    def _raise_if_closed(self) -> None:
        """Reject work started after the config entry has been unloaded."""
        if self._closed:
            raise OaseLocalTransportError("local session manager is closed")

    @asynccontextmanager
    async def _async_gateway_lock(self, gateway_id: str) -> AsyncIterator[None]:
        """Hold the lock that serialises one controller's request/reply stream.

        A waiter may wake up holding a lock that was pruned meanwhile; it then
        retries with the current one, so two callers never share a stream.
        """
        while True:
            self._raise_if_closed()
            lock = self._locks.setdefault(gateway_id, asyncio.Lock())
            async with lock:
                if self._locks.get(gateway_id) is lock:
                    self._raise_if_closed()
                    yield
                    return

    async def _async_with_session(
        self,
        gateway_id: str,
        serial_number: str,
        password: str,
        operation: Callable[[OaseLocalTlsSession], Awaitable[_T]],
    ) -> _T:
        """Run one operation on the gateway's session, replacing a stale one once."""
        async with self._async_gateway_lock(gateway_id):
            while True:
                session = self._sessions.get(gateway_id)
                reused = session is not None
                if session is None:
                    session = await self._async_connect_with_backoff(
                        gateway_id, serial_number, password
                    )
                try:
                    return await operation(session)
                except (*_SESSION_ERRORS, OaseLocalTransportError) as err:
                    await self._async_drop(gateway_id, abort=True)
                    if not reused:
                        if isinstance(err, OaseLocalTransportError):
                            raise
                        raise OaseLocalTransportError("local controller request failed") from err
                except BaseException:
                    # A cancelled request leaves its reply in the stream; the
                    # next caller would read it as the answer to its own.
                    await self._async_drop(gateway_id, abort=True)
                    raise

    async def _async_connect_with_backoff(
        self, gateway_id: str, serial_number: str, password: str
    ) -> OaseLocalTlsSession:
        """Connect unless recent attempts failed, backing off exponentially.

        Without this an unreachable controller costs a discovery window, and a
        reachable one a full TLS setup, on every 5 s status refresh. A changed
        credential is tried immediately.
        """
        now = asyncio.get_running_loop().time()
        credential_hash = hash(password)
        backoff = self._backoff.get(gateway_id)
        if backoff is not None and backoff.credential_hash != credential_hash:
            backoff = None
        if backoff is not None and now < backoff.retry_at:
            raise OaseLocalTransportError("waiting before reconnecting to the local controller")
        try:
            session = await self._async_connect(gateway_id, serial_number, password)
        except OaseLocalTransportError as err:
            failures = backoff.failures + 1 if backoff is not None else 1
            delay = (
                _RECONNECT_BACKOFF_MAX
                if isinstance(err, OaseLocalAuthenticationError)
                else min(_RECONNECT_BACKOFF_MIN * 2 ** (failures - 1), _RECONNECT_BACKOFF_MAX)
            )
            self._backoff[gateway_id] = _ReconnectBackoff(
                asyncio.get_running_loop().time() + delay, failures, credential_hash
            )
            raise
        self._backoff.pop(gateway_id, None)
        return session

    async def _async_connect(
        self, gateway_id: str, serial_number: str, password: str
    ) -> OaseLocalTlsSession:
        controllers = await async_discover_controllers(serial_number=serial_number)
        matches = [item for item in controllers if item.info.serial_number == serial_number]
        if len(matches) != 1:
            raise OaseLocalTransportError("controller was not uniquely found by local discovery")
        session = await async_open_tls_session(matches[0].host)
        try:
            await session.async_authenticate(password)
        except _SESSION_ERRORS as err:
            await session.async_close(abort=True)
            raise OaseLocalTransportError("local controller authentication failed") from err
        except BaseException:
            await session.async_close(abort=True)
            raise
        await self._async_drop(gateway_id, abort=True)
        self._sessions[gateway_id] = session
        return session

    async def _async_drop(self, gateway_id: str, *, abort: bool = False) -> None:
        """Forget and close one session."""
        session = self._sessions.pop(gateway_id, None)
        if session is not None:
            await session.async_close(abort=abort)


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
                operating_hours=await _async_read_operating_hours(
                    session, device.device_number, transaction_number, timeout
                ),
            )
        )
    return tuple(devices)


async def async_read_gateway_state(
    session: OaseLocalTlsSession, *, read_sockets: bool, timeout: float = 5.0
) -> LocalGatewayState:
    """Read all generic EGC state and an FM-Master's socket scene when requested."""
    egc_devices = await async_read_egc_devices(session, timeout=timeout)
    sockets = await async_read_socket_state(session, timeout=timeout) if read_sockets else None
    cloud_connection_enabled = await async_read_cloud_connection_enabled(session, timeout=timeout)
    return LocalGatewayState(egc_devices, sockets, cloud_connection_enabled)


async def async_read_cloud_connection_enabled(
    session: OaseLocalTlsSession, *, timeout: float = 5.0
) -> bool:
    """Read the controller's locally configured cloud-connection setting.

    This is deliberately read-only. Enabling cloud access requires a separate
    cloud registration/key lifecycle, so it is not represented as a writable
    Home Assistant control.
    """
    return (await async_read_cloud_state(session, timeout=timeout)).connection_enabled


async def async_read_cloud_state(
    session: OaseLocalTlsSession, *, timeout: float = 5.0
) -> LocalCloudState:
    """Read and strictly validate the complete local cloud-state record."""
    reply = await session.async_request(_GET_CLOUD_STATE, timeout=timeout)
    if reply.packet_type != _GET_CLOUD_STATE_REPLY:
        raise OaseLocalTransportError("controller returned an unexpected cloud-state reply")
    if len(reply.payload) != _CLOUD_STATE_LENGTH or reply.payload[0] not in (0, 1):
        raise OaseLocalTransportError("controller returned an invalid cloud-state reply")
    return LocalCloudState(connection_enabled=bool(reply.payload[0]), raw=reply.payload)


async def async_read_socket_state(
    session: OaseLocalTlsSession, *, timeout: float = 5.0
) -> onet.SocketState:
    """Read and validate the FM-Master's five-byte live socket scene."""
    reply = await session.async_request(
        onet.PacketType.GET_LIVE_SCENE, onet.build_socket_get_payload(), timeout=timeout
    )
    if reply.packet_type != onet.reply_type(onet.PacketType.GET_LIVE_SCENE):
        raise OaseLocalTransportError("controller returned an unexpected live-scene reply")
    try:
        scene = onet.parse_live_scene_reply(reply.payload)
        if scene.scene_type != 0x64 or scene.scene_length != 5:
            raise ValueError("unexpected socket scene format")
        return onet.parse_socket_state(scene.data)
    except ValueError as err:
        raise OaseLocalTransportError("controller returned an invalid socket scene") from err


async def async_set_socket(
    session: OaseLocalTlsSession, socket: onet.Socket, value: int, *, timeout: float = 5.0
) -> None:
    """Set one FM-Master socket channel and require an affirmative matching reply."""
    try:
        payload = onet.build_socket_set_payload(socket, value)
    except ValueError as err:
        raise OaseLocalTransportError("invalid FM-Master socket command") from err
    reply = await session.async_request(onet.PacketType.SET_LIVE_SCENE, payload, timeout=timeout)
    if reply.packet_type != onet.reply_type(onet.PacketType.SET_LIVE_SCENE):
        raise OaseLocalTransportError("controller returned an unexpected socket set reply")
    try:
        accepted = onet.parse_set_reply(reply.payload)
    except ValueError as err:
        raise OaseLocalTransportError("controller returned an invalid socket set reply") from err
    if not accepted:
        raise OaseLocalTransportError("controller rejected the socket set command")


async def async_set_dimmer(
    session: OaseLocalTlsSession,
    on: bool,
    brightness: int | None,
    *,
    timeout: float = 5.0,
) -> None:
    """Set the optional dimmer level before its on/off state."""
    if brightness is not None:
        await async_set_socket(
            session, onet.Socket.DIMMER_VALUE, max(0, min(255, int(brightness))), timeout=timeout
        )
    await async_set_socket(
        session, onet.Socket.DIMMER_ONOFF, onet.ON if on else onet.OFF, timeout=timeout
    )


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
    if not response.checksum_valid:
        return None
    if response.command_class != rdm.CommandClass.GET_COMMAND_RESPONSE or response.pid != int(pid):
        # Not the reply to this request: the stream is out of sync.
        raise OaseLocalTransportError("controller returned a mismatched RDM get reply")
    return response.data if response.is_ack else None


async def _async_read_operating_hours(
    session: OaseLocalTlsSession,
    device_number: int,
    transaction_number: int,
    timeout: float,
) -> int | None:
    """Read the OASE runtime counter, falling back to standard RDM device hours."""
    for pid in (rdm.Pid.OPERATING_HOURS, rdm.Pid.DEVICE_HOURS):
        data = await _async_rdm_get(session, device_number, pid, transaction_number, timeout)
        if data is not None and len(data) >= 4:
            return int.from_bytes(data[:4], "big")
    return None


async def async_set_egc_parameter(
    session: OaseLocalTlsSession,
    device_number: int,
    pid: int,
    value: bytes,
    *,
    timeout: float = 5.0,
) -> None:
    """Write one RDM parameter and require an acknowledged matching reply."""
    frame = rdm.build_frame(
        rdm.Uid.for_device(device_number), rdm.CommandClass.SET_COMMAND, pid, value
    )
    reply = await session.async_request(rdm.RDM_REQUEST, frame, timeout=timeout)
    if reply.packet_type != onet.reply_type(rdm.RDM_REQUEST):
        raise OaseLocalTransportError("controller returned an unexpected RDM set reply")
    response = rdm.parse_frame(reply.payload)
    if not response.checksum_valid:
        raise OaseLocalTransportError("controller returned an RDM set reply with invalid checksum")
    if not response.is_ack:
        raise OaseLocalTransportError(f"controller did not acknowledge RDM PID 0x{int(pid):04x}")
    if response.command_class != rdm.CommandClass.SET_COMMAND_RESPONSE or response.pid != int(pid):
        raise OaseLocalTransportError("controller returned a mismatched RDM set reply")


def local_credential_from_inventory(inventory: Mapping[str, Any], gateway_id: str) -> str:
    """Return one gateway's local credential from a cloud inventory response.

    Callers must never log the return value. The integration persists it in
    its config entry so that, once set up, local control survives cloud outages.
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


async def async_discover_controllers(
    *, timeout: float = 3.0, serial_number: str | None = None
) -> list[LocalDiscoveryResult]:
    """Discover local controllers using O-Net UDP broadcast and multicast.

    This is O-Net discovery, not DNS-SD: DEVICE_INFO is sent to UDP/5959 on
    both the IPv4 limited broadcast address and OASE's multicast group. Replies
    are deduplicated by controller serial number. With ``serial_number`` the
    window ends as soon as that controller has answered.
    """
    async with _local_network().udp_lock:
        try:
            return await _async_discover_controllers(timeout, serial_number)
        except OSError as err:
            raise OaseLocalTransportError(f"local discovery failed on UDP {LOCAL_UDP_PORT}") from err


async def _async_discover_controllers(
    timeout: float, serial_number: str | None
) -> list[LocalDiscoveryResult]:
    loop = asyncio.get_running_loop()
    replies: asyncio.Queue[tuple[bytes, tuple[str, int]]] = asyncio.Queue()
    udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        udp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        udp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        udp_socket.bind(("0.0.0.0", LOCAL_UDP_PORT))
        transport, _protocol = await loop.create_datagram_endpoint(
            lambda: _DiscoveryProtocol(replies), sock=udp_socket
        )
    except BaseException:
        udp_socket.close()
        raise
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
                if serial_number is not None and result.info.serial_number == serial_number:
                    break
        return sorted(results.values(), key=lambda result: (result.info.serial_number, result.host))
    finally:
        await _async_close_datagram(transport)


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
    async with _local_network().udp_lock:
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
            await _async_close_datagram(transport)

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
    network = _local_network()
    listener = network.listeners.setdefault(listener_port, _ReverseTlsListener(listener_port))
    try:
        await listener.async_acquire()
    except OSError as err:
        raise OaseLocalTransportError(f"unable to listen for TLS on port {listener_port}") from err

    connection: asyncio.Future[tuple[asyncio.StreamReader, asyncio.StreamWriter]] | None = None
    session: OaseLocalTlsSession | None = None
    try:
        connection = listener.expect(controller_ip)
        try:
            async with network.udp_lock:
                await _async_request_reverse_connection(controller_ip, listener_port, timeout)
            reader, writer = await asyncio.wait_for(connection, timeout)
        finally:
            listener.forget(controller_ip, connection)
        session = OaseLocalTlsSession(reader, writer, listener)
        return session
    except (ConnectionError, OSError, TimeoutError, ValueError) as err:
        raise OaseLocalTransportError(
            f"controller did not establish TLS on port {listener_port}"
        ) from err
    finally:
        if session is None:
            _abort_unclaimed_connection(connection)
            listener.release()


async def _async_request_reverse_connection(
    controller_ip: str, listener_port: int, timeout: float
) -> None:
    """Ask a controller over UDP to open a TLS connection back to this host."""
    loop = asyncio.get_running_loop()
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
    try:
        discovery = await asyncio.wait_for(discovery_reply, timeout)
        discovery_packet = onet.parse_packet(discovery)
        onet.parse_discovery_reply(discovery_packet.payload)

        reply: asyncio.Future[bytes] = loop.create_future()
        protocol.send_request(
            reply,
            tcp_connection_request_packet(listener_port),
            expected_packet_type=onet.reply_type(onet.PacketType.TCP_REQ),
        )
        _validate_tcp_connection_reply(await asyncio.wait_for(reply, timeout))
    finally:
        await _async_close_datagram(transport)


def _abort_unclaimed_connection(
    connection: asyncio.Future[tuple[asyncio.StreamReader, asyncio.StreamWriter]] | None,
) -> None:
    """Drop a controller connection that arrived for a failed attempt.

    Leaving it open would leak the transport and keep the listener's
    ``wait_closed`` from ever returning.
    """
    if connection is None:
        return
    if not connection.done():
        connection.cancel()
    elif not connection.cancelled() and connection.exception() is None:
        _reader, writer = connection.result()
        writer.transport.abort()


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
