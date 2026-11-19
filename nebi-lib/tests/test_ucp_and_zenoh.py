"""Unit & async loopback tests for UCP v1 UDP transport and Zenoh-over-UCP tunnel (`0xA4..0xA7`) framing."""

from __future__ import annotations

import asyncio
import os

import pytest

from nebi.transport.ucp import (
    UCP_CH_VIDEO,
    UCP_CH_ZENOH,
    SackTrailer,
    UcpClient,
    UcpSegment,
    UcpServer,
    decode_ucp_datagram,
    is_hevc_keyframe,
    seq_diff,
)
from nebi.transport.zenoh_ucp import Z_UCP_ACK, Z_UCP_DATA, Z_UCP_OPEN, ZenohUcpFrame


def test_ucp_segment_and_sack_wire_format() -> None:
    sack = SackTrailer(rcv_nxt=42, bitmap_lo=0xDEADBEEF, bitmap_hi=0x01020304)
    seg = UcpSegment(
        channel=UCP_CH_ZENOH,
        reliable=True,
        more_fragments=False,
        una=10,
        sn=11,
        ts_us=123456,
        payload=b"hello-nebi",
        sack=sack,
    )
    raw = seg.encode()
    assert len(raw) == 12 + len(b"hello-nebi") + 11
    assert raw[0] == 0x51  # UCP v1 magic_ver low byte

    parsed = decode_ucp_datagram(raw)
    assert len(parsed) == 1
    p = parsed[0]
    assert p.channel == UCP_CH_ZENOH
    assert p.reliable is True
    assert p.more_fragments is False
    assert p.una == 10
    assert p.sn == 11
    assert p.ts_us == 123456
    assert p.payload == b"hello-nebi"
    assert p.sack is not None
    assert p.sack.rcv_nxt == 42
    assert p.sack.bitmap_lo == 0xDEADBEEF
    assert p.sack.bitmap_hi == 0x01020304


def test_seq_diff_and_hevc_keyframe_scanner() -> None:
    assert seq_diff(5, 3) == 2
    assert seq_diff(2, 65534) == 4
    assert seq_diff(65530, 1) == -7

    # VPS NAL type 32 -> (32 << 1) = 0x40
    vps_au = b"\x00\x00\x00\x01\x40\x01\x0c\x01\xff"
    # Trail_R non-IDR slice NAL type 1 -> (1 << 1) = 0x02
    p_au = b"\x00\x00\x00\x01\x02\x01\xaa\xbb"
    assert is_hevc_keyframe(vps_au) is True
    assert is_hevc_keyframe(p_au) is False


def test_zenoh_over_ucp_frame_codec() -> None:
    f_open = ZenohUcpFrame(Z_UCP_OPEN, 0x12345678)
    raw_open = f_open.encode()
    assert len(raw_open) == 5 and raw_open[0] == 0xA4
    dec_open = ZenohUcpFrame.decode(raw_open)
    assert dec_open is not None and dec_open.frame_type == Z_UCP_OPEN and dec_open.attempt_id == 0x12345678

    f_ack = ZenohUcpFrame(Z_UCP_ACK, 0x12345678)
    assert ZenohUcpFrame.decode(f_ack.encode()).frame_type == Z_UCP_ACK  # type: ignore[union-attr]

    f_data = ZenohUcpFrame(Z_UCP_DATA, 0x5678, b"\x04\x00test")
    raw_data = f_data.encode()
    assert raw_data[:3] == b"\xa7\x56\x78"
    dec_data = ZenohUcpFrame.decode(raw_data)
    assert dec_data is not None and dec_data.attempt_id == 0x5678 and dec_data.payload == b"\x04\x00test"


