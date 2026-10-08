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
    _parse_discovery_result,
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
