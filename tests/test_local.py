"""Tests for the local O-Net transport primitives."""

import asyncio
import socket
import ssl
from unittest.mock import AsyncMock, patch

import pytest

from pyoase import onet, rdm

from custom_components.oase_control import local
from custom_components.oase_control.local import (
    LOCAL_TCP_PORT,
    LOCAL_UDP_PORT,
    OaseLocalAuthenticationError,
    OaseLocalCredentialError,
    OaseLocalSessionManager,
    OaseLocalTlsSession,
    OaseLocalTransportError,
    _create_server_tls_context,
    _local_network,
    _parse_discovery_result,
    async_open_tls_session,
    async_set_egc_parameter,
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