@pytest.mark.asyncio
async def test_ucp_client_server_fragmented_video_and_audio() -> None:
    rx_video: list[tuple[bytes, bool]] = []
    rx_server_audio: list[bytes] = []
    video_evt = asyncio.Event()
    audio_evt = asyncio.Event()

    def on_srv_audio(opus: bytes) -> None:
        rx_server_audio.append(opus)
        audio_evt.set()

    def on_cli_video(au: bytes, is_kf: bool, _ts: int) -> None:
        rx_video.append((au, is_kf))
        video_evt.set()

    server = UcpServer(host="127.0.0.1", port=0, on_audio_frame=on_srv_audio)
    await server.start()
    client = UcpClient(host="127.0.0.1", port=server.bound_port, on_video_frame=on_cli_video)
    try:
        await client.connect(timeout=2.0)
        assert client.connected
        assert server.session is not None

        # Send 24 KB synthetic keyframe H.265 AU (splits into ~17 UCP Channel 1 fragments)
        big_au = b"\x00\x00\x00\x01\x40\x01" + os.urandom(24_000)
        server.session.send_video_frame(big_au)
        await asyncio.wait_for(video_evt.wait(), timeout=2.0)
        assert len(rx_video) == 1
        assert rx_video[0][0] == big_au
        assert rx_video[0][1] is True

        # Send Opus audio frame client -> server over Channel 2
        client.session.send_audio_frame(b"opus-frame-payload")
        await asyncio.wait_for(audio_evt.wait(), timeout=2.0)
        assert rx_server_audio == [b"opus-frame-payload"]
    finally:
        await client.close()
        await server.stop()


def test_ucp_heartbeat_and_unreliable_zenoh_satisfy_real_robot_wire_gates() -> None:
    """Verify 31-byte Channel 0 + 0x05 SACK heartbeat/ACK (>23B gate) and unreliable 0xA7 + SACK."""
    from nebi.transport.ucp import UCP_CH_CONTROL, UCP_SACK_MAGIC, UcpEndpointSession

    emitted: list[bytes] = []
    sess = UcpEndpointSession(send_datagram=emitted.append)
    sess.rcv_nxt = 7

    # 1. send_ack_only() / send_heartbeat_probe() MUST emit >= 24B (exact 31B) with SACK magic 0x05
    sess.send_ack_only()
    assert len(emitted) == 1
    hb_raw = emitted[-1]
    assert len(hb_raw) == 31  # 12B hdr + 8B u64 LE + 11B SACK (> 23B ucp_input gate!)
    assert hb_raw[-11] == UCP_SACK_MAGIC == 0x05
    parsed_hb = decode_ucp_datagram(hb_raw)
    assert len(parsed_hb) == 1
    assert parsed_hb[0].channel == UCP_CH_CONTROL
    assert parsed_hb[0].reliable is False
    assert parsed_hb[0].una == 7
    assert parsed_hb[0].sack is not None and parsed_hb[0].sack.rcv_nxt == 7
    assert len(parsed_hb[0].payload) == 8
    assert (int.from_bytes(parsed_hb[0].payload, "little") >> 63) == 0  # no_liveview_flag == 0

    # 2. Disabling liveview sets bit 63 (`no_liveview_flag == 1`)
    sess.send_heartbeat_probe(liveview_enabled=False)
    parsed_off = decode_ucp_datagram(emitted[-1])[0]
    assert (int.from_bytes(parsed_off.payload, "little") >> 63) == 1

    # 3. Unreliable Zenoh 0xA7 Push MUST carry Channel 0 + 0x05 SACK trailer (`flags_ver = 0x1051`)
    zdata = ZenohUcpFrame(Z_UCP_DATA, 0x0042, b"\x04\x00\x1d\x01\x02\x03").encode()
    sess.send_zenoh_frame(zdata, reliable=False)
    unrel_raw = emitted[-1]
    assert len(unrel_raw) > 23
    assert unrel_raw[-11] == 0x05
    parsed_z = decode_ucp_datagram(unrel_raw)[0]
    assert parsed_z.channel == UCP_CH_CONTROL
    assert parsed_z.reliable is False
    assert parsed_z.sack is not None
    assert parsed_z.payload == zdata


