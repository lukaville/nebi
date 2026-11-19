"""Simulated robot audio-service intercom: validates downlink playback frames and streams a synthetic robot-mic uplink."""

from __future__ import annotations

import array
import asyncio
import math
import time
from collections import deque
from collections.abc import Callable
from typing import Any

from nebi.intercom import INTERCOM_FRAME_MS, INTERCOM_FRAME_SAMPLES, OpusDecoder, OpusEncoder
from nebi.transport.ucp import UcpEndpointSession
from nebi.transport.ucp_codec import UCP_CH_AUDIO


class SimIntercom:
    """Mirrors the robot: the uplink sends Opus only while `active` (after START_RECORD)."""

    def __init__(self, get_ucp: Callable[[], UcpEndpointSession | None]) -> None:
        self._get_ucp = get_ucp
        self.active: bool = False
        self.echo: bool = True  # loop downlink audio back with a room-echo delay
        self.rx_frames: int = 0
        self.rx_rejected: int = 0  # frames the real robot would drop
        self.tx_frames: int = 0
        self.rx_sessions: int = 0
        self.last_rx_seq: int = -1
        self.rx_level: float = 0.0
        self._decoder = OpusDecoder()
        self._echo_buf: deque[bytes] = deque()
        self._task: asyncio.Task[None] | None = None

    def reset_decoder(self) -> None:
        self._decoder.reset()

    def set_active(self, active: bool) -> None:
        self.active = active
        if active and (self._task is None or self._task.done()):
            self._task = asyncio.create_task(self._uplink_loop(), name="sim-intercom-uplink")
        elif not active and self._task is not None:
            self._task.cancel()
            self._task = None
            self._echo_buf.clear()

    def on_playback(self, d: Any) -> None:
        """Downlink frame from the app (`AudioPlaybackData`) -> validate and decode like the robot does."""
        # The robot requires all fields present, len(data) <= 960, data_size == len(data), and
        # encoding 0, 48 kHz, mono, 20 ms, 1 <= data_size <= 960.
        fields = ("encoding_type", "sample_rate", "channels", "frame_duration_ms", "timestamp", "seq_num_audio", "data_size")
        if not all(d.HasField(f) for f in fields):
            self.rx_rejected += 1
            return
        size = int(d.data_size)
        if not (0 < size <= 960 and size == len(d.data)) or (
            int(d.encoding_type), int(d.sample_rate), int(d.channels), int(d.frame_duration_ms)
        ) != (0, 48000, 1, 20):
            self.rx_rejected += 1
            return
        seq = int(d.seq_num_audio)
        if seq == 0:  # seq 0 starts a new session: the robot resets its playback state
            self._decoder.reset()
            self.rx_sessions += 1
        self.last_rx_seq = seq
        self.rx_frames += 1
        pcm = self._decoder.decode(bytes(d.data[:size]))
        if not pcm:
            return
        samples = array.array("h", pcm)
        peak = max((abs(v) for v in samples), default=0)
        self.rx_level = max(peak / 32768.0, self.rx_level * 0.8)
        if self.active and self.echo and len(self._echo_buf) < 100:
            self._echo_buf.append(pcm)

    async def _uplink_loop(self) -> None:
        """20 ms robot-mic uplink: room tone + periodic chirp + delayed echo of what the app said."""
        enc = OpusEncoder()
        frame_s = INTERCOM_FRAME_MS / 1000.0
        echo_delay_frames = 18  # ~360 ms "room" round trip
        n = 0
        rng = 0x1234ABCD
        echo_primed = False
        next_t = time.monotonic()
        try:
            while self.active:
                out = array.array("h", bytes(INTERCOM_FRAME_SAMPLES * 2))
                t_frame = n * INTERCOM_FRAME_SAMPLES
                chirp_phase = (n % 150) * INTERCOM_FRAME_SAMPLES  # one chirp every 3 s
                for i in range(INTERCOM_FRAME_SAMPLES):
                    rng = (rng * 1103515245 + 12345) & 0x7FFFFFFF
                    v = ((rng >> 16) & 0xFF) - 128  # faint room noise
                    k = chirp_phase + i
                    if k < 9600:  # 200 ms rising "beep-boop" chirp
                        env = math.sin(math.pi * k / 9600)
                        f = 900.0 + 900.0 * (k / 9600)
                        v += int(5000 * env * math.sin(2 * math.pi * f * (t_frame + i) / 48000))
                    out[i] = v
                # Start replaying once ~360 ms is buffered, then drain one frame per tick
                if len(self._echo_buf) >= echo_delay_frames:
                    echo_primed = True
                elif not self._echo_buf:
                    echo_primed = False
                if echo_primed:
                    echo = array.array("h", self._echo_buf.popleft())
                    for i in range(min(len(echo), INTERCOM_FRAME_SAMPLES)):
                        out[i] = max(-32768, min(32767, out[i] + int(echo[i] * 0.6)))
                ucp = self._get_ucp()
                for pkt in enc.encode(out.tobytes()):
                    if ucp is not None:
                        ucp.send_reliable(UCP_CH_AUDIO, pkt)  # hardware: flags = 0x0102
                        self.tx_frames += 1
                n += 1
                next_t += frame_s
                delay = next_t - time.monotonic()
                if delay < -0.2:  # fell far behind (debugger/GC): resync instead of bursting
                    next_t = time.monotonic()
                    delay = 0.0
                await asyncio.sleep(max(0.0, delay))
        except asyncio.CancelledError:
            return

    # --- Fault Injection Helper (for Web UI & Tests) ---
