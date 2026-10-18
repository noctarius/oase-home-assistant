"""Tests for the local O-Net transport primitives."""

import asyncio
import socket
import ssl
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from pyoase import onet, rdm

from custom_components.oase_control import local
from custom_components.oase_control.local import (
    LOCAL_TCP_PORT,
    LOCAL_UDP_PORT,
    LocalDiscoveryResult,
    OaseLocalAuthenticationError,
    OaseLocalCredentialError,
    OaseLocalSessionManager,
    OaseLocalTlsSession,
    OaseLocalTransportError,
    _create_server_tls_context,
    _local_network,
    _parse_discovery_result,
    async_discover_controllers,
    async_read_socket_state,
    async_open_tls_session,
    async_read_egc_devices,
    async_set_dimmer,
    async_set_egc_parameter,
    async_set_socket,
    device_info_probe_packet,
    local_credential_from_inventory,
    local_credentials_from_inventory,
    tcp_connection_request_packet,
)


def test_device_info_probe_uses_the_shared_onet_codec():
    """The local probe stays wire-compatible with pyoase cloud relay frames."""
    packet = onet.parse_packet(device_info_probe_packet())

    assert packet.packet_type == onet.PacketType.DEVICE_INFO
    assert packet.payload == b""
    assert packet.version == onet.PROTOCOL_VERSION
    assert LOCAL_UDP_PORT == 5959
    assert LOCAL_TCP_PORT == 5999


def test_discovery_reply_parser_ignores_unrelated_datagrams():
    """Broadcast discovery accepts only O-Net DEVICE_INFO replies."""
    assert _parse_discovery_result(b"not-an-onet-packet", "10.0.0.2") is None
    assert _parse_discovery_result(
        onet.encode_packet(onet.PacketType.ALIVE), "10.0.0.2"
    ) is None


def test_discovery_reply_parser_returns_the_controller_identity():
    """A valid broadcast reply retains the sender address and O-Net identity."""
    payload = bytearray(324)
    payload[2:7] = b"OASE\0"
    payload[34:47] = b"606300063406\0"
    payload[66:87] = b"EGC Controller Cloud\0"
    payload[134:140] = bytes.fromhex("28562f8ac4ac")

    result = _parse_discovery_result(
        onet.encode_packet(onet.reply_type(onet.PacketType.DEVICE_INFO), bytes(payload)),
        "10.96.1.89",
    )

    assert result is not None
    assert result.host == "10.96.1.89"
    assert result.info.serial_number == "606300063406"
    assert result.info.mac_addresses == ("28:56:2f:8a:c4:ac",)


def test_tcp_connection_request_matches_the_local_reverse_tls_contract():
    """The controller receives encryption, listener port, and Unix time in order."""
    packet = onet.parse_packet(
        tcp_connection_request_packet(timestamp=1_793_000_000, transaction_number=2)
    )

    assert packet.packet_type == onet.PacketType.TCP_REQ
    assert packet.transaction_number == 2
    assert packet.payload == b"\x01\x6f\x17\x40\x02\xdf\x6a"


def test_local_server_context_matches_the_controller_tls_requirements():
    """The local listener serves TLS 1.2 with an RSA-compatible cipher suite."""
    context = _create_server_tls_context()

    assert context.minimum_version.name == "TLSv1_2"
    assert context.maximum_version.name == "TLSv1_2"
    assert any(cipher["name"] == "ECDHE-RSA-AES128-GCM-SHA256" for cipher in context.get_ciphers())


async def test_tls_session_authenticates_after_a_successful_password_check():
    """A one-byte success reply enables subsequent local O-Net commands."""
    session = OaseLocalTlsSession(AsyncMock(), AsyncMock(), AsyncMock())
    session.async_request = AsyncMock(
        return_value=onet.OnetPacket(onet.reply_type(onet.PacketType.PASSWORD_CHECK), b"\x01")
    )

    await session.async_authenticate("credential")

    session.async_request.assert_awaited_once_with(
        onet.PacketType.PASSWORD_CHECK,
        onet.encode_password_payload("credential"),
        timeout=5.0,
    )


@pytest.mark.parametrize("payload", [b"\x02", b"", b"\x03", b"\x01\x00"])
async def test_tls_session_rejects_unsuccessful_or_malformed_password_check_reply(payload):
    """A rejected or incomplete reply must never be treated as authenticated."""
    session = OaseLocalTlsSession(AsyncMock(), AsyncMock(), AsyncMock())
    session.async_request = AsyncMock(
        return_value=onet.OnetPacket(onet.reply_type(onet.PacketType.PASSWORD_CHECK), payload)
    )

    with pytest.raises(OaseLocalAuthenticationError):
        await session.async_authenticate("credential")


