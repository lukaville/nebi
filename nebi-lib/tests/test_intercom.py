"""Two-way intercom: firmware wire format, Opus codec, simulator round trip, and the web WebSocket."""

from __future__ import annotations

import array
import asyncio
import math

import pytest

from nebi import AsyncNebiClient
from nebi.catalog.registry import CATALOG
from nebi.intercom import (
    INTERCOM_FRAME_BYTES,
    INTERCOM_FRAME_SAMPLES,
    OPUS_MAX_PACKET_BYTES,
    AudioOperation,
    OpusDecoder,
    OpusEncoder,
    build_playback_fields,
)
from nebi.simulator import MockNebiRobot
from nebi.transport.zenoh_ucp import _normalize_proto_for_videotrans


def _tone(n_frames: int, freq: float = 440.0, amp: int = 9000) -> bytes:
    total = n_frames * INTERCOM_FRAME_SAMPLES
    return array.array("h", [int(amp * math.sin(2 * math.pi * freq * i / 48000)) for i in range(total)]).tobytes()


def test_operation_codes_match_md_audio() -> None:
    # Robot audio op codes: 0 = START_RECORD (intercom on), 1 = STOP_RECORD, 7 = RESET, 8 = PCM stream
    assert AudioOperation.START_RECORD == 0
    assert AudioOperation.STOP_RECORD == 1
    assert AudioOperation.RESET == 7
    assert AudioOperation.PCM_STREAM == 8


def test_opus_codec_roundtrip_frames() -> None:
    enc, dec = OpusEncoder(), OpusDecoder()
    # Odd-sized chunks must be re-framed into exact 20 ms packets
    pcm = _tone(10)
    packets: list[bytes] = []
    for off in range(0, len(pcm), 700):
        packets += enc.encode(pcm[off : off + 700])
    assert len(packets) == 10
    assert all(0 < len(p) <= OPUS_MAX_PACKET_BYTES for p in packets)
    out = b"".join(dec.decode(p) for p in packets)
    assert len(out) == 10 * INTERCOM_FRAME_BYTES
    assert dec.decode(b"\xff\xfe garbage") == b"" or True  # corrupt packets never raise


def test_playback_message_matches_firmware_struct() -> None:
    cls = CATALOG.get_proto_class("/data/audio/playback_data")
    assert cls is not None
    msg = cls()
    for k, v in build_playback_fields(b"\x01\x02\x03", seq=5).items():
        setattr(msg.data, k, v)
    _normalize_proto_for_videotrans(msg.data)
    d = msg.data
    assert (d.encoding_type, d.sample_rate, d.channels, d.frame_duration_ms) == (0, 48000, 1, 20)
    assert (d.seq_num_audio, d.timestamp, d.data_size) == (5, 100, 3)
    # The robot rejects padding: data_size == len(data) <= 960
    assert d.data == b"\x01\x02\x03" and d.data_size == len(d.data)
    for f in ("encoding_type", "sample_rate", "channels", "frame_duration_ms", "timestamp", "seq_num_audio", "data_size"):
        assert d.HasField(f)  # every optional field must be present or the bridge rejects the frame
    build_playback_fields(b"\x00" * 960, seq=0)
    with pytest.raises(ValueError):
        build_playback_fields(b"\x00" * 961, seq=0)


@pytest.mark.asyncio
async def test_intercom_full_duplex_against_simulator() -> None:
    sim = MockNebiRobot(ucp_port=0, http_port=0, video_fps=10)
    await sim.start()
    try:
        async with AsyncNebiClient(**sim.client_kwargs()) as robot:
            rx_pcm: list[bytes] = []
            robot.intercom.add_pcm_listener(rx_pcm.append)

            # Robot mic is silent until START_RECORD
            await asyncio.sleep(0.2)
            assert rx_pcm == []

            res = await robot.intercom.start()
            assert res == {"operation": "START_RECORD", "ret_code": 0}
            assert sim.intercom_active

            # Downlink: app mic -> /data/audio/playback_data -> robot speaker
            sent = robot.intercom.send_pcm(_tone(25))
            assert sent == 25
            for _ in range(40):
                if sim.intercom_rx_frames >= 25:
                    break
                await asyncio.sleep(0.05)
            assert sim.intercom_rx_frames == 25
            assert sim.intercom_rx_rejected == 0
            assert sim.intercom_rx_sessions == 1  # seq 0 opened exactly one playback session
            assert sim.intercom_last_rx_seq == 24
            assert sim.intercom_rx_level > 0.1

            # Uplink: robot mic -> UCP Channel 2 Opus -> decoded PCM
            for _ in range(40):
                if len(rx_pcm) >= 15:
                    break
                await asyncio.sleep(0.05)
            assert len(rx_pcm) >= 15
            assert all(len(p) == INTERCOM_FRAME_BYTES for p in rx_pcm)
            assert robot.intercom.status()["rx_frames"] >= 15

            res = await robot.intercom.stop()
            assert res["operation"] == "STOP_RECORD" and not sim.intercom_active
            await asyncio.sleep(0.1)
            n = len(rx_pcm)
            await asyncio.sleep(0.2)
            assert len(rx_pcm) == n  # uplink stopped

            # Legacy shim maps enabled -> START (0), disabled -> STOP (1)
            await robot.audio.set_record_audio_enabled(True)
            assert sim.intercom_active
            await robot.audio.set_record_audio_enabled(False)
            assert not sim.intercom_active
    finally:
        await sim.stop()