def test_videotrans_proto_normalization() -> None:
    """Verify the robot bridge's field-presence & fixed-slice invariants are filled in."""
    from nebi.catalog.registry import CATALOG
    from nebi.transport.zenoh_ucp import _normalize_proto_for_videotrans

    # 1. NmWifiConnectReqData requires exact 16B uuid + 4B resv + explicit optional scalars
    conn_cls = CATALOG.get_proto_class("/cmd/nm/wifi_connect_req")
    assert conn_cls is not None
    conn = conn_cls()
    conn.header.seq_num = 1
    conn.data.ssid_len = 7
    conn.data.ssid = b"TestNet"
    conn.data.psk_len = 15
    conn.data.psk = b"testpassword123"
    conn.data.security = 2
    conn.data.timeout_sec = 25
    _normalize_proto_for_videotrans(conn.data)
    parsed_conn = conn_cls.FromString(conn.SerializeToString())
    assert parsed_conn.HasField("header") and parsed_conn.header.seq_num == 1
    assert parsed_conn.HasField("data")
    assert len(parsed_conn.data.uuid) == 16
    assert len(parsed_conn.data.resv) == 4
    assert parsed_conn.data.HasField("ssid_len")
    assert parsed_conn.data.HasField("psk_len")
    assert parsed_conn.data.HasField("security")
    assert parsed_conn.data.HasField("timeout_sec")

    # 2. NmWifiProvisionStartReqData requires 7B resv, 16B uuid, Some(0) mqtt_endpoint_len, 7B mqtt_resv
    start_cls = CATALOG.get_proto_class("/cmd/nm/wifi_provision_start_req")
    assert start_cls is not None
    start = start_cls()
    start.data.trigger_source = 1
    _normalize_proto_for_videotrans(start.data)
    parsed_start = start_cls.FromString(start.SerializeToString())
    assert len(parsed_start.data.resv) == 7
    assert len(parsed_start.data.uuid) == 16
    assert parsed_start.data.HasField("mqtt_endpoint_len") and parsed_start.data.mqtt_endpoint_len == 0
    assert len(parsed_start.data.mqtt_resv) == 7

    # 3. Zero-field request data (`RobotSnReq`) still marks `msg.data` present
    sn_cls = CATALOG.get_proto_class("/cmd/robot/sn_req")
    assert sn_cls is not None
    sn = sn_cls()
    sn.header.seq_num = 3
    sn.data.SetInParent()
    _normalize_proto_for_videotrans(sn.data)
    parsed_sn = sn_cls.FromString(sn.SerializeToString())
    assert parsed_sn.HasField("data")

    # 4. Fixed-length `repeated` arrays (`LedCtrlOnewayData.rgbw_data == 14`)
    led_cls = CATALOG.get_proto_class("/cmd/led/ctrl_oneway")
    assert led_cls is not None
    led = led_cls()
    led.data.seq = 9
    led.data.rgbw_data.append(0x00D2FF00)
    _normalize_proto_for_videotrans(led.data)
    parsed_led = led_cls.FromString(led.SerializeToString())
    assert len(parsed_led.data.rgbw_data) == 14
    assert parsed_led.data.rgbw_data[0] == 0x00D2FF00


def test_ucp_reliable_video_and_audio_reassembly() -> None:
    """Verify the real robot's reliable Channel 1 (`0x0101`) video & Channel 2 (`0x0102`) audio delivery."""
    from nebi.transport.ucp import UCP_CH_AUDIO, UcpEndpointSession

    tx_pkts: list[bytes] = []
    rx_video: list[tuple[bytes, bool, int]] = []
    rx_audio: list[bytes] = []

    sender = UcpEndpointSession(send_datagram=tx_pkts.append)
    receiver = UcpEndpointSession(
        send_datagram=lambda _b: None,
        on_video_frame=lambda au, kf, ts: rx_video.append((au, kf, ts)),
        on_audio_frame=rx_audio.append,
    )

    # Multi-fragment reliable H.265 keyframe (`UCP_CH_VIDEO == 1`, `reliable=True`)
    hevc_au = b"\x00\x00\x00\x01\x26\x01" + os.urandom(6000)
    sender.send_reliable(UCP_CH_VIDEO, hevc_au)
    assert len(tx_pkts) >= 4
    for dgram in tx_pkts:
        receiver.handle_datagram(dgram)

    assert receiver.video_frames_rx == 1
    assert len(rx_video) == 1
    assert rx_video[0][0] == hevc_au
    assert rx_video[0][1] is True

    # Reliable Opus audio (`UCP_CH_AUDIO == 2`, `reliable=True`)
    tx_pkts.clear()
    sender.send_reliable(UCP_CH_AUDIO, b"opus-reliable-chunk")
    for dgram in tx_pkts:
        receiver.handle_datagram(dgram)

    assert receiver.audio_frames_rx == 1
    assert rx_audio == [b"opus-reliable-chunk"]
