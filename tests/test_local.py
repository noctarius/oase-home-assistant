"""Tests for the read-only local O-Net probe primitives."""

from pyoase import onet

from custom_components.oase_control.local import (
    LOCAL_TCP_PORT,
    LOCAL_UDP_PORT,
    device_info_probe_packet,
)


def test_device_info_probe_uses_the_shared_onet_codec():
    """The local probe stays wire-compatible with pyoase cloud relay frames."""
    packet = onet.parse_packet(device_info_probe_packet())

    assert packet.packet_type == onet.PacketType.DEVICE_INFO
    assert packet.payload == b""
    assert packet.version == onet.PROTOCOL_VERSION
    assert LOCAL_UDP_PORT == 5959
    assert LOCAL_TCP_PORT == 5999
