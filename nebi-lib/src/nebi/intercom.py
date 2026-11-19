"""Two-way Opus intercom for Nebi.

Direction        | Transport                                                    | Payload
-----------------|--------------------------------------------------------------|-----------------------------------------
Robot -> App     | UCP `:9007` Channel 2 (reliable, `flags = 0x0102`)           | one raw Opus packet per message
App -> Robot     | Zenoh `app/data/audio/playback_data` (`AudioPlaybackData`)   | Opus packet + seq/timestamp metadata
Control          | Zenoh `app/cmd/audio/op_req` -> `device/cmd/audio/op_rsp`    | `operation_type` (`AudioOperation`)

Both directions use Opus 48 kHz, mono, 20 ms (960-sample) frames (VOIP application, FEC on).

The robot only encodes its microphone while the intercom is active (after `START_RECORD`), so
`Intercom.start()` must be called before any robot audio arrives.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from enum import IntEnum
from typing import TYPE_CHECKING, Any

from nebi._callbacks import ListenerSet, TaskSet
from nebi.speaker import Speaker

if TYPE_CHECKING:
    from nebi.transport.zenoh_ucp import ZenohTopicBus

logger = logging.getLogger(__name__)

INTERCOM_SAMPLE_RATE = 48_000
INTERCOM_CHANNELS = 1
INTERCOM_FRAME_MS = 20
INTERCOM_FRAME_SAMPLES = INTERCOM_SAMPLE_RATE * INTERCOM_FRAME_MS // 1000  # 960
INTERCOM_FRAME_BYTES = INTERCOM_FRAME_SAMPLES * 2  # s16le mono
# The robot rejects a playback frame unless `1 <= len(data) <= 960` and `data_size == len(data)`.
OPUS_MAX_PACKET_BYTES = 960
OPUS_ENCODING_TYPE = 0  # `AudioPlaybackDataData.encoding_type` for Opus
DEFAULT_OPUS_BITRATE = 32_000

PLAYBACK_TOPIC = "/data/audio/playback_data"
OP_REQ_TOPIC = "/cmd/audio/op_req"
OP_RSP_TOPIC = "/cmd/audio/op_rsp"


class AudioOperation(IntEnum):
    """`AudioOpReqData.operation_type` values handled by the robot's audio service."""

    START_RECORD = 0  # intercom uplink on: mic -> AEC -> denoise -> Opus -> UCP Channel 2
    STOP_RECORD = 1  # intercom uplink off (Opus encoder bypassed)
    RESET = 7  # stop intercom recording + reset state
    PCM_STREAM = 8  # internal PCM streaming session control (not used by the app intercom)


PcmCallback = Callable[[bytes], Any]
OpusCallback = Callable[[bytes], Any]


class OpusEncoder:
    """Incremental 48 kHz mono Opus encoder: feed arbitrary s16le PCM, get 20 ms Opus packets."""

    def __init__(self, bitrate: int = DEFAULT_OPUS_BITRATE) -> None:
        import av  # lazy: PyAV is heavy and only needed once audio is encoded/decoded

        self._av = av
        ctx = av.CodecContext.create("libopus", "w")
        ctx.sample_rate = INTERCOM_SAMPLE_RATE
        ctx.layout = "mono"
        ctx.format = "s16"
        ctx.bit_rate = int(bitrate)
        ctx.options = {
            "application": "voip",
            "frame_duration": str(INTERCOM_FRAME_MS),
            "fec": "1",
            "packet_loss": "10",
            "compression_level": "10",
        }
        ctx.open()
        self._ctx = ctx
        self._pending = bytearray()
        self._pts = 0

    def encode(self, pcm_s16le: bytes) -> list[bytes]:
        """Append PCM and return every complete 20 ms Opus packet now available."""
        self._pending += pcm_s16le
        out: list[bytes] = []
        while len(self._pending) >= INTERCOM_FRAME_BYTES:
            chunk = bytes(self._pending[:INTERCOM_FRAME_BYTES])
            del self._pending[:INTERCOM_FRAME_BYTES]
            frame = self._av.AudioFrame(format="s16", layout="mono", samples=INTERCOM_FRAME_SAMPLES)
            frame.planes[0].update(chunk)
            frame.sample_rate = INTERCOM_SAMPLE_RATE
            frame.pts = self._pts
            self._pts += INTERCOM_FRAME_SAMPLES
            out.extend(bytes(p) for p in self._ctx.encode(frame))
        return out

    def reset(self) -> None:
        self._pending.clear()


