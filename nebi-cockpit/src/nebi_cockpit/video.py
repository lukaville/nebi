"""Live Annex-B H.265 (HEVC) decoder & browser broadcaster (`WebSocket JPEG`, `MJPEG`).

HEVC decode + JPEG encode run on one worker thread, never on the asyncio loop (the PIL conversion and
JPEG encode cost several ms per frame and would stall the control socket / telemetry push). The loop
only enqueues access units into a small bounded queue and receives finished JPEGs back via
`loop.call_soon_threadsafe`.

Queue policy: an IDR (keyframe) is never dropped in favour of a P-frame. When the worker falls behind
and the queue overflows, everything older than the newest queued IDR is dropped; if there is no IDR to
resync on, the whole queue is dropped and P-frames are skipped until the next IDR arrives (a keyframe
is requested via `on_keyframe_needed`). Decoding a P-frame without its references only produces smear,
so skipping to the next IDR is the correct recovery.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import threading
import time
from collections import deque
from collections.abc import AsyncIterator, Callable
from typing import cast

import av
from av.video.codeccontext import VideoCodecContext
from PIL import Image

logger = logging.getLogger(__name__)

_INPUT_QUEUE_MAX = 6  # access units waiting for the decoder (~250 ms at 24 fps)
_KEYFRAME_REQUEST_MIN_INTERVAL_S = 0.5
_JPEG_MIN_INTERVAL_STREAMING_S = 0.028  # ~35 fps cap while a browser is watching
_JPEG_MIN_INTERVAL_IDLE_S = 0.25  # 4 fps keeps `latest_jpeg` (snapshot, agent) fresh with no viewers
_SUBSCRIBER_QUEUE_FRAMES = 2
_PLACEHOLDER_SIZE = (640, 360)
_PLACEHOLDER_RGB = (16, 22, 34)
_WORKER_JOIN_TIMEOUT_S = 1.0

_Item = tuple[int, bytes, bool, int]  # (generation, annexb_au, is_keyframe, ts_us)


class VideoStreamHub:
    """Decodes incoming robot Annex-B H.265 Access Units via `PyAV` and fans out low-latency browser streams."""

    def __init__(self, jpeg_quality: int = 82, on_keyframe_needed: Callable[[], None] | None = None) -> None:
        self.jpeg_quality = jpeg_quality
        self.on_keyframe_needed = on_keyframe_needed

        self.latest_keyframe_au: bytes | None = None
        self.latest_jpeg: bytes | None = None
        self.latest_width, self.latest_height = _PLACEHOLDER_SIZE
        self.frames_decoded: int = 0
        self.frames_dropped: int = 0
        self.last_frame_ts: float = 0.0

        self._jpeg_queues: set[asyncio.Queue[bytes]] = set()

        # loop-side state
        self._seen_keyframe = False  # gate: nothing is queued until an IDR has been seen
        self._generation = 0  # bumped by reset_decoder(); stale worker results are discarded
        self._loop: asyncio.AbstractEventLoop | None = None
        self._last_kf_request = 0.0

        # shared with the worker (guarded by _cond)
        self._cond = threading.Condition()
        self._queue: deque[_Item] = deque()
        self._stopping = False
        self._thread: threading.Thread | None = None

        # worker-only state
        self._decoder: VideoCodecContext | None = None
        self._decoder_gen = -1
        self._last_jpeg_mono = 0.0

        # Initial placeholder frame before the first IDR arrives
        placeholder = Image.new("RGB", _PLACEHOLDER_SIZE, _PLACEHOLDER_RGB)
        buf = io.BytesIO()
        placeholder.save(buf, format="JPEG", quality=80)
        self.latest_jpeg = buf.getvalue()

    # ------------------------------------------------------------------ loop side

    def reset_decoder(self) -> None:
        """Forget all decoder state and the last frame (robot switch / reconnect)."""
        with self._cond:
            self._generation += 1
            self._queue.clear()
        self._seen_keyframe = False
        self.latest_keyframe_au = None
        self.latest_jpeg = None
        self.frames_decoded = 0
        self.last_frame_ts = 0.0

    def close(self) -> None:
        """Stop the decode worker (it restarts automatically on the next access unit)."""
        with self._cond:
            self._stopping = True
            self._queue.clear()
            self._cond.notify_all()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=_WORKER_JOIN_TIMEOUT_S)
        self._thread = None

    def on_h265_access_unit(self, annexb_au: bytes, is_keyframe: bool, ts_us: int) -> None:
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            pass  # called off-loop (sync tests): keep the last known loop
        if is_keyframe:
            self._seen_keyframe = True
            self.latest_keyframe_au = annexb_au
        if not self._seen_keyframe:
            return
        self._ensure_worker()
        resync = False
        with self._cond:
            q = self._queue
            if len(q) >= _INPUT_QUEUE_MAX:
                if is_keyframe:
                    # The new IDR restarts the reference chain: everything queued before it is obsolete.
                    self.frames_dropped += len(q)
                    q.clear()
                else:
                    idx = max((i for i, it in enumerate(q) if it[2]), default=-1)
                    if idx > 0:  # resync on the newest queued IDR
                        for _ in range(idx):
                            q.popleft()
                        self.frames_dropped += idx
                    if len(q) >= _INPUT_QUEUE_MAX:
                        # No IDR to resync on: drop everything (incl. this P-frame) up to the next keyframe.
                        self.frames_dropped += len(q) + 1
                        q.clear()
                        self._seen_keyframe = False
                        resync = True
            if not resync:
                q.append((self._generation, annexb_au, is_keyframe, ts_us))
                self._cond.notify()
        if resync:
            self._request_keyframe_from_loop()

    def _ensure_worker(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        with self._cond:
            self._stopping = False
        self._thread = threading.Thread(target=self._worker, name="nebi-video-decode", daemon=True)
        self._thread.start()

    def _request_keyframe_from_loop(self) -> None:
        cb = self.on_keyframe_needed
        if cb is None:
            return
        now = time.monotonic()
        if now - self._last_kf_request < _KEYFRAME_REQUEST_MIN_INTERVAL_S:
            return
        self._last_kf_request = now
        try:
            cb()
        except Exception as exc:  # never let a keyframe request break the video path
            logger.debug("keyframe request failed: %s", exc)

    def _publish(self, gen: int, jpg: bytes, width: int, height: int) -> None:
        """Runs on the loop thread (scheduled by the worker)."""
        if gen != self._generation:
            return
        self.latest_jpeg = jpg
        self.latest_width, self.latest_height = width, height
        for jq in tuple(self._jpeg_queues):
            if jq.full():  # slow viewer: drop its oldest frame, never block the loop
                with contextlib.suppress(asyncio.QueueEmpty):
                    jq.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                jq.put_nowait(jpg)

    def _on_decode_error(self, gen: int) -> None:
        """Runs on the loop thread: skip to the next IDR and ask the robot for one."""
        if gen != self._generation:
            return
        self._seen_keyframe = False
        with self._cond:
            # Queued P-frames reference the broken picture; keep only from the next IDR on.
            while self._queue and not self._queue[0][2]:
                self._queue.popleft()
        self._request_keyframe_from_loop()

    def _call_on_loop(self, fn: Callable[..., None], *args: object) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            if fn == self._publish:
                # No loop (synchronous use): keep the latest frame; async fan-out is impossible.
                gen, jpg, w, h = args
                if gen == self._generation:
                    self.latest_jpeg, self.latest_width, self.latest_height = jpg, w, h  # type: ignore[assignment]
            return
        with contextlib.suppress(RuntimeError):  # loop closed between the check and the call
            loop.call_soon_threadsafe(fn, *args)

    # ------------------------------------------------------------------ worker thread

    def _worker(self) -> None:
        while True:
            with self._cond:
                while not self._queue and not self._stopping:
                    self._cond.wait(timeout=1.0)
                if self._stopping:
                    return
                gen, au, is_kf, _ts = self._queue.popleft()
                current_gen = self._generation
            if gen != current_gen:
                continue
            try:
                self._decode_one(gen, au, is_kf)
            except Exception as exc:  # pragma: no cover - defensive; the thread must not die
                logger.debug("video worker error: %s", exc)

    def _decode_one(self, gen: int, au: bytes, is_kf: bool) -> None:
        if self._decoder is None or self._decoder_gen != gen:
            self._decoder = cast(VideoCodecContext, av.CodecContext.create("hevc", "r"))
            self._decoder.open()
            self._decoder_gen = gen
            self._last_jpeg_mono = 0.0
        try:
            frames = self._decoder.decode(av.Packet(au))
        except Exception as exc:
            logger.debug("HEVC decode transient error (awaiting next IDR): %s", exc)
            self._call_on_loop(self._on_decode_error, gen)
            return
        for vf in frames:
            if gen != self._generation:
                return
            self.frames_decoded += 1
            self.last_frame_ts = time.time()
            now_mono = time.monotonic()
            min_interval = _JPEG_MIN_INTERVAL_STREAMING_S if self._jpeg_queues else _JPEG_MIN_INTERVAL_IDLE_S
            if not is_kf and (now_mono - self._last_jpeg_mono) < min_interval:
                continue
            self._last_jpeg_mono = now_mono
            img = vf.to_image()
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=self.jpeg_quality)
            self._call_on_loop(self._publish, gen, buf.getvalue(), img.size[0], img.size[1])

    # ------------------------------------------------------------------ subscribers

    def subscribe_jpeg(self) -> asyncio.Queue[bytes]:
        q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=_SUBSCRIBER_QUEUE_FRAMES)
        if self.latest_jpeg is not None and self.frames_decoded > 0:
            q.put_nowait(self.latest_jpeg)
        self._jpeg_queues.add(q)
        return q

    def unsubscribe_jpeg(self, q: asyncio.Queue[bytes]) -> None:
        self._jpeg_queues.discard(q)

    async def mjpeg_generator(self) -> AsyncIterator[bytes]:
        q = self.subscribe_jpeg()
        try:
            while True:
                jpg = await q.get()
                header = (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    + f"Content-Length: {len(jpg)}\r\n\r\n".encode("ascii")
                )
                yield header + jpg + b"\r\n"
        finally:
            self.unsubscribe_jpeg(q)