async def test_tls_session_rejects_a_wrong_password_check_packet_type():
    """A frame for another command cannot authenticate a local session."""
    session = OaseLocalTlsSession(AsyncMock(), AsyncMock(), AsyncMock())
    session.async_request = AsyncMock(return_value=onet.OnetPacket(onet.PacketType.ALIVE, b"\x01"))

    with pytest.raises(OaseLocalAuthenticationError, match="unexpected password check reply type"):
        await session.async_authenticate("credential")


async def test_local_rdm_set_writes_the_requested_parameter_and_requires_an_ack():
    """A local write uses an RDM SET frame and validates its corresponding reply."""
    device_number = 2148503843
    response = rdm.build_frame(
        rdm.CONTROLLER_UID,
        rdm.CommandClass.SET_COMMAND_RESPONSE,
        rdm.Pid.DEVICE_ON,
        source=rdm.Uid.for_device(device_number),
        port_id=rdm.ResponseType.ACK,
    )
    session = OaseLocalTlsSession(AsyncMock(), AsyncMock(), AsyncMock())
    session.async_request = AsyncMock(
        return_value=onet.OnetPacket(onet.reply_type(rdm.RDM_REQUEST), response)
    )

    await async_set_egc_parameter(session, device_number, rdm.Pid.DEVICE_ON, b"\xff")

    packet_type, frame = session.async_request.await_args.args
    request = rdm.parse_frame(frame)
    assert packet_type == rdm.RDM_REQUEST
    assert request.command_class == rdm.CommandClass.SET_COMMAND
    assert request.pid == rdm.Pid.DEVICE_ON
    assert request.data == b"\xff"


async def test_local_socket_scene_read_requires_the_fm_master_reply_shape():
    """A local FM-Master read decodes all four outlets only from a live scene."""
    payload = b"\x01" + (0).to_bytes(4, "little") + (1).to_bytes(4, "little") + bytes(
        [0x64, 5, 0xFF, 0x00, 0xFF, 0xFF, 0x80]
    )
    session = OaseLocalTlsSession(AsyncMock(), AsyncMock(), AsyncMock())
    session.async_request = AsyncMock(
        return_value=onet.OnetPacket(onet.reply_type(onet.PacketType.GET_LIVE_SCENE), payload)
    )

    state = await async_read_socket_state(session)

    assert state == onet.SocketState(True, False, True, True, 128)
    session.async_request.assert_awaited_once_with(
        onet.PacketType.GET_LIVE_SCENE, onet.build_socket_get_payload(), timeout=5.0
    )


@pytest.mark.parametrize(
    ("packet_type", "payload", "message"),
    [
        (onet.PacketType.ALIVE, b"\x01", "unexpected"),
        (onet.reply_type(onet.PacketType.SET_LIVE_SCENE), b"", "invalid"),
        (onet.reply_type(onet.PacketType.SET_LIVE_SCENE), b"\x00", "rejected"),
    ],
)
async def test_local_socket_write_rejects_nonaffirmative_replies(packet_type, payload, message):
    """Only a matching affirmative SET_LIVE_SCENE reply completes a local write."""
    session = OaseLocalTlsSession(AsyncMock(), AsyncMock(), AsyncMock())
    session.async_request = AsyncMock(return_value=onet.OnetPacket(packet_type, payload))

    with pytest.raises(OaseLocalTransportError, match=message):
        await async_set_socket(session, onet.Socket.SOCKET_2, onet.ON)


async def test_local_dimmer_write_sets_level_before_the_onoff_channel():
    """The app's dimmer ordering is retained over the local TLS session."""
    session = OaseLocalTlsSession(AsyncMock(), AsyncMock(), AsyncMock())
    session.async_request = AsyncMock(
        return_value=onet.OnetPacket(onet.reply_type(onet.PacketType.SET_LIVE_SCENE), b"\x01")
    )

    await async_set_dimmer(session, True, 128)

    assert session.async_request.await_args_list[0].args == (
        onet.PacketType.SET_LIVE_SCENE,
        onet.build_socket_set_payload(onet.Socket.DIMMER_VALUE, 128),
    )
    assert session.async_request.await_args_list[1].args == (
        onet.PacketType.SET_LIVE_SCENE,
        onet.build_socket_set_payload(onet.Socket.DIMMER_ONOFF, onet.ON),
    )