class OpusDecoder:
    """48 kHz mono Opus packet decoder returning s16le PCM."""

    def __init__(self) -> None:
        self._new()

    def _new(self) -> None:
        import av  # lazy: heavy optional dependency, see OpusEncoder

        self._av = av
        ctx = av.CodecContext.create("libopus", "r")
        ctx.sample_rate = INTERCOM_SAMPLE_RATE
        ctx.layout = "mono"
        self._ctx = ctx
        self._resampler = av.AudioResampler(format="s16", layout="mono", rate=INTERCOM_SAMPLE_RATE)

    def decode(self, opus_packet: bytes) -> bytes:
        try:
            frames = self._ctx.decode(self._av.Packet(opus_packet))
        except self._av.error.FFmpegError as exc:  # corrupt packet: drop it, keep the stream alive
            logger.debug("Opus decode error: %s", exc)
            return b""
        out = bytearray()
        for frame in frames:
            for rs in self._resampler.resample(frame):
                out += bytes(rs.planes[0])[: rs.samples * 2]
        return bytes(out)

    def reset(self) -> None:
        self._new()


def build_playback_fields(opus_packet: bytes, seq: int) -> dict[str, Any]:
    """`AudioPlaybackDataData` fields for one downlink frame (mirrors the firmware mock publisher)."""
    if not 0 < len(opus_packet) <= OPUS_MAX_PACKET_BYTES:
        raise ValueError(f"Opus packet too large for robot slot: {len(opus_packet)} > {OPUS_MAX_PACKET_BYTES}")
    return {
        "encoding_type": OPUS_ENCODING_TYPE,
        "sample_rate": INTERCOM_SAMPLE_RATE,
        "channels": INTERCOM_CHANNELS,
        "frame_duration_ms": INTERCOM_FRAME_MS,
        "timestamp": (seq * INTERCOM_FRAME_MS) & 0xFFFFFFFF,
        "seq_num_audio": seq & 0xFFFFFFFF,
        "data_size": len(opus_packet),
        "data": opus_packet,  # exact length: the robot rejects padding (data_size must equal len(data))
    }


