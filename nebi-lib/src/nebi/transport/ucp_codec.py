"""UCP v1 wire format (port 9007): constants and the segment / SACK codec. Pure functions, no I/O.

Wire format:
  - Handshake: `b"ucp:sync"` -> `b"ucp:ack"`, teardown `b"ucp:leav"`
  - 12-byte LE segment header: `<HHHH I` (`flags_ver`, `una`, `sn`, `pkt_len`, `ts_us`)
  - Low byte of `flags_ver` == `0x51` (magic 5, version 1)
  - Optional 11-byte SACK trailer (`<B H II`, magic `0x05`) when `flags_ver & 0x1000`
  - Channel 0: `"UCP:"` RTT heartbeat, 8-byte RcJoystick word, unreliable Zenoh Push
  - Channel 1: Fragmented Annex-B H.265 (HEVC) Access Units (`MORE_FRAGMENTS = 0x0200`)
  - Channel 2: Opus audio frames
  - Channel 3: Reliable Zenoh-over-UCP stream (`0xA4..0xA7`)
"""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass

UCP_MAGIC_VER = 0x51
UCP_FLAG_RELIABLE = 0x0100
UCP_FLAG_MORE_FRAGS = 0x0200
UCP_FLAG_HAS_SACK = 0x1000

UCP_CH_CONTROL = 0
UCP_CH_VIDEO = 1
UCP_CH_AUDIO = 2
UCP_CH_ZENOH = 3

UCP_HDR_STRUCT = struct.Struct("<HHHHI")  # 12 bytes
UCP_SACK_STRUCT = struct.Struct("<BHII")  # 11 bytes
UCP_SACK_MAGIC = 0x05

UCP_MSS = 1460
UCP_MAX_PAYLOAD = UCP_MSS - UCP_HDR_STRUCT.size - UCP_SACK_STRUCT.size  # 1437 bytes safe payload
UCP_MAX_VIDEO_AU_BYTES = 2 * 1024 * 1024

UCP_SYNC = b"ucp:sync"
UCP_ACK = b"ucp:ack"
UCP_LEAV = b"ucp:leav"
UCP_HB_PREFIX = b"UCP:"

# The robot parses EVERY unreliable 8-byte Channel-0 payload as an RcJoystick word (bits 0..62) plus the
# `no_liveview_flag` (bit 63). Nothing else may ever be sent unreliably with exactly this length.
UCP_JOYSTICK_PAYLOAD_LEN = 8

# Zenoh-over-UCP tunnel frame types (first payload byte on Channel 3, or Channel 0 for unreliable Push).
Z_UCP_OPEN = 0xA4
Z_UCP_ACK = 0xA5
Z_UCP_CLOSE = 0xA6
Z_UCP_DATA = 0xA7
Z_UCP_FRAME_TYPES = frozenset({Z_UCP_OPEN, Z_UCP_ACK, Z_UCP_CLOSE, Z_UCP_DATA})

HEVC_KEYFRAME_NALS = frozenset({16, 17, 18, 19, 20, 21, 32, 33})  # BLA/IDR/CRA IRAP + VPS/SPS


def monotonic_us() -> int:
    """Monotonic clock in microseconds, truncated to the u32 `ts_us` header field."""
    return int(time.monotonic() * 1_000_000) & 0xFFFFFFFF


def seq_diff(a: int, b: int) -> int:
    """Signed 16-bit sequence difference `(a - b)` in `[-32768, 32767]`."""
    d = (a - b) & 0xFFFF
    return d - 0x10000 if d >= 0x8000 else d


def is_hevc_keyframe(annexb_bytes: bytes) -> bool:
    """Scan Annex-B start codes (`00 00 01` or `00 00 00 01`) for HEVC keyframe NAL units."""
    idx = 0
    n = len(annexb_bytes)
    while idx < n - 4:
        pos = annexb_bytes.find(b"\x00\x00\x01", idx)
        if pos < 0 or pos + 3 >= n:
            break
        nal_type = (annexb_bytes[pos + 3] >> 1) & 0x3F
        if nal_type in HEVC_KEYFRAME_NALS:
            return True
        idx = pos + 4
    return False