async def test_closed_session_manager_cannot_open_a_new_connection():
    """An unloading config entry cannot be resurrected by an in-flight poll."""
    manager = OaseLocalSessionManager()

    await manager.async_close()

    with pytest.raises(OaseLocalTransportError, match="closed"):
        await manager.async_read_egc_devices("gateway-id", "606300063406", "credential")


async def test_session_manager_prunes_removed_gateway_resources():
    """Sessions and locks are bounded by the gateway inventory."""
    manager = OaseLocalSessionManager()
    session = AsyncMock()
    manager._sessions["removed-gateway"] = session
    manager._locks["removed-gateway"] = asyncio.Lock()

    await manager.async_prune(set())

    session.async_close.assert_awaited_once()
    assert manager._sessions == {}
    assert manager._locks == {}


def test_local_credential_is_extracted_from_the_matching_gateway_only():
    """Credential lookup does not accidentally select another gateway's value."""
    inventory = {
        "gateways": [
            {"id": "other", "customAttributesJson": '[{"Id": 101, "Value": {"Value": "other"}}]'},
            {
                "id": "target",
                "customAttributesJson": '[{"Id": 1, "Value": {"Value": null}}, '
                '{"Id": 101, "Value": {"Value": "credential"}}]',
            },
        ]
    }

    assert local_credential_from_inventory(inventory, "target") == "credential"


@pytest.mark.parametrize(
    ("inventory", "gateway_id"),
    [
        ({}, "target"),
        ({"gateways": []}, "target"),
        ({"gateways": [{"id": "target", "customAttributesJson": "not-json"}]}, "target"),
        ({"gateways": [{"id": "target", "customAttributesJson": "[]"}]}, "target"),
    ],
)
def test_local_credential_rejects_absent_or_invalid_inventory_data(inventory, gateway_id):
    """Absent credentials produce an actionable error without exposing data."""
    with pytest.raises(OaseLocalCredentialError):
        local_credential_from_inventory(inventory, gateway_id)


def test_local_credentials_skips_gateways_without_usable_credentials():
    """A missing value for one gateway must not discard another gateway's cache."""
    inventory = {
        "gateways": [
            {"id": "missing", "customAttributesJson": "[]"},
            {"id": "usable", "customAttributesJson": '[{"Id": 101, "Value": {"Value": "value"}}]'},
        ]
    }

    assert local_credentials_from_inventory(inventory) == {"usable": "value"}


def _free_tcp_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _client_tls_context() -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


async def _wait_for_routed_connection(port: int) -> None:
    listener = _local_network().listeners[port]
    for _ in range(200):
        waiter = listener._waiters.get("127.0.0.1")
        if waiter is not None and waiter.done():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("controller connection was not routed")


async def test_reverse_tls_sessions_share_one_listener(socket_enabled):
    """Every controller connects back to one port, so the listener is shared."""
    port = _free_tcp_port()
    listener = local._ReverseTlsListener(port)

    await listener.async_acquire()
    server = listener._server
    await listener.async_acquire()
    assert listener._server is server

    listener.release()
    assert listener._server is server
    listener.release()
    assert listener._server is None
    await asyncio.wait_for(server.wait_closed(), 1)


async def test_failed_tls_open_drops_an_unclaimed_controller_connection(socket_enabled):
    """A connection that arrives for a failed attempt must not hang the listener."""
    port = _free_tcp_port()
    clients = []

    async def _connect_then_fail(_controller_ip, listener_port, _timeout):
        clients.append(
            await asyncio.open_connection("127.0.0.1", listener_port, ssl=_client_tls_context())
        )
        await _wait_for_routed_connection(listener_port)
        raise ValueError("controller rejected TCP connection request")

    with patch.object(local, "_async_request_reverse_connection", _connect_then_fail):
        listener_task = asyncio.ensure_future(
            async_open_tls_session("127.0.0.1", listener_port=port, timeout=2)
        )
        with pytest.raises(OaseLocalTransportError):
            await asyncio.wait_for(listener_task, 5)

    listener = _local_network().listeners[port]
    assert listener._server is None
    assert listener._waiters == {}
    reader, writer = clients[0]
    assert await asyncio.wait_for(reader.read(), 2) == b""
    writer.close()


