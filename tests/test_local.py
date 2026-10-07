"""Tests for the local O-Net transport primitives."""

from unittest.mock import AsyncMock

import pytest

from pyoase import onet

from custom_components.oase_control.local import (
    LOCAL_TCP_PORT,
    LOCAL_UDP_PORT,
    OaseLocalAuthenticationError,
    OaseLocalCredentialError,
    OaseLocalTlsSession,
    _create_server_tls_context,
    device_info_probe_packet,
    local_credential_from_inventory,
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