class Intercom:
    """Full-duplex intercom session on top of an `AsyncNebiClient`.

    ```python
    async with AsyncNebiClient(host) as robot:
        ic = robot.intercom
        ic.add_pcm_listener(lambda pcm: speaker.write(pcm))   # robot mic, s16le 48 kHz mono
        await ic.start()
        ic.send_pcm(mic_chunk)                                # your mic, s16le 48 kHz mono
        ...
        await ic.stop()
    ```

    Raw Opus is also available: `add_opus_listener()` / `send_opus()`. Code that plays speech should claim the
    speaker first (`intercom.speaker`, see `nebi.speaker`) so it never talks over another voice.
    """

    def __init__(
        self,
        bus_getter: Callable[[], ZenohTopicBus],
        add_audio_listener: Callable[[OpusCallback], Callable[[], None]],
    ) -> None:
        self._get_bus = bus_getter  # the client rebuilds its bus on reconnect; always use the live one
        self._add_audio_listener = add_audio_listener
        self._tasks = TaskSet("intercom listener")
        self._pcm_listeners: ListenerSet[PcmCallback] = ListenerSet(self._tasks)
        self._opus_listeners: ListenerSet[OpusCallback] = ListenerSet(self._tasks)
        self._unsub_audio: Callable[[], None] | None = None
        self._encoder: OpusEncoder | None = None
        self._decoder: OpusDecoder | None = None
        self._tx_seq = 0
        self.active = False
        self.bitrate = DEFAULT_OPUS_BITRATE
        self.rx_frames = 0
        self.tx_frames = 0
        self.last_rx_monotonic = 0.0
        self.last_op_ret_code: int | None = None
        self.speaker = Speaker(self)  # one voice at a time on the downlink (speech turns vs push-to-talk)

    # --- lifecycle ---------------------------------------------------------------------------

    async def start(self, timeout: float = 2.0) -> dict[str, Any]:
        """Enable robot mic uplink (`START_RECORD`) and start receiving/decoding robot audio."""
        if self._unsub_audio is None:
            self._unsub_audio = self._add_audio_listener(self._on_robot_opus)
        self._decoder = OpusDecoder()
        self.new_talk_session()
        try:
            res = await self._operation(AudioOperation.START_RECORD, timeout)
        except BaseException:
            self.detach()  # a failed/timed-out START must not leave the intercom marked active
            raise
        self.active = True
        return res

    async def stop(self, timeout: float = 2.0) -> dict[str, Any]:
        """Disable robot mic uplink (`STOP_RECORD`) and stop decoding."""
        self.active = False
        if self._unsub_audio is not None:
            self._unsub_audio()
            self._unsub_audio = None
        if self._encoder is not None:
            self._encoder.reset()
        return await self._operation(AudioOperation.STOP_RECORD, timeout)

    async def restore(self, timeout: float = 2.0) -> dict[str, Any] | None:
        """Re-arm the robot side after a reconnect (the robot forgets intercom state with the session)."""
        if not self.active:
            return None
        self.new_talk_session()
        if self._decoder is not None:
            self._decoder.reset()
        return await self._operation(AudioOperation.START_RECORD, timeout)

    async def reset(self, timeout: float = 2.0) -> dict[str, Any]:
        return await self._operation(AudioOperation.RESET, timeout)

    def detach(self) -> None:
        """Drop local listeners without sending anything to the robot (used on disconnect)."""
        self.active = False
        if self._unsub_audio is not None:
            self._unsub_audio()
            self._unsub_audio = None

    async def _operation(self, op: AudioOperation, timeout: float) -> dict[str, Any]:
        rsp = await self._get_bus().request(OP_REQ_TOPIC, OP_RSP_TOPIC, timeout=timeout, operation_type=int(op))
        self.last_op_ret_code = int(rsp.data.ret_code)
        return {"operation": op.name, "ret_code": self.last_op_ret_code}

    # --- downlink (app -> robot speaker) -----------------------------------------------------

    def new_talk_session(self) -> None:
        """Restart downlink numbering: `seq_num_audio = 0` makes the robot's audio service reset its jitter buffer."""
        self._tx_seq = 0
        self._encoder = OpusEncoder(self.bitrate)

    def send_opus(self, opus_packet: bytes) -> int:
        """Publish one 20 ms Opus packet to the robot speaker; returns its `seq_num_audio`."""
        seq = self._tx_seq
        self._get_bus().publish(PLAYBACK_TOPIC, **build_playback_fields(opus_packet, seq))
        self._tx_seq = (self._tx_seq + 1) & 0xFFFFFFFF
        self.tx_frames += 1
        return seq

    def send_pcm(self, pcm_s16le: bytes) -> int:
        """Encode s16le 48 kHz mono PCM (any length) and send every complete 20 ms frame."""
        if self._encoder is None:
            self._encoder = OpusEncoder(self.bitrate)
        packets = self._encoder.encode(pcm_s16le)
        for pkt in packets:
            self.send_opus(pkt)
        return len(packets)

    # --- uplink (robot mic -> app) -----------------------------------------------------------

    def add_pcm_listener(self, callback: PcmCallback) -> Callable[[], None]:
        """Robot-mic audio as s16le 48 kHz mono PCM; returns an unsubscribe function."""
        return self._pcm_listeners.add(callback)

    def add_opus_listener(self, callback: OpusCallback) -> Callable[[], None]:
        """Raw robot-mic Opus packets; returns an unsubscribe function."""
        return self._opus_listeners.add(callback)

    def _on_robot_opus(self, opus_packet: bytes) -> None:
        if not self.active:
            return
        self.rx_frames += 1
        self.last_rx_monotonic = time.monotonic()
        self._opus_listeners.emit(opus_packet)
        if not self._pcm_listeners or self._decoder is None:
            return
        pcm = self._decoder.decode(opus_packet)
        if pcm:
            self._pcm_listeners.emit(pcm)

    def status(self) -> dict[str, Any]:
        age = time.monotonic() - self.last_rx_monotonic if self.last_rx_monotonic else None
        return {
            "active": self.active,
            "rx_frames": self.rx_frames,
            "tx_frames": self.tx_frames,
            "rx_age_s": round(age, 2) if age is not None else None,
            "last_ret_code": self.last_op_ret_code,
        }