async def test_opened_tls_session_releases_the_listener_when_closed(socket_enabled):
    """A successful session keeps the listener only for as long as it is open."""
    port = _free_tcp_port()
    clients = []

    async def _connect(_controller_ip, listener_port, _timeout):
        clients.append(
            await asyncio.open_connection("127.0.0.1", listener_port, ssl=_client_tls_context())
        )

    with patch.object(local, "_async_request_reverse_connection", _connect):
        session = await asyncio.wait_for(
            async_open_tls_session("127.0.0.1", listener_port=port, timeout=2), 5
        )

    listener = _local_network().listeners[port]
    assert listener._server is not None
    clients[0][1].close()
    await session.async_close()
    assert listener._server is None


def _patched_connect(manager: OaseLocalSessionManager, *sessions):
    """Make the manager's connects hand out the given sessions in order."""
    connect = AsyncMock(side_effect=list(sessions))

    async def _connect(gateway_id, *_args):
        session = await connect(gateway_id)
        manager._sessions[gateway_id] = session
        return session

    manager._async_connect = _connect
    return connect


async def test_cancelled_authentication_closes_the_new_session():
    """Cancelling a connect must not leak the TLS session and listener lease."""
    session = AsyncMock()
    session.async_authenticate.side_effect = asyncio.CancelledError
    found = SimpleNamespace(host="192.0.2.1", info=SimpleNamespace(serial_number="serial"))
    manager = OaseLocalSessionManager()

    with (
        patch.object(local, "async_discover_controllers", AsyncMock(return_value=[found])),
        patch.object(local, "async_open_tls_session", AsyncMock(return_value=session)),
        pytest.raises(asyncio.CancelledError),
    ):
        await manager._async_connect("gateway-id", "serial", "credential")

    session.async_close.assert_awaited_once_with(abort=True)
    assert manager._sessions == {}


async def test_cancelled_request_drops_the_out_of_sync_session():
    """A reply left unread by a cancelled request must not reach the next caller."""
    session = AsyncMock()
    manager = OaseLocalSessionManager()
    manager._sessions["gateway-id"] = session

    with (
        patch.object(local, "async_read_egc_devices", AsyncMock(side_effect=asyncio.CancelledError)),
        pytest.raises(asyncio.CancelledError),
    ):
        await manager.async_read_egc_devices("gateway-id", "serial", "credential")

    session.async_close.assert_awaited_once_with(abort=True)
    assert manager._sessions == {}


async def test_stale_session_is_replaced_once():
    """A broken persistent session is reconnected and the request retried."""
    stale = AsyncMock()
    fresh = AsyncMock()
    manager = OaseLocalSessionManager()
    manager._sessions["gateway-id"] = stale
    connect = _patched_connect(manager, fresh)
    read = AsyncMock(side_effect=[TimeoutError, ()])

    with patch.object(local, "async_read_egc_devices", read):
        assert await manager.async_read_egc_devices("gateway-id", "serial", "credential") == ()

    connect.assert_awaited_once()
    stale.async_close.assert_awaited_once_with(abort=True)
    assert manager._sessions == {"gateway-id": fresh}


async def test_failure_on_a_fresh_session_is_reported_without_reconnecting():
    """A new session that fails immediately is not hammered with reconnects."""
    fresh = AsyncMock()
    manager = OaseLocalSessionManager()
    connect = _patched_connect(manager, fresh)

    with (
        patch.object(local, "async_read_egc_devices", AsyncMock(side_effect=OSError)),
        pytest.raises(OaseLocalTransportError),
    ):
        await manager.async_read_egc_devices("gateway-id", "serial", "credential")

    connect.assert_awaited_once()
    assert manager._sessions == {}


async def test_discovery_socket_errors_are_transport_errors_and_close_the_socket():
    """A busy UDP port neither escapes as OSError nor leaks the socket."""
    udp_socket = Mock()
    udp_socket.bind.side_effect = OSError("address in use")

    with (
        patch.object(local.socket, "socket", return_value=udp_socket),
        pytest.raises(OaseLocalTransportError),
    ):
        await async_discover_controllers()

    udp_socket.close.assert_called_once()


