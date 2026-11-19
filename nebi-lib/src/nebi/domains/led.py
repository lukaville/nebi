"""Eye LEDs: solid colours and animated expressions on `/cmd/led/ctrl_oneway` (2 eyes x 7 LEDs, `0xRRGGBBWW`).

The robot shows its own idle look ("firmware eyes") until an app streams frames on `/cmd/led/ctrl_oneway`; the
frames take over the eyes immediately, and shortly after they stop the firmware takes the eyes back by itself.
So there is no "release" message: holding a colour means re-sending it a few times per second, and releasing
means stopping. One background loop runs at a time; every call replaces the previous one.

Public API (on `robot.system`, also reachable as `robot.eyes`):

* `set_color(r, g, b, w=0, hold_sec=30.0, brightness=1.0)`: `hold_sec=None` holds until `release()`;
* `play_expression(name, loops=1, brightness=1.0)`: `loops=0` repeats until `release()`;
* `release()` / `stop()`: stop sending, the firmware eyes come back;
* `list_expressions()`, `eyes_status()`.

Bad arguments raise `NebiError` with `code` (`LED_BAD_COLOR`, `LED_BAD_HOLD`, `LED_BAD_LOOPS`,
`LED_BAD_BRIGHTNESS`, `LED_UNKNOWN_EXPRESSION`), `message` and `hint`.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nebi.domains._base import DomainController
from nebi.exceptions import NebiError

if TYPE_CHECKING:
    from nebi.transport.zenoh_ucp import ZenohTopicBus

logger = logging.getLogger(__name__)

LED_COUNT = 14  # [0..6] left eye, [7..13] right eye
LED_FRAME_INTERVAL_S = 0.04  # 25 Hz animation frames
LED_HOLD_REFRESH_S = 0.20  # the firmware takes the eyes back unless frames keep coming (~5 Hz is plenty)
EYE_ANIMATION_STEPS = 36  # ~1.44 s per loop at 25 Hz
DEFAULT_HOLD_S = 30.0
MAX_HOLD_S = 24 * 3600.0
MAX_LOOPS = 10_000

# name -> description (the animations are synthesized by `build_eye_animation`)
EXPRESSIONS: dict[str, str] = {
    "happy": "gold and cyan smile wave",
    "wink": "right eye winks with a gold slit",
    "heart": "rose-crimson double heartbeat",
    "cool": "blue-violet scanner sweep",
    "sleepy": "slow indigo breathing",
    "dizzy": "counter-rotating spinners",
    "angry": "fast red-orange pulse",
    "music_pulse": "rainbow VU-meter bounce",
}
EXPRESSION_ALIASES = {"love": "heart", "music": "music_pulse"}
LOOP_SECONDS = EYE_ANIMATION_STEPS * LED_FRAME_INTERVAL_S


def pack_rgbw(r: int, g: int, b: int, w: int = 0) -> int:
    """Pack one `0xRRGGBBWW` LED pixel."""
    return ((int(r) & 0xFF) << 24) | ((int(g) & 0xFF) << 16) | ((int(b) & 0xFF) << 8) | (int(w) & 0xFF)


def build_eye_animation(effect_name: str) -> list[list[int]]:
    """Synthesize 25 Hz (40 ms/frame) 14-LED (`[0..6]` left eye, `[7..13]` right eye) eye animations.

    `effect_name` is an expression name (`happy`, `happy.csv`, ...); unknown names get the rainbow VU bounce.
    """
    key = Path(effect_name).stem.lower()
    p = pack_rgbw
    frames: list[list[int]] = []
    steps = EYE_ANIMATION_STEPS

    for step in range(steps):
        phase = step / steps
        left = [0] * 7
        right = [0] * 7

        if key in ("happy", "led_uwb_success"):
            # Warm gold + cyan sparkling smile wave across both 7-LED eyes
            for i in range(7):
                wave = 0.5 + 0.5 * math.sin(2.0 * math.pi * (phase * 2.0 - i / 7.0))
                r = int(40 + 215 * wave)
                g = int(180 + 75 * wave)
                b = int(255 * (1.0 - 0.6 * wave))
                left[i] = p(r, g, b, 12)
                right[6 - i] = p(r, g, b, 12)
        elif key == "wink":
            # Left eye stays bright cyan; right eye smoothly winks shut and pops open with gold sparkle
            for i in range(7):
                left[i] = p(0, 215, 255, 8)
            if 0.22 <= phase <= 0.62:
                # Closed eye center slit in warm gold
                right[3] = p(255, 190, 0, 6)
            else:
                for i in range(7):
                    right[i] = p(0, 215, 255, 8)
        elif key == "heart":
            # Double-heartbeat pulse ("lub-dub") in rose-crimson across both eyes
            p1 = math.exp(-((phase - 0.20) ** 2) / 0.008)
            p2 = math.exp(-((phase - 0.42) ** 2) / 0.010)
            inten = min(1.0, 0.15 + 0.95 * max(p1, p2))
            col = p(int(255 * inten), int(20 * inten), int(95 * inten), int(10 * inten))
            left = [col] * 7
            right = [col] * 7
        elif key in ("cool", "led_uwb_pairing_blue"):
            # Cyber-blue / violet KITT scanner sweep across all 14 eye LEDs
            pos = 6.5 * (0.5 - 0.5 * math.cos(2.0 * math.pi * phase))
            for i in range(7):
                d = max(0.0, 1.0 - abs(i - pos) / 2.2)
                col = p(int(90 * d), int(180 * d), int(40 + 215 * d), int(8 * d))
                left[i] = col
                right[i] = col
        elif key in ("sleepy", "led_daily"):
            # Slow warm-indigo breathing glow on center LEDs
            br = 0.12 + 0.55 * (0.5 - 0.5 * math.cos(2.0 * math.pi * phase))
            for i in range(7):
                edge = 1.0 - 0.22 * abs(i - 3)
                v = br * edge
                col = p(int(95 * v), int(60 * v), int(220 * v), int(6 * v))
                left[i] = col
                right[i] = col
        elif key == "dizzy":
            # Counter-rotating 7-LED spinners on left vs right eye
            idx_l = (step * 2) % 7
            idx_r = (7 - (step * 2) % 7) % 7
            for i in range(7):
                dl = min((i - idx_l) % 7, (idx_l - i) % 7)
                dr = min((i - idx_r) % 7, (idx_r - i) % 7)
                left[i] = p(255, 180, 0, 8) if dl == 0 else (p(0, 180, 120, 0) if dl == 1 else 0)
                right[i] = p(255, 60, 200, 8) if dr == 0 else (p(0, 160, 255, 0) if dr == 1 else 0)
        elif key == "angry":
            # Fiery red-orange rapid warning pulse
            flash = 0.35 + 0.65 * abs(math.sin(4.0 * math.pi * phase))
            for i in range(7):
                r = int(255 * flash)
                g = int(35 * flash) if i in (2, 3, 4) else 0
                left[i] = p(r, g, 0, 0)
                right[i] = p(r, g, 0, 0)
        else:
            # `music_pulse` / default rainbow symmetric VU-meter bounce from eye centers outward
            span = 3.2 * abs(math.sin(2.0 * math.pi * phase * 2.0))
            palette = [
                p(0, 255, 180, 8),
                p(0, 190, 255, 6),
                p(160, 90, 255, 4),
                p(255, 60, 140, 4),
            ]
            for i in range(7):
                dist = abs(i - 3)
                if dist <= span + 0.35:
                    left[i] = palette[min(3, dist)]
                    right[i] = palette[min(3, dist)]

        frames.append(left + right)
    return frames


def led_error(code: str, message: str, hint: str, **details: Any) -> NebiError:
    """LLM-friendly LED error: machine-readable `code`, plain `message`, actionable `hint`."""
    return NebiError(code, message, stage="led", hint=hint, details=details)


def resolve_expression(name: str) -> str:
    """Canonical expression name (`Happy`, `happy.csv`, `love` -> `heart`); raises LED_UNKNOWN_EXPRESSION."""
    key = Path(str(name or "").strip()).stem.lower()
    key = EXPRESSION_ALIASES.get(key, key)
    if key not in EXPRESSIONS:
        raise led_error(
            "LED_UNKNOWN_EXPRESSION",
            f"Unknown eye expression {name!r}.",
            f"Use one of: {', '.join(EXPRESSIONS)} (list_expressions() / GET /api/eyes).",
            expression=name,
        )
    return key


def _check_channel(name: str, value: Any) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError):
        v = -1
    if v < 0 or v > 255 or (isinstance(value, float) and not value.is_integer()):
        raise led_error("LED_BAD_COLOR", f"Colour channel {name}={value!r} is out of range.",
                        "Each of r, g, b, w must be an integer 0..255.", channel=name, value=value)
    return v


def _check_brightness(brightness: Any) -> float:
    try:
        v = float(brightness)
    except (TypeError, ValueError):
        v = math.nan
    if not 0.0 <= v <= 1.0:
        raise led_error("LED_BAD_BRIGHTNESS", f"Brightness {brightness!r} is out of range.",
                        "brightness is a number from 0.0 (dark) to 1.0 (full).", brightness=brightness)
    return v


def scale_pixel(pixel: int, k: float) -> int:
    """Scale every channel of a packed `0xRRGGBBWW` pixel by `k` (0..1)."""
    if k >= 1.0:
        return pixel
    return pack_rgbw(*(round(((pixel >> s) & 0xFF) * k) for s in (24, 16, 8, 0)))


class LedController(DomainController):
    """Eye LEDs: solid colours, animated expressions, release back to the firmware. One background loop at a time."""

    def __init__(self, bus: ZenohTopicBus) -> None:
        super().__init__(bus)
        self._led_seq = 0
        self._led_task: asyncio.Task[None] | None = None
        self._led_frames_sent = 0
        self._led_state: dict[str, Any] = {"mode": "firmware"}
        self._led_until: float | None = None  # monotonic deadline of the current hold/animation (None: until release)
        self._led_last_error = ""
        # Serializes stop-then-start: without it two concurrent calls could both cancel the same old loop and each
        # start a new one, orphaning the first (it would keep the eyes held after `release()`).
        self._led_lock = asyncio.Lock()

    # kept as static aliases for callers of the pre-split API
    _pack_rgbw = staticmethod(pack_rgbw)
    _build_eye_animation = staticmethod(build_eye_animation)

    # --- public API ---

    @staticmethod
    def list_expressions() -> list[dict[str, Any]]:
        """Eye expressions `play_expression` accepts: name, description, seconds per loop."""
        return [{"name": k, "description": d, "loop_s": round(LOOP_SECONDS, 2)} for k, d in EXPRESSIONS.items()]

    def eyes_status(self) -> dict[str, Any]:
        """What the eyes show: `mode` is `firmware` (robot's own look), `color` or `expression`."""
        active = self._led_task is not None and not self._led_task.done()
        st: dict[str, Any] = dict(self._led_state) if active else {"mode": "firmware"}
        left = None
        if active and self._led_until is not None:
            left = round(max(0.0, self._led_until - time.monotonic()), 1)
        st["until_release"] = active and self._led_until is None
        st["seconds_left"] = left
        st["frames_sent"] = self._led_frames_sent
        st["seq"] = self._led_seq
        if self._led_last_error:
            st["last_error"] = self._led_last_error
        return st

    async def set_color(self, r: int, g: int, b: int, w: int = 0, *, hold_sec: float | None = DEFAULT_HOLD_S,
                        brightness: float = 1.0) -> dict[str, Any]:
        """Show a solid colour on both eyes. `hold_sec=None` holds until `release()`, `0` sends a single frame
        (the firmware takes back the eyes a moment later). Black (0,0,0,0) holds the eyes dark."""
        chans = [_check_channel(n, v) for n, v in (("r", r), ("g", g), ("b", b), ("w", w))]
        k = _check_brightness(brightness)
        hold = self._check_hold(hold_sec)
        async with self._led_lock:
            await self._stop_locked()
            scaled = [round(c * k) for c in chans]
            pixels = [pack_rgbw(*scaled)] * LED_COUNT
            self._publish_led_pixels(pixels)
            self._led_state = {"mode": "color", "color": "#{:02x}{:02x}{:02x}".format(*chans[:3]), "rgbw": chans,
                               "brightness": k, "hold_sec": hold}
            if hold is None or hold > 0:
                self._led_until = None if hold is None else time.monotonic() + hold
                self._led_task = asyncio.create_task(self._hold_solid_led(pixels, hold))
            return self.eyes_status() | {"ok": True}

    async def play_expression(self, name: str, loops: int = 1, brightness: float = 1.0) -> dict[str, Any]:
        """Play an animated eye expression (`list_expressions()`); `loops=0` repeats until `release()`.
        When it ends the firmware eyes come back by themselves."""
        key = resolve_expression(name)
        try:
            n = int(loops)
        except (TypeError, ValueError):
            n = -1
        if n < 0 or n > MAX_LOOPS:
            raise led_error("LED_BAD_LOOPS", f"loops={loops!r} is out of range.",
                            f"loops is 1..{MAX_LOOPS}, or 0 to repeat until release.", loops=loops)
        k = _check_brightness(brightness)
        frames = build_eye_animation(key)
        if k < 1.0:
            frames = [[scale_pixel(px, k) for px in f] for f in frames]
        async with self._led_lock:
            await self._stop_locked()
            self._led_state = {"mode": "expression", "expression": key, "loops": n, "brightness": k}
            self._led_until = None if n == 0 else time.monotonic() + n * LOOP_SECONDS
            self._led_task = asyncio.create_task(self._run_led_frames(frames, loops=n))
            return self.eyes_status() | {"ok": True}

    async def release(self) -> dict[str, Any]:
        """Stop sending frames: the robot's own (firmware) eyes come back by themselves."""
        await self.stop()
        return self.eyes_status() | {"ok": True}

    async def stop(self) -> None:
        """Cancel the running colour hold / expression (same as `release()`, without the status reply)."""
        async with self._led_lock:
            await self._stop_locked()

    async def _stop_locked(self) -> None:
        """Cancel the running loop; the caller holds `_led_lock`."""
        if self._led_task is not None and not self._led_task.done():
            self._led_task.cancel()
            try:
                await self._led_task
            except asyncio.CancelledError:
                pass
        self._led_task = None
        self._led_until = None
        self._led_state = {"mode": "firmware"}
        self._led_last_error = ""

    # --- pre-split API (kept for existing callers) ---

    async def set_led_expression(self, csv_effect: str = "happy.csv", play_count: int = 1, action: int = 1) -> int:
        """Play an expression (`action=0` releases). Returns the frame sequence number."""
        if int(action) == 0:
            await self.stop()
            return self._led_seq
        await self.play_expression(csv_effect, loops=max(1, int(play_count)))
        return self._led_seq

    async def set_led_rgbw(self, r: int, g: int, b: int, w: int = 0, hold_sec: float = DEFAULT_HOLD_S) -> int:
        """Solid colour held for `hold_sec` (see `set_color`). Returns the frame sequence number."""
        await self.set_color(r, g, b, w, hold_sec=hold_sec)
        return self._led_seq

    # --- internals ---

    @staticmethod
    def _check_hold(hold_sec: float | None) -> float | None:
        if hold_sec is None:
            return None
        try:
            v = float(hold_sec)
        except (TypeError, ValueError):
            v = math.nan
        if not 0.0 <= v <= MAX_HOLD_S:
            raise led_error("LED_BAD_HOLD", f"hold_sec={hold_sec!r} is out of range.",
                            f"hold_sec is 0..{MAX_HOLD_S:g} seconds, or null to hold until release.", hold_sec=hold_sec)
        return v

    def _publish_led_pixels(self, pixels_14: list[int]) -> int:
        """Publish a single 14-LED `0xRRGGBBWW` frame on `/cmd/led/ctrl_oneway`."""
        self._led_seq = (self._led_seq + 1) & 0xFFFFFFFF
        norm = (list(pixels_14) + [0] * LED_COUNT)[:LED_COUNT]
        seq = self._bus.publish(
            "/cmd/led/ctrl_oneway",
            seq=self._led_seq,
            timestamp_us=int(time.monotonic() * 1_000_000),
            rgbw_data=norm,
        )
        self._led_frames_sent += 1
        return seq

    async def _run_led_frames(self, frames: list[list[int]], loops: int, interval_sec: float = LED_FRAME_INTERVAL_S) -> None:
        """Stream the animation `loops` times (0: forever); frames are paced against a monotonic clock."""
        try:
            t0 = time.monotonic()
            i = 0
            done = 0
            while loops == 0 or done < loops:
                for pixels in frames:
                    self._publish_led_pixels(pixels)
                    i += 1
                    await asyncio.sleep(max(0.0, t0 + i * interval_sec - time.monotonic()))
                done += 1
        except Exception as exc:  # bus closed mid-animation (disconnect): cosmetic, just stop
            self._led_last_error = str(exc)
            logger.debug("LED animation stopped: %s", exc)

    async def _hold_solid_led(self, pixels: list[int], duration_sec: float | None = DEFAULT_HOLD_S) -> None:
        """Re-send the colour at ~5 Hz so the firmware does not take the eyes back (until the deadline, if any)."""
        deadline = None if duration_sec is None else time.monotonic() + duration_sec
        try:
            while deadline is None or time.monotonic() < deadline:
                await asyncio.sleep(LED_HOLD_REFRESH_S if deadline is None else
                                    max(0.0, min(LED_HOLD_REFRESH_S, deadline - time.monotonic())))
                if deadline is not None and time.monotonic() >= deadline:
                    break
                self._publish_led_pixels(pixels)
        except Exception as exc:  # bus closed (disconnect): cosmetic, just stop
            self._led_last_error = str(exc)
            logger.debug("LED hold stopped: %s", exc)