@dataclass(slots=True)
class SackTrailer:
    rcv_nxt: int
    bitmap_lo: int = 0
    bitmap_hi: int = 0

    def encode(self) -> bytes:
        return UCP_SACK_STRUCT.pack(
            UCP_SACK_MAGIC,
            self.rcv_nxt & 0xFFFF,
            self.bitmap_lo & 0xFFFFFFFF,
            self.bitmap_hi & 0xFFFFFFFF,
        )

    @classmethod
    def decode(cls, buf: bytes) -> SackTrailer | None:
        if len(buf) != UCP_SACK_STRUCT.size:
            return None
        magic, rcv_nxt, lo, hi = UCP_SACK_STRUCT.unpack(buf)
        if magic != UCP_SACK_MAGIC:
            return None
        return cls(rcv_nxt=rcv_nxt, bitmap_lo=lo, bitmap_hi=hi)


@dataclass(slots=True)
class UcpSegment:
    channel: int
    reliable: bool
    more_fragments: bool
    una: int
    sn: int
    ts_us: int
    payload: bytes
    sack: SackTrailer | None = None

    def encode(self) -> bytes:
        flags_ver = (
            UCP_MAGIC_VER
            | (UCP_FLAG_RELIABLE if self.reliable else 0)
            | (UCP_FLAG_MORE_FRAGS if self.more_fragments else 0)
            | ((self.channel & 0x3) << 10)
            | (UCP_FLAG_HAS_SACK if self.sack is not None else 0)
        )
        sack_bytes = self.sack.encode() if self.sack is not None else b""
        pkt_len = UCP_HDR_STRUCT.size + len(self.payload) + len(sack_bytes)
        hdr = UCP_HDR_STRUCT.pack(
            flags_ver & 0xFFFF,
            self.una & 0xFFFF,
            self.sn & 0xFFFF,
            pkt_len & 0xFFFF,
            self.ts_us & 0xFFFFFFFF,
        )
        return hdr + self.payload + sack_bytes


def decode_ucp_datagram(datagram: bytes) -> list[UcpSegment]:
    """Parse one or more concatenated UCP v1 segments from a UDP datagram (stops at the first malformed one)."""
    segments: list[UcpSegment] = []
    offset = 0
    total = len(datagram)
    while offset + UCP_HDR_STRUCT.size <= total:
        flags_ver, una, sn, pkt_len, ts_us = UCP_HDR_STRUCT.unpack_from(datagram, offset)
        if (flags_ver & 0xFF) != UCP_MAGIC_VER:
            break
        if pkt_len < UCP_HDR_STRUCT.size or offset + pkt_len > total:
            break
        sack: SackTrailer | None = None
        if flags_ver & UCP_FLAG_HAS_SACK:
            if pkt_len < UCP_HDR_STRUCT.size + UCP_SACK_STRUCT.size:
                break
            payload_end = offset + pkt_len - UCP_SACK_STRUCT.size
            sack = SackTrailer.decode(datagram[payload_end : offset + pkt_len])
        else:
            payload_end = offset + pkt_len

        segments.append(
            UcpSegment(
                channel=(flags_ver >> 10) & 0x3,
                reliable=bool(flags_ver & UCP_FLAG_RELIABLE),
                more_fragments=bool(flags_ver & UCP_FLAG_MORE_FRAGS),
                una=una,
                sn=sn,
                ts_us=ts_us,
                payload=datagram[offset + UCP_HDR_STRUCT.size : payload_end],
                sack=sack,
            )
        )
        offset += pkt_len
    return segments


def split_payload(data: bytes, chunk: int = UCP_MAX_PAYLOAD) -> list[bytes]:
    """Split `data` into UCP-sized fragments; an empty payload yields one empty fragment."""
    return [data[i : i + chunk] for i in range(0, max(1, len(data)), chunk)]