async def test_mismatched_rdm_get_reply_is_rejected():
    """A reply for another parameter means the stream is out of sync."""
    discovery = onet.OnetPacket(onet.reply_type(rdm.EGC_DISCOVERY), b"")
    wrong_pid = rdm.build_frame(
        rdm.CONTROLLER_UID,
        rdm.CommandClass.GET_COMMAND_RESPONSE,
        rdm.Pid.PUMP_POWER,
        b"\x80",
        source=rdm.Uid.for_device(12345),
        port_id=rdm.ResponseType.ACK,
    )
    session = OaseLocalTlsSession(AsyncMock(), AsyncMock(), AsyncMock())
    session.async_request = AsyncMock(
        side_effect=[discovery, onet.OnetPacket(onet.reply_type(rdm.RDM_REQUEST), wrong_pid)]
    )

    with (
        patch.object(rdm, "parse_discovery_reply", return_value=[
            SimpleNamespace(article_number=1, device_number=12345, manufacturer_id=1, subdevice_count=0)
        ]),
        pytest.raises(OaseLocalTransportError, match="mismatched"),
    ):
        await async_read_egc_devices(session)


async def test_oversized_reply_header_is_rejected():
    """A corrupt length field cannot make the reader buffer gigabytes."""
    reader = AsyncMock()
    reader.readexactly.return_value = onet.encode_packet(0x10FF)[:4] + b"\xff\xff\xff\x7f" + bytes(8)
    session = OaseLocalTlsSession(reader, Mock(drain=AsyncMock()), AsyncMock())

    with pytest.raises(OaseLocalTransportError, match="maximum"):
        await session.async_request(onet.PacketType.DEVICE_INFO)

    reader.readexactly.assert_awaited_once_with(16)


async def test_session_close_aborts_a_controller_that_never_completes_tls_shutdown():
    """A vanished controller cannot hold the gateway lock for the TLS timeout."""
    never = asyncio.get_running_loop().create_future()
    writer = MagicMock()
    writer.wait_closed = Mock(return_value=never)
    listener = Mock()
    session = OaseLocalTlsSession(AsyncMock(), writer, listener)

    with patch.object(local, "_CLOSE_TIMEOUT", 0.01):
        await session.async_close()
        await session.async_close()

    writer.transport.abort.assert_called_once()
    listener.release.assert_called_once()


async def test_failed_connects_back_off_until_the_retry_time():
    """An unreachable controller is not rediscovered on every status refresh."""
    manager = OaseLocalSessionManager()
    connect = AsyncMock(side_effect=OaseLocalTransportError("not found"))
    manager._async_connect = connect

    for _ in range(2):
        with pytest.raises(OaseLocalTransportError):
            await manager.async_read_egc_devices("gateway-id", "serial", "credential")
    connect.assert_awaited_once()

    manager._backoff["gateway-id"] = local._ReconnectBackoff(0.0, 1, hash("credential"))
    with pytest.raises(OaseLocalTransportError):
        await manager.async_read_egc_devices("gateway-id", "serial", "credential")
    assert connect.await_count == 2
    assert manager._backoff["gateway-id"].failures == 2


async def test_rejected_credential_backs_off_longest_but_a_new_one_is_tried_at_once():
    """A wrong password is not retried every 5 s, but a refreshed one is."""
    manager = OaseLocalSessionManager()
    session = AsyncMock()
    connect = AsyncMock(side_effect=[OaseLocalAuthenticationError("rejected"), session])
    manager._async_connect = connect
    loop_time = asyncio.get_running_loop().time()

    with pytest.raises(OaseLocalAuthenticationError):
        await manager.async_read_egc_devices("gateway-id", "serial", "old")
    assert manager._backoff["gateway-id"].retry_at >= loop_time + local._RECONNECT_BACKOFF_MAX

    with patch.object(local, "async_read_egc_devices", AsyncMock(return_value=())):
        assert await manager.async_read_egc_devices("gateway-id", "serial", "new") == ()
    assert "gateway-id" not in manager._backoff


async def test_discovery_ends_once_the_wanted_controller_answered():
    """A known controller does not wait for the whole discovery window."""
    payload = bytearray(324)
    payload[2:7] = b"OASE\0"
    payload[34:47] = b"606300063406\0"
    reply = onet.encode_packet(onet.reply_type(onet.PacketType.DEVICE_INFO), bytes(payload))
    loop = asyncio.get_running_loop()

    async def _endpoint(factory, sock):
        protocol = factory()
        protocol.datagram_received(reply, ("10.0.0.2", LOCAL_UDP_PORT))
        return Mock(), protocol

    with (
        patch.object(local.socket, "socket", return_value=Mock()),
        patch.object(loop, "create_datagram_endpoint", _endpoint),
    ):
        results = await asyncio.wait_for(
            async_discover_controllers(timeout=30, serial_number="606300063406"), 1
        )

    assert results == [LocalDiscoveryResult("10.0.0.2", results[0].info)]
