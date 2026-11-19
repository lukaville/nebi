"""Async UCP v1 transport (UDP :9007): ARQ/fragmentation/heartbeat engine and the UDP client.

The wire codec lives in `nebi.transport.ucp_codec` (re-exported here for compatibility); the robot-side
`UcpServer` used by the simulator lives in `nebi.sim.ucp` (lazily re-exported here).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import struct
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from nebi.latch import apply_latch, latch_key_for_host
from nebi.network import create_bound_udp_socket
from nebi.transport.ucp_codec import (
    HEVC_KEYFRAME_NALS,
    UCP_ACK,
    UCP_CH_AUDIO,
    UCP_CH_CONTROL,
    UCP_CH_VIDEO,
    UCP_CH_ZENOH,
    UCP_FLAG_HAS_SACK,
    UCP_FLAG_MORE_FRAGS,
    UCP_FLAG_RELIABLE,
    UCP_HB_PREFIX,
    UCP_HDR_STRUCT,
    UCP_JOYSTICK_PAYLOAD_LEN,
    UCP_LEAV,
    UCP_MAGIC_VER,
    UCP_MAX_PAYLOAD,
    UCP_MAX_VIDEO_AU_BYTES,
    UCP_MSS,
    UCP_SACK_MAGIC,
    UCP_SACK_STRUCT,
    UCP_SYNC,
    Z_UCP_FRAME_TYPES,
    SackTrailer,
    UcpSegment,
    decode_ucp_datagram,
    is_hevc_keyframe,
    monotonic_us,
    seq_diff,
    split_payload,
)
from nebi.types import (
    JOY_B63_BIT,
    JOY_GEAR_MASK,
    JOY_GEAR_SHIFT,
    JOYSTICK_STICKS_MASK,
    JOYSTICK_STICKS_NEUTRAL,
    JOYSTICK_TRIGGER_MASK,
    JOYSTICK_WIRE_MASK,
    JOYSTICK_WIRE_NEUTRAL_WORD,
    SOFTAP_HOST,
)

if TYPE_CHECKING:
    from nebi.sim.ucp import UcpServer

__all__ = [
    "HEVC_KEYFRAME_NALS",
    "JOYSTICK_HOLD_AFTER_S",
    "JOYSTICK_WORD_MASK",
    "UCP_ACK",
    "UCP_CH_AUDIO",
    "UCP_CH_CONTROL",
    "UCP_CH_VIDEO",
    "UCP_CH_ZENOH",
    "UCP_FLAG_HAS_SACK",
    "UCP_FLAG_MORE_FRAGS",
    "UCP_FLAG_RELIABLE",
    "UCP_HB_PREFIX",
    "UCP_HDR_STRUCT",
    "UCP_JOYSTICK_NEUTRAL_WORD",
    "UCP_LEAV",
    "UCP_MAGIC_VER",
    "UCP_MAX_PAYLOAD",
    "UCP_MAX_RETRIES",
    "UCP_MAX_VIDEO_AU_BYTES",
    "UCP_MSS",
    "UCP_NO_LIVEVIEW_BIT",
    "UCP_SACK_MAGIC",
    "UCP_SACK_STRUCT",
    "UCP_SYNC",
    "UCP_TX_WINDOW_MAX",
    "BytesCallback",
    "JoystickWordCallback",
    "SackTrailer",
    "UcpClient",
    "UcpEndpointSession",
    "UcpSegment",
    "UcpServer",
    "VideoFrameCallback",
    "decode_ucp_datagram",
    "is_hevc_keyframe",
    "joystick_hold_word",
    "monotonic_us",
    "run_callback",
    "seq_diff",
]

logger = logging.getLogger(__name__)

VideoFrameCallback = Callable[[bytes, bool, int], Awaitable[None] | None]
BytesCallback = Callable[[bytes], Awaitable[None] | None]
JoystickWordCallback = Callable[[int], Awaitable[None] | None]

# --- RcJoystick word on the wire ------------------------------------------------------------------------------
# The bit layout is owned by `nebi.types` (the motion controller's RcJoystick); bit 63 is UCP's `no_liveview_flag`.
JOYSTICK_WORD_MASK = JOYSTICK_WIRE_MASK            # bits 0..62 carry the RcJoystick word
UCP_NO_LIVEVIEW_BIT = 1 << JOY_B63_BIT
_GEAR_SHIFT = JOY_GEAR_SHIFT
_GEAR_MASK = JOY_GEAR_MASK << JOY_GEAR_SHIFT
_STICKS_MASK = JOYSTICK_STICKS_MASK
_STICKS_NEUTRAL = JOYSTICK_STICKS_NEUTRAL
_TRIGGER_MASK = JOYSTICK_TRIGGER_MASK

# Neutral 63-bit RcJoystick word with wire gear 1 (Standard) = 0x0102_0FBE_FA1F_43E8 (`JoystickState().pack_wire()`).
# A former hard-coded value was packed on the wrong bit boundaries (the robot decoded it as lx 0.98, yaw 0.92,
# gimbal -0.90, roll -1, height 0); on 2026-11-17 a reconnect stood the robot up (bit 59 = 0) and those
# heartbeat frames drove it crouched and tilted for ~10 s.
UCP_JOYSTICK_NEUTRAL_WORD = JOYSTICK_WIRE_NEUTRAL_WORD

# Dead-man for the transport itself: the heartbeat repeats the last commanded word only while it is fresh. After
# JOYSTICK_HOLD_AFTER_S without a new `send_joystick_word`, heartbeats carry `joystick_hold_word(last)`: sticks
# centred and one-shot triggers cleared, latched/level bits (bit 59 stand/fold, bit 58 estop, gear, height, roll)
# kept, so expiry can never create a stand/fold or estop edge. A crashed/hung controller therefore stops the robot.
JOYSTICK_HOLD_AFTER_S = 0.25

# Reliable ARQ limits (the tunnel feeds this window with bounded backpressure, see `zenoh_tunnel`).
UCP_TX_WINDOW_MAX = 256
UCP_MAX_RETRIES = 40


def joystick_hold_word(word: int) -> int:
    """`word` with sticks neutral and one-shot trigger bits cleared (latched/level bits untouched)."""
    word &= JOYSTICK_WORD_MASK
    return ((word & ~_STICKS_MASK) | _STICKS_NEUTRAL) & ~_TRIGGER_MASK


def run_callback(cb: Callable[..., Any] | None, args: tuple[Any, ...], tasks: set[asyncio.Task[Any]]) -> None:
    """Invoke a user callback; a coroutine result is scheduled as a task tracked in `tasks` (failures logged).

    A buggy listener must never corrupt the reliable stream or kill the maintenance loop, so exceptions are
    logged and swallowed here.
    """
    if cb is None:
        return
    try:
        res = cb(*args)
    except Exception:
        logger.exception("UCP callback %r raised", cb)
        return
    if asyncio.iscoroutine(res):
        task = asyncio.ensure_future(res)
        tasks.add(task)

        def _done(t: asyncio.Task[Any]) -> None:
            tasks.discard(t)
            if not t.cancelled() and t.exception() is not None:
                logger.error("UCP async callback failed", exc_info=t.exception())

        task.add_done_callback(_done)



def __getattr__(name: str) -> Any:
    # `UcpServer` is simulator infrastructure (nebi.sim.ucp); resolved lazily so production imports never load it.
    if name == "UcpServer":
        from nebi.sim.ucp import UcpServer

        return UcpServer
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


class UcpEndpointSession:
    """Shared ARQ + fragmentation + heartbeat engine used by both UcpClient and UcpServer."""

    def __init__(
        self,
        send_datagram: Callable[[bytes], None],
        *,
        on_video_frame: VideoFrameCallback | None = None,
        on_audio_frame: BytesCallback | None = None,
        on_zenoh_frame: BytesCallback | None = None,
        on_joystick_frame: JoystickWordCallback | None = None,
        latch_key: str | None = None,
    ) -> None:
        self._send_datagram = send_datagram
        self.on_video_frame = on_video_frame
        self.on_audio_frame = on_audio_frame
        self.on_zenoh_frame = on_zenoh_frame
        self.on_joystick_frame = on_joystick_frame

        self.snd_nxt: int = 0
        self.rcv_nxt: int = 0
        self.video_sn: int = 0
        self.unrel_sn: int = 0
        self.liveview_enabled: bool = True
        self.latch_key = latch_key
        # start from the persisted bit-59 latch so the very first heartbeat cannot create a stand/fold edge
        self.joystick_word: int = apply_latch(UCP_JOYSTICK_NEUTRAL_WORD, latch_key)
        self._joystick_word_ts: float = 0.0  # monotonic time of the last fresh send_joystick_word (0 = never)
        self.link_dead: bool = False         # set when a reliable segment exceeded UCP_MAX_RETRIES
        self._tasks: set[asyncio.Task[Any]] = set()
        self._video_next_sn: int | None = None
        self._video_dropping: bool = False

        # Reliable TX window: sn -> (segment, last_sent_monotonic, retries)
        self._tx_window: OrderedDict[int, tuple[UcpSegment, float, int]] = OrderedDict()
        # Reliable out-of-order RX buffer: sn -> UcpSegment
        self._rx_ooo: dict[int, UcpSegment] = {}
        # Fragment reassemblers (the real robot sends Video=0x0101, Audio=0x0102, Zenoh=0x0103 reliably)
        self._rel_reasm: dict[int, bytearray] = {
            UCP_CH_CONTROL: bytearray(),
            UCP_CH_VIDEO: bytearray(),
            UCP_CH_AUDIO: bytearray(),
            UCP_CH_ZENOH: bytearray(),
        }
        self._video_buf = bytearray()

        # Diagnostics
        self.rtt_ms: float = 1.5
        self.last_rx_monotonic: float = time.monotonic()
        self.video_frames_rx: int = 0
        self.audio_frames_rx: int = 0
        self._video_bytes_window: int = 0
        self._video_frames_window: int = 0
        self._stats_ts: float = time.monotonic()
        self.video_fps: float = 0.0
        self.video_kbps: float = 0.0

    def prepare_new_session(self) -> None:
        """Called before every (re)connect: never resume a stale command, and re-read the persisted latch
        (another process may have folded/stood the robot since this session object last sent)."""
        self.joystick_word = apply_latch(joystick_hold_word(self.joystick_word), self.latch_key)
        self._joystick_word_ts = 0.0
        self.link_dead = False

    def reset_sequence_state(self) -> None:
        """Reset ARQ sequence counters and reassembly buffers (`ucp_set_conn_state(0)` equivalent)."""
        self.snd_nxt = 0
        self.rcv_nxt = 0
        self.video_sn = 0
        self.unrel_sn = 0
        self._tx_window.clear()
        self._rx_ooo.clear()
        for buf in self._rel_reasm.values():
            buf.clear()
        self._video_buf.clear()
        self._video_next_sn = None
        self._video_dropping = False

    def _build_sack(self, *, always: bool = False) -> SackTrailer | None:
        if not self._rx_ooo and not always:
            return None
        lo = 0
        hi = 0
        for sn in self._rx_ooo:
            d = seq_diff(sn, self.rcv_nxt)
            if 1 <= d <= 32:
                lo |= 1 << (d - 1)
            elif 33 <= d <= 64:
                hi |= 1 << (d - 33)
        return SackTrailer(rcv_nxt=self.rcv_nxt, bitmap_lo=lo, bitmap_hi=hi)

    def dispatch_callback(self, cb: Callable[..., Any] | None, *args: Any) -> None:
        """Run a user callback without letting it break the transport (it runs inside datagram_received)."""
        run_callback(cb, args, self._tasks)


    @property
    def tx_window_full(self) -> bool:
        """True when too many reliable segments are unacknowledged; producers should pause."""
        return len(self._tx_window) >= UCP_TX_WINDOW_MAX

    def send_reliable(self, channel: int, data: bytes) -> None:
        """Fragment and transmit reliable payload on Channel 3 (or 0)."""
        chunks = split_payload(data)
        now = time.monotonic()
        for idx, chunk in enumerate(chunks):
            more = idx < (len(chunks) - 1)
            sn = self.snd_nxt
            self.snd_nxt = (self.snd_nxt + 1) & 0xFFFF
            seg = UcpSegment(
                channel=channel,
                reliable=True,
                more_fragments=more,
                una=self.rcv_nxt,
                sn=sn,
                ts_us=monotonic_us(),
                payload=chunk,
                sack=self._build_sack(),
            )
            self._tx_window[sn] = (seg, now, 0)
            self._send_datagram(seg.encode())

    def send_unreliable(self, channel: int, data: bytes, *, force_sack: bool = False) -> None:
        """Send best-effort payload (e.g. Channel 0 heartbeat/ACK, Zenoh Push, or audio)."""
        chunks = split_payload(data)
        for idx, chunk in enumerate(chunks):
            more = idx < (len(chunks) - 1)
            sn = self.unrel_sn
            self.unrel_sn = (self.unrel_sn + 1) & 0xFFFF
            seg = UcpSegment(
                channel=channel,
                reliable=False,
                more_fragments=more,
                una=self.rcv_nxt,
                sn=sn,
                ts_us=monotonic_us(),
                payload=chunk,
                sack=self._build_sack(always=force_sack),
            )
            self._send_datagram(seg.encode())

    def send_video_frame(self, annexb_au: bytes) -> None:
        """Fragment and transmit an Annex-B H.265 Access Unit over UCP Channel 1."""
        if not annexb_au:
            return
        ts = monotonic_us()
        for offset in range(0, len(annexb_au), UCP_MAX_PAYLOAD):
            chunk = annexb_au[offset : offset + UCP_MAX_PAYLOAD]
            more = (offset + UCP_MAX_PAYLOAD) < len(annexb_au)
            sn = self.video_sn
            self.video_sn = (self.video_sn + 1) & 0xFFFF
            seg = UcpSegment(
                channel=UCP_CH_VIDEO,
                reliable=False,
                more_fragments=more,
                una=self.rcv_nxt,
                sn=sn,
                ts_us=ts,
                payload=chunk,
            )
            self._send_datagram(seg.encode())

    def send_audio_frame(self, opus_frame: bytes) -> None:
        """Send a single Opus frame over UCP Channel 2."""
        self.send_unreliable(UCP_CH_AUDIO, opus_frame)

    def send_zenoh_frame(self, frame_bytes: bytes, reliable: bool = True) -> None:
        """Send a Zenoh-over-UCP tunnel frame (`0xA4..0xA7`) on Channel 3 (reliable) or Channel 0 + SACK (unreliable)."""
        if reliable:
            self.send_reliable(UCP_CH_ZENOH, frame_bytes)
        else:
            # Unreliable Zenoh frames go on Channel 0 with HAS_SACK (`flags_ver = 0x1051`)
            if len(frame_bytes) == UCP_JOYSTICK_PAYLOAD_LEN:
                # The robot treats EVERY 8-byte Channel-0 + SACK payload as an RcJoystick word.
                raise ValueError("refusing an 8-byte unreliable Zenoh frame on Channel 0 (would be read as a joystick word)")
            self.send_unreliable(UCP_CH_CONTROL, frame_bytes, force_sack=True)

    def send_heartbeat_probe(self, liveview_enabled: bool | None = None) -> None:
        """Send 31-byte Channel 0 + SACK packet (`12B hdr + 8B u64 LE + 11B 0x05 SACK`, `flags_ver = 0x1051`).

        Robot-side behaviour:
          - Datagrams `< 13`B are dropped and `HAS_SACK` packets of `<= 23`B are rejected.
          - Bits `0..62` of the 8-byte `uint64 LE` payload are published directly to the robot's `/cmd/rc/joystick`!
            (Never put a clock timestamp here — the robot treats every 8-byte `0x1051` packet as `RcJoystick`!)
          - Bit `63` of the 8-byte `uint64 LE` payload sets `no_liveview_flag` (`0` = stream live H.265 video).
          - Header `una` + 11-byte `0x05` SACK trailer carry cumulative and selective ACKs to the peer.
        """
        if liveview_enabled is not None:
            self.liveview_enabled = liveview_enabled
        no_liveview_bit = 0 if self.liveview_enabled else UCP_NO_LIVEVIEW_BIT
        word = self.joystick_word & JOYSTICK_WORD_MASK
        if time.monotonic() - self._joystick_word_ts > JOYSTICK_HOLD_AFTER_S:
            word = joystick_hold_word(word)  # stale command: hold position, never resume motion or re-trigger
        word |= no_liveview_bit
        payload = struct.pack("<Q", word)
        self.send_unreliable(UCP_CH_CONTROL, payload, force_sack=True)

    def send_joystick_word(self, word: int) -> None:
        """Update active 63-bit `RcJoystick` state and immediately transmit a 31-byte `0x1051` packet."""
        self.joystick_word = int(word) & JOYSTICK_WORD_MASK
        self._joystick_word_ts = time.monotonic()
        self.send_heartbeat_probe()

    def send_ack_only(self) -> None:
        """Deliver cumulative `una` + `0x05` SACK + current `joystick_word` via a 31-byte `0x1051` packet."""
        self.send_heartbeat_probe()

    def tick_maintenance(self) -> None:
        """Retransmit unacknowledged reliable segments and update video bitrate/FPS metrics."""
        now = time.monotonic()
        rto = max(0.15, min(0.5, (self.rtt_ms / 1000.0) * 3.0 + 0.12))
        for sn, (seg, last_ts, retries) in list(self._tx_window.items()):
            if retries >= UCP_MAX_RETRIES:
                self.link_dead = True  # the client's maintenance loop turns this into a link loss
                continue
            if now - last_ts >= min(2.0, rto * (1.5 ** min(retries, 6))):  # exponential backoff
                seg.una = self.rcv_nxt
                seg.sack = self._build_sack()
                self._tx_window[sn] = (seg, now, retries + 1)
                self._send_datagram(seg.encode())

        dt = now - self._stats_ts
        if dt >= 0.5:
            self.video_fps = round(self._video_frames_window / dt, 1)
            self.video_kbps = round((self._video_bytes_window * 8.0) / (dt * 1000.0), 1)
            self._video_frames_window = 0
            self._video_bytes_window = 0
            self._stats_ts = now

    def _process_acks(self, una: int, sack: SackTrailer | None) -> None:
        for sn in list(self._tx_window.keys()):
            if seq_diff(sn, una) < 0:
                self._tx_window.pop(sn, None)
        if sack is not None:
            for sn in list(self._tx_window.keys()):
                if seq_diff(sn, sack.rcv_nxt) < 0:
                    self._tx_window.pop(sn, None)
                    continue
                d = seq_diff(sn, sack.rcv_nxt)
                if (1 <= d <= 32 and (sack.bitmap_lo & (1 << (d - 1)))) or (33 <= d <= 64 and (sack.bitmap_hi & (1 << (d - 33)))):
                    self._tx_window.pop(sn, None)

    def _deliver_ordered_reliable(self, seg: UcpSegment) -> None:
        buf = self._rel_reasm.setdefault(seg.channel, bytearray())
        if seg.channel == UCP_CH_VIDEO:
            if len(buf) + len(seg.payload) <= UCP_MAX_VIDEO_AU_BYTES:
                buf.extend(seg.payload)
            else:
                buf.clear()
            if not seg.more_fragments and buf:
                au = bytes(buf)
                buf.clear()
                self.video_frames_rx += 1
                self._video_frames_window += 1
                self._video_bytes_window += len(au)
                self.dispatch_callback(self.on_video_frame, au, is_hevc_keyframe(au), seg.ts_us)
            return

        if seg.channel == UCP_CH_AUDIO:
            buf.extend(seg.payload)
            if not seg.more_fragments:
                complete = bytes(buf)
                buf.clear()
                if complete:
                    self.audio_frames_rx += 1
                    self.dispatch_callback(self.on_audio_frame, complete)
            return

        buf.extend(seg.payload)
        if not seg.more_fragments:
            complete = bytes(buf)
            buf.clear()
            if complete:
                self._handle_control_or_zenoh(seg.channel, complete)

    def _handle_control_or_zenoh(self, channel: int, payload: bytes) -> None:
        if channel == UCP_CH_CONTROL and payload.startswith(UCP_HB_PREFIX):
            if len(payload) >= 10:
                length_code, subtype, ts_sent = struct.unpack_from("<BBI", payload, 4)
                if subtype == 1:
                    reply = UCP_HB_PREFIX + struct.pack("<BBI", length_code, 3, ts_sent)
                    self.send_unreliable(UCP_CH_CONTROL, reply)
                elif subtype in (2, 3):
                    rtt_us = (monotonic_us() - ts_sent) & 0xFFFFFFFF
                    if rtt_us < 5_000_000:
                        sample_ms = rtt_us / 1000.0
                        self.rtt_ms = round(0.75 * self.rtt_ms + 0.25 * sample_ms, 2)
            return

        if channel == UCP_CH_CONTROL and len(payload) == UCP_JOYSTICK_PAYLOAD_LEN and payload[0] not in Z_UCP_FRAME_TYPES:
            # Official 8-byte Channel 0 + SACK packet (`RcJoystick` in bits 0..62 + `no_liveview_flag` in bit 63)
            (hb_u64,) = struct.unpack("<Q", payload)
            self.liveview_enabled = not (hb_u64 & UCP_NO_LIVEVIEW_BIT)
            self.dispatch_callback(self.on_joystick_frame, hb_u64 & JOYSTICK_WORD_MASK)
            return

        if payload and payload[0] in Z_UCP_FRAME_TYPES:
            self.dispatch_callback(self.on_zenoh_frame, payload)

    def handle_datagram(self, datagram: bytes) -> None:
        self.last_rx_monotonic = time.monotonic()
        segments = decode_ucp_datagram(datagram)
        acked_reliable = False

        for seg in segments:
            self._process_acks(seg.una, seg.sack)

            if seg.reliable:
                d = seq_diff(seg.sn, self.rcv_nxt)
                if d == 0:
                    self.rcv_nxt = (self.rcv_nxt + 1) & 0xFFFF
                    self._deliver_ordered_reliable(seg)
                    while self.rcv_nxt in self._rx_ooo:
                        nxt_seg = self._rx_ooo.pop(self.rcv_nxt)
                        self.rcv_nxt = (self.rcv_nxt + 1) & 0xFFFF
                        self._deliver_ordered_reliable(nxt_seg)
                    acked_reliable = True
                elif 0 < d <= 512:
                    self._rx_ooo[seg.sn] = seg
                    acked_reliable = True
                else:
                    acked_reliable = True
                continue

            # Unreliable channels
            if seg.channel == UCP_CH_VIDEO:
                # A lost fragment would merge two access units or corrupt one: drop until the next AU boundary.
                # (only a gap INSIDE an AU matters; the real robot sends video reliably, see _deliver_ordered_reliable)
                gap = self._video_next_sn is not None and seg.sn != self._video_next_sn and bool(self._video_buf)
                self._video_next_sn = (seg.sn + 1) & 0xFFFF
                if gap:
                    self._video_buf.clear()
                    self._video_dropping = True
                if self._video_dropping:
                    if not seg.more_fragments:
                        self._video_dropping = False  # next fragment starts a fresh AU
                    continue
                if len(self._video_buf) + len(seg.payload) <= UCP_MAX_VIDEO_AU_BYTES:
                    self._video_buf.extend(seg.payload)
                else:
                    self._video_buf.clear()
                if not seg.more_fragments and self._video_buf:
                    au = bytes(self._video_buf)
                    self._video_buf.clear()
                    self.video_frames_rx += 1
                    self._video_frames_window += 1
                    self._video_bytes_window += len(au)
                    self.dispatch_callback(self.on_video_frame, au, is_hevc_keyframe(au), seg.ts_us)
            elif seg.channel == UCP_CH_AUDIO:
                if seg.payload:
                    self.audio_frames_rx += 1
                    self.dispatch_callback(self.on_audio_frame, seg.payload)
            elif seg.payload:
                self._handle_control_or_zenoh(seg.channel, seg.payload)

        if acked_reliable:
            self.send_ack_only()


class _ClientUdpProtocol(asyncio.DatagramProtocol):
    def __init__(self, client: UcpClient) -> None:
        self._client = client

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self._client._on_udp_datagram(data, addr)

    def error_received(self, exc: Exception) -> None:
        logger.debug("UCP UDP socket error: %s", exc)


class UcpClient:
    """Async UCP v1 client connecting to `<robot_ip>:9007` with optional split-socket interface pinning."""

    def __init__(
        self,
        host: str = SOFTAP_HOST,
        port: int = 9007,
        *,
        on_video_frame: VideoFrameCallback | None = None,
        on_audio_frame: BytesCallback | None = None,
        on_zenoh_frame: BytesCallback | None = None,
        bind_interface: str | None = None,
        local_ip: str | None = None,
        heartbeat_timeout: float = 3.5,
        degraded_silence_sec: float = 1.2,
        on_link_lost: Callable[[str], Any] | None = None,
        on_degraded_change: Callable[[bool], Any] | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.bind_interface = bind_interface
        self.local_ip = local_ip
        self.heartbeat_timeout = heartbeat_timeout
        self.degraded_silence_sec = degraded_silence_sec
        self.on_link_lost = on_link_lost
        self.on_degraded_change = on_degraded_change
        self.degraded = False

        self._transport: asyncio.DatagramTransport | None = None
        self._ack_event = asyncio.Event()
        self._connected = False
        self._closing_intentionally = False
        self._maint_task: asyncio.Task[None] | None = None
        self.session = UcpEndpointSession(
            send_datagram=self._send_raw,
            on_video_frame=on_video_frame,
            on_audio_frame=on_audio_frame,
            on_zenoh_frame=on_zenoh_frame,
            latch_key=latch_key_for_host(host),
        )

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def rx_silence_ms(self) -> int:
        if not self._connected:
            return 0
        return int(max(0.0, (time.monotonic() - self.session.last_rx_monotonic) * 1000.0))

    def _emit_cb(self, cb: Callable[..., Any] | None, *args: Any) -> None:
        self.session.dispatch_callback(cb, *args)

    def _send_raw(self, data: bytes) -> None:
        if self._transport is not None and not self._transport.is_closing():
            self._transport.sendto(data)

    def _on_udp_datagram(self, data: bytes, _addr: tuple[str, int]) -> None:
        if data == UCP_ACK or data.startswith(UCP_ACK):
            self.session.last_rx_monotonic = time.monotonic()
            self._connected = True
            self._ack_event.set()
            return
        if data == UCP_LEAV:
            was_connected = self._connected
            self._connected = False
            if was_connected and not self._closing_intentionally:
                self._emit_cb(self.on_link_lost, "Robot closed UCP session (ucp:leav)")
            return
        self.session.handle_datagram(data)

    async def connect(
        self,
        timeout: float = 2.0,
        retries: int = 3,
        *,
        reset_first: bool = False,
    ) -> None:
        if self._transport is not None:
            await self.close()
        self._closing_intentionally = False
        self._connected = False
        self.degraded = False
        self.session.reset_sequence_state()
        self.session.prepare_new_session()  # hold word + latch re-read: never resume a pre-disconnect command
        loop = asyncio.get_running_loop()

        if self.bind_interface or self.local_ip:
            sock = create_bound_udp_socket(
                self.host,
                self.port,
                bind_interface=self.bind_interface,
                local_ip=self.local_ip,
            )
            transport, _ = await loop.create_datagram_endpoint(
                lambda: _ClientUdpProtocol(self),
                sock=sock,
            )
        else:
            transport, _ = await loop.create_datagram_endpoint(
                lambda: _ClientUdpProtocol(self),
                remote_addr=(self.host, self.port),
            )

        self._transport = transport
        self._ack_event.clear()

        if reset_first:
            # Force the robot to drop its UCP connection state so rcv_nxt/snd_nxt restart from 0
            self._send_raw(UCP_LEAV)
            await asyncio.sleep(0.08)
            self._ack_event.clear()
            self._connected = False

        per_try = max(0.25, timeout / max(1, retries))
        for _ in range(retries):
            self._send_raw(UCP_SYNC)
            try:
                await asyncio.wait_for(self._ack_event.wait(), timeout=per_try)
                break
            except asyncio.TimeoutError:
                continue

        if not self._connected:
            self._transport.close()
            self._transport = None
            raise TimeoutError(f"UCP handshake timed out connecting to {self.host}:{self.port}")

        self.session.last_rx_monotonic = time.monotonic()
        # Immediately announce initial una=0 + SACK + no_liveview_flag=0 (31B Channel 0 + SACK)
        self.session.send_heartbeat_probe()
        self._maint_task = asyncio.create_task(self._maintenance_loop(), name="ucp-client-maintenance")

    async def _maintenance_loop(self) -> None:
        tick = 0
        try:
            while self._connected:
                await asyncio.sleep(0.05)
                self.session.tick_maintenance()
                tick += 1
                if tick % 4 == 0:  # 200ms heartbeat probe
                    self.session.send_heartbeat_probe()

                if self.session.link_dead:
                    self._connected = False
                    if not self._closing_intentionally:
                        self._emit_cb(self.on_link_lost, f"UCP reliable segment unacknowledged after {UCP_MAX_RETRIES} retries")
                    return

                silence = time.monotonic() - self.session.last_rx_monotonic
                now_degraded = silence >= self.degraded_silence_sec or self.session.rtt_ms >= 250.0
                if now_degraded != self.degraded:
                    self.degraded = now_degraded
                    self._emit_cb(self.on_degraded_change, now_degraded)

                if silence >= self.heartbeat_timeout:
                    self._connected = False
                    if not self._closing_intentionally:
                        self._emit_cb(
                            self.on_link_lost,
                            f"UCP heartbeat timeout ({silence:.1f}s without UDP response from {self.host}:{self.port})",
                        )
                    return
        except asyncio.CancelledError:
            return

    async def close(self) -> None:
        self._closing_intentionally = True
        self._connected = False
        self.degraded = False
        if self._maint_task is not None:
            self._maint_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._maint_task
            self._maint_task = None
        if self._transport is not None:
            try:
                self._send_raw(UCP_LEAV)
            except OSError as exc:  # best-effort goodbye; the robot's 3.5 s watchdog covers a lost LEAV
                logger.debug("UCP LEAV send failed: %s", exc)
            self._transport.close()
            self._transport = None
