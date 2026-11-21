"""Gemini Live (on-demand realtime voice + vision) integration for `nebi-ai`.

Designed for **zero idle token consumption**:
  1. Streams Beni's microphone (`/ws/intercom`) and camera (`/ws/video`) locally over LAN (0 API tokens).
  2. Applies a stateful 190-3900 Hz bandpass filter that removes Beni's 171 Hz cooling-fan motor drone
     and 4.36 kHz charger switching whine, then runs local speech-band VAD with a 1.1 s pre-roll buffer.
  3. Connects to `gemini-3.8-live` on demand when speech or chat occurs (~280 ms), sends 1 camera JPEG
     snapshot + explicit `ActivityStart` / speech PCM / `ActivityEnd`, plays Gemini's 24->48 kHz audio on
     Beni's speaker with echo gating, and returns to **Standby (0 tokens/s)** after `live_idle_timeout_sec`.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit, urlunsplit

import numpy as np
import websockets
from google import genai
from google.genai import types
from scipy.signal import butter, sosfilt, sosfilt_zi
from websockets.typing import Origin

from nebi_ai.bridge import RobotBridge
from nebi_ai.config import MemoryStore, PetConfig

if TYPE_CHECKING:
    from nebi_ai.agent import TurnRecord

logger = logging.getLogger(__name__)

_INTERCOM_FRAME_BYTES = 1920  # 20 ms @ 48 kHz s16le mono (960 samples * 2 bytes)
_INTERCOM_FRAME_SEC = 0.02
_STREAM_LEAD_SEC = 0.08
_SPK_IDLE_RELEASE_SEC = 0.14
_ECHO_TAIL_SEC = 0.55
_MIC_BATCH_FRAMES = 3  # 60 ms batches -> 960 samples @ 16 kHz
_PREROLL_CHUNKS = 18  # 18 * 60 ms = 1.08 s pre-roll before VAD trigger
_ONSET_CHUNKS_REQUIRED = 3  # 180 ms of speech energy required to trigger wakeup
_HANGOVER_CHUNKS = 14  # 0.84 s of post-speech quiet before ActivityEnd
_MAX_UTTERANCE_CHUNKS = 400  # 24 s safety cap per single utterance
_VIDEO_SNAPSHOT_COOLDOWN_SEC = 4.0


def pcm48k_to_16k(pcm_s16le: bytes) -> bytes:
    """Anti-aliased 3:1 decimation from 48 kHz s16le mono to 16 kHz s16le mono."""
    even_len = len(pcm_s16le) & ~1
    if even_len < 6:
        return b""
    samples = np.frombuffer(pcm_s16le[:even_len], dtype="<i2")
    trimmed = (samples.size // 3) * 3
    if trimmed == 0:
        return b""
    averaged = samples[:trimmed].astype(np.int32).reshape(-1, 3).mean(axis=1)
    out = np.clip(np.rint(averaged), -32768, 32767).astype("<i2")
    return out.tobytes()


def pcm24k_to_48k(pcm_s16le: bytes) -> bytes:
    """Exact 2x linear-interpolated upsampling from 24 kHz s16le mono to 48 kHz s16le mono."""
    even_len = len(pcm_s16le) & ~1
    if even_len < 2:
        return b""
    samples = np.frombuffer(pcm_s16le[:even_len], dtype="<i2").astype(np.int32)
    out = np.empty(samples.size * 2, dtype="<i2")
    out[0::2] = samples.astype("<i2")
    if samples.size > 1:
        out[1:-1:2] = ((samples[:-1] + samples[1:]) // 2).astype("<i2")
    out[-1] = samples[-1].astype("<i2")
    return out.tobytes()


class SpeechBandFilter:
    """Stateful 190-3900 Hz bandpass + presence boost for 16 kHz robot mic audio.

    Strips Beni's ~171 Hz cooling-fan motor harmonic and ~4.36 kHz charger switching whine
    while boosting speech clarity and returning `(filtered_pcm16k_bytes, rms)`.
    """

    def __init__(self, sample_rate: float = 16000.0, gain: float = 2.2) -> None:
        self._sos = butter(4, [190.0, 3900.0], btype="bandpass", fs=sample_rate, output="sos")
        self._zi = np.zeros_like(sosfilt_zi(self._sos))
        self._gain = gain

    def reset(self) -> None:
        self._zi.fill(0.0)

    def process(self, pcm16k_s16le: bytes) -> tuple[bytes, float]:
        even_len = len(pcm16k_s16le) & ~1
        if even_len < 2:
            return b"", 0.0
        x = np.frombuffer(pcm16k_s16le[:even_len], dtype="<i2").astype(np.float32) / 32768.0
        y, self._zi = sosfilt(self._sos, x, zi=self._zi)
        boosted = np.tanh(y * self._gain) * 0.90
        rms = float(np.sqrt(np.mean(boosted * boosted)))
        pcm_out = np.clip(boosted * 32767.0, -32768, 32767).astype("<i2").tobytes()
        return pcm_out, rms


def _http_to_ws_url(base_url: str, path: str) -> str:
    parts = urlsplit(base_url.rstrip("/"))
    scheme = "wss" if parts.scheme == "https" else "ws"
    return urlunsplit((scheme, parts.netloc, path, "", ""))


def resolve_gemini_api_key(override: str | None = None) -> str:
    """Return the active Gemini/Google API key, or empty string if none is configured."""
    if override and override.strip():
        return override.strip()
    return (os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY") or "").strip()


_LANG_NAMES: dict[str, str] = {
    "en": "English",
    "ru": "Russian",
    "es": "Spanish",
    "fr": "French",
    "de": "German",
    "ja": "Japanese",
    "zh": "Chinese",
    "ko": "Korean",
}

_LANG_BCP47: dict[str, str] = {
    "en": "en-US",
    "ru": "ru-RU",
    "es": "es-ES",
    "fr": "fr-FR",
    "de": "de-DE",
    "ja": "ja-JP",
    "zh": "cmn-CN",
    "ko": "ko-KR",
}

_MOTION_TOOLS: frozenset[str] = frozenset(
    {"robot_stand", "robot_sit", "robot_drive", "robot_turn", "robot_tilt", "robot_body", "robot_dock"}
)

_NON_BLOCKING_TOOLS: frozenset[str] = frozenset(
    {
        "robot_stand",
        "robot_sit",
        "robot_stop",
        "robot_drive",
        "robot_turn",
        "robot_tilt",
        "robot_eyes",
        "robot_body",
        "robot_emote",
        "robot_sound",
        "robot_follow",
        "robot_unfollow",
        "robot_dock",
    }
)


def _resolve_bcp47_lang(code: str) -> str:
    cleaned = (code or "en").strip()
    if "-" in cleaned:
        return cleaned
    return _LANG_BCP47.get(cleaned.lower(), "en-US")


def build_live_system_instruction(cfg: PetConfig, memory_snapshot: str) -> str:
    """Build the system instruction for the realtime Gemini Live pet persona."""
    lang_code = (cfg.listen_language or "en").strip().lower()
    lang_name = _LANG_NAMES.get(lang_code, lang_code or "English")
    wake_clause = (
        f"- **Wake-word mode is ON**: Only respond when addressed by name ({cfg.wake_words}) or in an active follow-up.\n"
        if cfg.wake_word_enabled
        else "- **Open-mic mode is ON**: Whenever the human speaks to you, reply warmly and perform any requested action!\n"
    )
    mem_excerpt = memory_snapshot.strip()[:3000] if memory_snapshot.strip() else "(empty)"
    return (
        f"You are {cfg.pet_name}, a small two-wheeled-legged self-balancing pet robot (Beni / Nebi LZ110) "
        "with a gimbal camera, LED eyes, expressive body gestures, and a speaker.\n\n"
        f"## Personality\n{cfg.personality}\n\n"
        "## Realtime Voice, Vision & Tools\n"
        f"- **Language**: ALWAYS respond in **{lang_name}** (and interpret noisy/muffled speech as {lang_name} unless the user explicitly asks to speak another language).\n"
        "- A camera snapshot from your head camera is attached automatically when the human speaks; call `robot_look` "
        "anytime you want a fresh camera image (for example after turning or tilting your head).\n"
        "- Keep spoken replies short, lively, and pet-like (1-2 short sentences) and pair them with "
        "`robot_eyes` or `robot_body(silent=True)` gestures so you feel alive!\n"
        "- **Do NOT interrupt your own voice**: never call `robot_sound` or `robot_emote` during normal replies "
        "(they share the robot speaker and cut off your spoken voice). Use `robot_eyes` and `robot_body(silent=True)` "
        "instead, and only call `robot_sound` if the human explicitly asks for a sound effect (like meow or horn).\n"
        f"{wake_clause}"
        f"- **Safety**: Check your camera view before driving, keep each `robot_drive` <= {cfg.max_drive_m:.2f} m, "
        "never drive near stairs/edges, and sit or dock if battery is low.\n"
        "- **DeepAgent Brain (`ask_deep_agent`)**: Call `ask_deep_agent(task=...)` whenever you need to save or read "
        "persistent memories (`/MEMORY.md`), run health diagnostics, or execute complex multi-step plans.\n\n"
        f"## Current Persistent Memory (`/MEMORY.md`)\n{mem_excerpt}\n"
    )


def build_live_tool_declarations() -> list[Any]:
    """Declare fast low-latency robot tools + `ask_deep_agent` delegation for Gemini Live."""
    decls = [
        types.FunctionDeclaration(
            name="robot_look",
            description="Capture a fresh camera frame and inspect detected people/pets right now.",
            parameters_json_schema={"type": "object", "properties": {}},
        ),
        types.FunctionDeclaration(
            name="robot_stand",
            description="Stand up and self-balance on two wheels so you can move, turn, or gesture.",
            parameters_json_schema={"type": "object", "properties": {}},
        ),
        types.FunctionDeclaration(
            name="robot_sit",
            description="Sit / crouch down safely on the floor.",
            parameters_json_schema={"type": "object", "properties": {}},
        ),
        types.FunctionDeclaration(
            name="robot_stop",
            description="Immediately stop all wheel movement and target following.",
            parameters_json_schema={"type": "object", "properties": {}},
        ),
        types.FunctionDeclaration(
            name="robot_drive",
            description="Drive forward, backward, left, or right a short, safe distance in meters.",
            parameters_json_schema={
                "type": "object",
                "properties": {
                    "direction": {"type": "string", "enum": ["forward", "backward", "left", "right"]},
                    "distance_m": {"type": "number", "description": "Distance in meters (e.g. 0.3)."},
                    "speed": {"type": "number", "description": "Speed 0.1 to 0.8 (default 0.3)."},
                },
                "required": ["direction"],
            },
        ),
        types.FunctionDeclaration(
            name="robot_turn",
            description="Turn left, right, or around in place by degrees (1-360).",
            parameters_json_schema={
                "type": "object",
                "properties": {
                    "direction": {"type": "string", "enum": ["left", "right", "around"]},
                    "degrees": {"type": "number", "description": "Angle in degrees (e.g. 45 or 90)."},
                    "speed": {"type": "number", "description": "Turn speed 0.2 to 1.0 (default 0.5)."},
                },
                "required": ["direction"],
            },
        ),
        types.FunctionDeclaration(
            name="robot_tilt",
            description="Tilt your head/camera gimbal ('up', 'down', or 'center').",
            parameters_json_schema={
                "type": "object",
                "properties": {
                    "direction": {"type": "string", "enum": ["up", "down", "center"]},
                    "duration_s": {"type": "number", "description": "Seconds to tilt (0.1 to 2.0, default 0.5)."},
                },
                "required": ["direction"],
            },
        ),
        types.FunctionDeclaration(
            name="robot_eyes",
            description=(
                "Set your LED eyes to an expression or color ('happy', 'love', 'wink', "
                "'sleepy', 'dizzy', 'cool', 'angry', 'music', 'cyan', 'pink', 'gold', 'green', 'auto')."
            ),
            parameters_json_schema={
                "type": "object",
                "properties": {
                    "what": {"type": "string"},
                    "hold": {"type": "number", "description": "Seconds to hold expression (default 5.0)."},
                },
                "required": ["what"],
            },
        ),
        types.FunctionDeclaration(
            name="robot_body",
            description=(
                "Perform an expressive physical pet gesture ('nod', 'shake', 'perk', 'cower', "
                "'wiggle', 'curious', 'sleep', 'wake', 'look-at'). Note: wiggle/perk/cower require standing first."
            ),
            parameters_json_schema={
                "type": "object",
                "properties": {
                    "gesture": {"type": "string"},
                    "silent": {"type": "boolean", "description": "Keep true when speaking with Gemini Live voice."},
                },
                "required": ["gesture"],
            },
        ),
        types.FunctionDeclaration(
            name="robot_emote",
            description="Play an expressive pet sound + eye animation ('happy', 'curious', 'love', 'sad', 'celebrate', 'greet').",
            parameters_json_schema={
                "type": "object",
                "properties": {"emotion": {"type": "string", "enum": ["happy", "curious", "love", "sad", "celebrate", "greet"]}},
                "required": ["emotion"],
            },
        ),
        types.FunctionDeclaration(
            name="robot_sound",
            description="Play a built-in pet sound effect ('greet', 'meow', 'chirp', 'shutter', 'victory', 'curious', 'purr', 'horn').",
            parameters_json_schema={
                "type": "object",
                "properties": {"sound_id": {"type": "string", "enum": ["greet", "meow", "chirp", "shutter", "victory", "curious", "purr", "horn"]}},
                "required": ["sound_id"],
            },
        ),
        types.FunctionDeclaration(
            name="robot_state",
            description="Check your battery percentage, charging status, posture, IMU tilt, and visible people/pets.",
            parameters_json_schema={"type": "object", "properties": {}},
        ),
        types.FunctionDeclaration(
            name="robot_follow",
            description="Visually follow the visible person/pet using a follow style ('trail', 'parallel', 'lead', 'orbit', 'spotlight').",
            parameters_json_schema={
                "type": "object",
                "properties": {
                    "style": {"type": "string", "enum": ["trail", "parallel", "lead", "orbit", "spotlight"]},
                    "distance_m": {"type": "number", "description": "Optional follow distance in meters (e.g. 1.5)."},
                },
            },
        ),
        types.FunctionDeclaration(
            name="robot_unfollow",
            description="Stop visually tracking or following targets.",
            parameters_json_schema={"type": "object", "properties": {}},
        ),
        types.FunctionDeclaration(
            name="robot_dock",
            description="Drive onto and dock with a visible charging station.",
            parameters_json_schema={"type": "object", "properties": {}},
        ),
        types.FunctionDeclaration(
            name="ask_deep_agent",
            description=(
                "Delegate a task to your DeepAgents long-term memory & planning brain — use this to remember "
                "new facts/names in `/MEMORY.md`, inspect memory files, run full health diagnostics, or execute "
                "multi-step robot plans."
            ),
            parameters_json_schema={
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "Clear instruction for the DeepAgent (e.g. 'Save in /MEMORY.md that Nickolay likes coffee').",
                    }
                },
                "required": ["task"],
            },
        ),
    ]
    for decl in decls:
        if decl.name in _NON_BLOCKING_TOOLS:
            decl.behavior = types.Behavior.NON_BLOCKING
    return [types.Tool(function_declarations=decls)]


class GeminiLiveManager:
    """Manages on-demand, zero-idle-token Gemini 3.8 Live voice & camera interaction for Beni."""

    def __init__(
        self,
        bridge: RobotBridge,
        memory_store: MemoryStore,
        *,
        get_config: Callable[[], PetConfig],
        get_api_key: Callable[[], str | None],
        delegate_deep_agent: Callable[[str], Awaitable[TurnRecord]],
        on_turn: Callable[[dict[str, Any]], Awaitable[None]],
        on_event: Callable[[str, dict[str, Any]], None],
        on_tokens: Callable[[int], None] | None = None,
    ) -> None:
        self.bridge = bridge
        self.memory_store = memory_store
        self._get_config = get_config
        self._get_api_key = get_api_key
        self._delegate_deep_agent = delegate_deep_agent
        self._on_turn = on_turn
        self._on_event = on_event
        self._on_tokens = on_tokens

        self._tasks: list[asyncio.Task[None]] = []
        self._stop_event = asyncio.Event()
        self._reload_event = asyncio.Event()
        self._wake_event = asyncio.Event()

        self._mic_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=60)
        self._spk_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=400)
        self._activity_queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue(maxsize=300)
        self._text_queue: asyncio.Queue[str] = asyncio.Queue(maxsize=20)
        self._raw_mic_taps: set[asyncio.Queue[bytes]] = set()

        self._flush_epoch: int = 0
        self._playing: bool = False
        self._motor_busy: bool = False
        self._user_speaking: bool = False
        self._echo_mute_until: float = 0.0
        self._last_activity_at: float = 0.0
        self._last_video_sent_at: float = 0.0
        self._spk_idle_event = asyncio.Event()
        self._spk_idle_event.set()

        self.connected: bool = False
        self.mode: str = "standby"  # "off" | "standby" | "connecting" | "listening" | "thinking" | "warm"
        self.current_mic_rms: float = 0.0
        self.noise_floor_rms: float = 0.012
        self.live_tokens_used: int = 0
        self.last_turn_tokens: int = 0
        self.last_error: str | None = None

    def _is_ai_live_active(self, cfg: PetConfig) -> bool:
        return bool(cfg.autonomy_enabled and cfg.live_enabled)

    def status_dict(self) -> dict[str, Any]:
        cfg = self._get_config()
        if not cfg.autonomy_enabled:
            mode = "paused"
        elif not cfg.live_enabled:
            mode = "off"
        else:
            mode = self.mode
        return {
            "connected": self.connected,
            "mode": mode,
            "idle_token_saver": cfg.idle_token_saver,
            "model": cfg.live_model,
            "mic_rms": round(self.current_mic_rms, 4),
            "noise_floor_rms": round(self.noise_floor_rms, 4),
            "live_tokens": self.live_tokens_used,
            "last_turn_tokens": self.last_turn_tokens,
            "error": self.last_error,
        }

    def _set_mode(self, mode: str) -> None:
        if self.mode != mode:
            self.mode = mode
            self._on_event("live_state", self.status_dict())

    def start(self) -> None:
        self._stop_event.clear()
        if any(not t.done() for t in self._tasks):
            return
        self._tasks = [
            asyncio.create_task(self._intercom_ws_worker(), name="nebi-ai-intercom"),
            asyncio.create_task(self._video_cache_worker(), name="nebi-ai-video-cache"),
            asyncio.create_task(self._vad_worker(), name="nebi-ai-vad"),
            asyncio.create_task(self._live_session_supervisor(), name="nebi-ai-gemini-live"),
        ]

    async def stop(self) -> None:
        self._stop_event.set()
        self._reload_event.set()
        self._wake_event.set()
        self.flush_speaker()
        for t in self._tasks:
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        self.connected = False
        self.mode = "standby"

    def notify_config_changed(self) -> None:
        """Reset any warm Live WebSocket so updated model/voice/personality/memory apply on next utterance."""
        self._reload_event.set()
        while not self._activity_queue.empty():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._activity_queue.get_nowait()

    def flush_speaker(self) -> None:
        """Immediately discard queued Gemini downlink audio (used on barge-in or emergency STOP)."""
        self._flush_epoch += 1
        while not self._spk_queue.empty():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._spk_queue.get_nowait()

    async def send_text(self, message: str) -> bool:
        """Send a typed chat message into Gemini Live (waking it from Standby if needed)."""
        cleaned = message.strip()
        cfg = self._get_config()
        if not cleaned or not self._is_ai_live_active(cfg) or not resolve_gemini_api_key(self._get_api_key()):
            return False
        try:
            self._text_queue.put_nowait(cleaned)
            self._last_activity_at = time.monotonic()
            self._wake_event.set()
            return True
        except asyncio.QueueFull:
            return False

    async def record_diagnostic_clip(self, seconds: float = 5.0) -> dict[str, Any]:
        """Tap the live robot mic for `seconds`, save raw 48 kHz + filtered 16 kHz WAVs, and return metadata."""
        dur = max(1.0, min(15.0, float(seconds)))
        tap_q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=int(dur / _INTERCOM_FRAME_SEC) + 50)
        self._raw_mic_taps.add(tap_q)
        chunks: list[bytes] = []
        deadline = time.monotonic() + dur
        try:
            while time.monotonic() < deadline:
                rem = max(0.05, deadline - time.monotonic())
                try:
                    frame = await asyncio.wait_for(tap_q.get(), timeout=min(0.5, rem))
                    chunks.append(frame)
                except TimeoutError:
                    continue
        finally:
            self._raw_mic_taps.discard(tap_q)

        raw48 = b"".join(chunks)
        if not raw48:
            return {"ok": False, "error": "No microphone audio received from robot (/ws/intercom)"}

        pcm16_raw = pcm48k_to_16k(raw48)
        filt = SpeechBandFilter()
        pcm16_clean, rms_clean = filt.process(pcm16_raw)
        arr48 = np.frombuffer(raw48[: len(raw48) & ~1], dtype="<i2").astype(np.float32) / 32768.0
        rms_raw = float(np.sqrt(np.mean(arr48 * arr48))) if arr48.size else 0.0

        ts = time.strftime("%H%M%S")
        raw_path = await asyncio.to_thread(
            self.memory_store.save_wav_clip, f"test_{ts}_raw48k.wav", raw48, sample_rate=48000
        )
        clean_path = await asyncio.to_thread(
            self.memory_store.save_wav_clip, f"test_{ts}_filtered16k.wav", pcm16_clean, sample_rate=16000
        )
        info = {
            "ok": True,
            "seconds": round(len(raw48) / (48000 * 2), 2),
            "raw_rms": round(rms_raw, 4),
            "filtered_rms": round(rms_clean, 4),
            "raw_clip": raw_path.name,
            "filtered_clip": clean_path.name,
        }
        self._on_event("clip_saved", info)
        return info

    @property
    def is_playing(self) -> bool:
        return self._playing or not self._spk_queue.empty()

    def _is_mic_muted_for_echo(self) -> bool:
        return self.is_playing or self._motor_busy or self.bridge.speaking or time.monotonic() < self._echo_mute_until

    async def _wait_speaker_idle(self, timeout: float = 2.5) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._spk_idle_event.wait(), timeout=timeout)

    async def _execute_live_tool(
        self,
        name: str,
        args: dict[str, Any],
        live_session: Any = None,
    ) -> tuple[dict[str, Any], bool]:
        """Execute a Gemini Live tool call and return `(result_dict, ok_bool)`."""
        cfg = self._get_config()

        if name == "robot_look":
            res = await self.bridge.call("look", {"annotate": True})
            jpg = await asyncio.to_thread(self.bridge.read_last_snapshot_bytes)
            if jpg and live_session is not None:
                await live_session.send_realtime_input(video=types.Blob(data=jpg, mime_type="image/jpeg"))
                self._last_video_sent_at = time.monotonic()
            return res, bool(res.get("ok", True))

        if name == "ask_deep_agent":
            task_text = str(args.get("task") or "").strip()
            if not task_text:
                return {"ok": False, "error": "Empty task"}, False
            rec = await self._delegate_deep_agent(f"[Delegated from Gemini Live]: {task_text}")
            out: dict[str, Any] = {
                "ok": rec.error is None,
                "reply": rec.reply,
                "tool_calls": rec.tool_calls,
            }
            if rec.error:
                out["error"] = rec.error
            return out, rec.error is None

        if name in ("robot_emote", "robot_sound"):
            if not cfg.voice_enabled:
                return {"ok": False, "error": "Robot voice/sound is disabled in Parameters"}, False
            await self._wait_speaker_idle()

        def _parse_speed(val: Any, default: float) -> float:
            if isinstance(val, (int, float)):
                return max(0.1, min(1.0, float(val)))
            s = str(val or "").strip().lower()
            if s == "slow":
                return 0.2
            if s == "fast":
                return 0.6
            if s == "normal":
                return default
            try:
                return max(0.1, min(1.0, float(s)))
            except ValueError:
                return default

        raw_dir = str(args.get("direction", "forward")).strip().lower()
        drive_dir = "backward" if raw_dir == "back" else raw_dir
        follow_payload: dict[str, Any] = {"style": str(args.get("style") or "trail")}
        if args.get("distance_m") is not None:
            follow_payload["distance"] = max(0.5, min(5.0, float(args["distance_m"])))

        cmd_map: dict[str, tuple[str, dict[str, Any]]] = {
            "robot_stand": ("stand", {}),
            "robot_sit": ("sit", {}),
            "robot_stop": ("stop", {}),
            "robot_state": ("state", {}),
            "robot_unfollow": ("unfollow", {}),
            "robot_dock": ("dock", {}),
            "robot_drive": (
                "drive",
                {
                    "direction": drive_dir,
                    "distance": max(0.05, min(cfg.max_drive_m, float(args.get("distance_m", 0.3)))),
                    "speed": _parse_speed(args.get("speed"), 0.3),
                },
            ),
            "robot_turn": (
                "turn",
                {
                    "direction": str(args.get("direction", "left")),
                    "degrees": max(1.0, min(360.0, float(args.get("degrees", 45.0)))),
                    "speed": _parse_speed(args.get("speed"), 0.5),
                },
            ),
            "robot_tilt": (
                "tilt",
                {
                    "direction": str(args.get("direction", "center")),
                    "for": max(0.1, min(2.0, float(args.get("duration_s", args.get("amount", 0.5))))),
                },
            ),
            "robot_eyes": (
                "eyes",
                {
                    "what": str(args.get("what", "happy")),
                    "hold": max(0.5, min(60.0, float(args.get("hold", 5.0)))),
                },
            ),
            "robot_body": (
                "body",
                {
                    "gesture": str(args.get("gesture", "nod")),
                    "silent": bool(args.get("silent", True)),
                },
            ),
            "robot_emote": ("emote", {"emotion": str(args.get("emotion", "happy"))}),
            "robot_sound": ("sound", {"name": str(args.get("sound_id", "chirp"))}),
            "robot_follow": ("follow", follow_payload),
        }
        if name not in cmd_map:
            return {"ok": False, "error": f"Unknown Live tool: {name}"}, False

        cmd, payload = cmd_map[name]
        is_motion = name in _MOTION_TOOLS
        if is_motion:
            self._motor_busy = True
        try:
            res = await self.bridge.call(cmd, payload)
        except Exception as exc:
            logger.warning("Live tool %s failed: %s", name, exc)
            return {"ok": False, "error": str(exc)}, False
        finally:
            if is_motion:
                self._motor_busy = False
                self._echo_mute_until = max(self._echo_mute_until, time.monotonic() + 0.70)
        return res, bool(res.get("ok", True))

    # ------------------------------------------------------------------ local LAN media workers (0 tokens)

    def _enqueue_mic_frame(self, pcm48k: bytes) -> None:
        for tap_q in list(self._raw_mic_taps):
            with contextlib.suppress(asyncio.QueueFull):
                tap_q.put_nowait(pcm48k)
        if self._mic_queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._mic_queue.get_nowait()
        self._mic_queue.put_nowait(pcm48k)

    async def _intercom_ws_worker(self) -> None:
        """Maintain `/ws/intercom` (or in-process `Intercom`) for local VAD mic + speaker playback."""
        if not self.bridge.is_remote:
            await self._intercom_inproc_worker()
            return

        base_url = self.bridge.target.daemon_url.rstrip("/")
        ws_url = _http_to_ws_url(base_url, "/ws/intercom")

        while not self._stop_event.is_set():
            cfg = self._get_config()
            if not cfg.live_enabled and not self._raw_mic_taps:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop_event.wait(), timeout=1.0)
                continue
            try:
                async with websockets.connect(
                    ws_url,
                    origin=Origin(base_url),
                    max_size=2 * 1024 * 1024,
                    ping_interval=20.0,
                ) as ws:
                    await ws.send(json.dumps({"type": "start"}))
                    rx_task = asyncio.create_task(self._ws_intercom_rx(ws))
                    tx_task = asyncio.create_task(self._ws_intercom_tx(ws))
                    stop_task = asyncio.create_task(self._stop_event.wait())
                    try:
                        await asyncio.wait({rx_task, tx_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
                    finally:
                        for t in (rx_task, tx_task, stop_task):
                            t.cancel()
                        with contextlib.suppress(Exception):
                            await ws.send(json.dumps({"type": "talk", "on": False}))
                            await ws.send(json.dumps({"type": "stop"}))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("Live /ws/intercom link disconnected (%s); retrying in 2s", exc)
                self._playing = False
                self._spk_idle_event.set()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop_event.wait(), timeout=2.0)

    async def _ws_intercom_rx(self, ws: Any) -> None:
        while not self._stop_event.is_set():
            if not self._get_config().live_enabled and not self._raw_mic_taps:
                return
            msg = await ws.recv()
            if isinstance(msg, bytes) and msg:
                self._enqueue_mic_frame(msg)

    async def _ws_intercom_tx(self, ws: Any) -> None:
        buf = bytearray()
        talking = False
        next_deadline = 0.0
        seen_epoch = self._flush_epoch

        try:
            while not self._stop_event.is_set():
                if self._flush_epoch != seen_epoch:
                    seen_epoch = self._flush_epoch
                    buf.clear()
                    if talking:
                        await ws.send(json.dumps({"type": "talk", "on": False}))
                        talking = False
                        self._playing = False
                        self._echo_mute_until = time.monotonic() + _ECHO_TAIL_SEC
                        self._spk_idle_event.set()

                if len(buf) < _INTERCOM_FRAME_BYTES:
                    try:
                        chunk = await asyncio.wait_for(
                            self._spk_queue.get(),
                            timeout=_SPK_IDLE_RELEASE_SEC if talking else 0.5,
                        )
                        if self._flush_epoch != seen_epoch:
                            continue
                        buf.extend(chunk)
                        while not self._spk_queue.empty() and len(buf) < _INTERCOM_FRAME_BYTES * 25:
                            buf.extend(self._spk_queue.get_nowait())
                    except TimeoutError:
                        if talking:
                            if buf:
                                buf.extend(b"\x00" * (_INTERCOM_FRAME_BYTES - len(buf)))
                                await ws.send(bytes(buf[:_INTERCOM_FRAME_BYTES]))
                                buf.clear()
                            await ws.send(json.dumps({"type": "talk", "on": False}))
                            talking = False
                            self._playing = False
                            self._echo_mute_until = time.monotonic() + _ECHO_TAIL_SEC
                            self._spk_idle_event.set()
                        continue

                if not buf:
                    continue
                if len(buf) < _INTERCOM_FRAME_BYTES and self._spk_queue.empty():
                    buf.extend(b"\x00" * (_INTERCOM_FRAME_BYTES - len(buf)))

                if not talking:
                    await ws.send(json.dumps({"type": "talk", "on": True}))
                    talking = True
                    self._playing = True
                    self._spk_idle_event.clear()
                    next_deadline = time.monotonic() - _STREAM_LEAD_SEC

                frame = bytes(buf[:_INTERCOM_FRAME_BYTES])
                del buf[:_INTERCOM_FRAME_BYTES]
                now = time.monotonic()
                if next_deadline > now:
                    await asyncio.sleep(next_deadline - now)
                next_deadline = max(next_deadline + _INTERCOM_FRAME_SEC, time.monotonic() - _STREAM_LEAD_SEC)
                await ws.send(frame)
        finally:
            self._playing = False
            self._spk_idle_event.set()

    async def _intercom_inproc_worker(self) -> None:
        while not self._stop_event.is_set():
            cfg = self._get_config()
            mgr = self.bridge._manager
            client = getattr(mgr, "client", None) if mgr is not None else None
            if (not cfg.live_enabled and not self._raw_mic_taps) or client is None or not getattr(client, "connected", False):
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop_event.wait(), timeout=1.5)
                continue

            unsub = client.intercom.add_pcm_listener(self._enqueue_mic_frame)
            try:
                if not client.intercom.active:
                    await client.intercom.start()
                buf = bytearray()
                talking = False
                next_deadline = 0.0
                seen_epoch = self._flush_epoch

                while not self._stop_event.is_set() and client.connected and (
                    self._get_config().live_enabled or self._raw_mic_taps
                ):
                    if self._flush_epoch != seen_epoch:
                        seen_epoch = self._flush_epoch
                        buf.clear()
                        talking = False
                        self._playing = False
                        self._echo_mute_until = time.monotonic() + _ECHO_TAIL_SEC
                        self._spk_idle_event.set()

                    if len(buf) < _INTERCOM_FRAME_BYTES:
                        try:
                            chunk = await asyncio.wait_for(
                                self._spk_queue.get(),
                                timeout=_SPK_IDLE_RELEASE_SEC if talking else 0.5,
                            )
                            if self._flush_epoch != seen_epoch:
                                continue
                            buf.extend(chunk)
                        except TimeoutError:
                            if talking:
                                if buf:
                                    buf.extend(b"\x00" * (_INTERCOM_FRAME_BYTES - len(buf)))
                                    client.intercom.send_pcm(bytes(buf[:_INTERCOM_FRAME_BYTES]))
                                    buf.clear()
                                talking = False
                                self._playing = False
                                self._echo_mute_until = time.monotonic() + _ECHO_TAIL_SEC
                                self._spk_idle_event.set()
                            continue

                    if not buf:
                        continue
                    if len(buf) < _INTERCOM_FRAME_BYTES and self._spk_queue.empty():
                        buf.extend(b"\x00" * (_INTERCOM_FRAME_BYTES - len(buf)))

                    if not talking:
                        client.intercom.new_talk_session()
                        talking = True
                        self._playing = True
                        self._spk_idle_event.clear()
                        next_deadline = time.monotonic() - _STREAM_LEAD_SEC

                    frame = bytes(buf[:_INTERCOM_FRAME_BYTES])
                    del buf[:_INTERCOM_FRAME_BYTES]
                    now = time.monotonic()
                    if next_deadline > now:
                        await asyncio.sleep(next_deadline - now)
                    next_deadline = max(next_deadline + _INTERCOM_FRAME_SEC, time.monotonic() - _STREAM_LEAD_SEC)
                    client.intercom.send_pcm(frame)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("In-process intercom loop error: %s", exc)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop_event.wait(), timeout=2.0)
            finally:
                unsub()
                self._playing = False
                self._spk_idle_event.set()

    async def _video_cache_worker(self) -> None:
        """Cache live camera JPEGs locally in RAM (0 API tokens); snapshots are sent only on speech/demand."""
        if not self.bridge.is_remote:
            while not self._stop_event.is_set():
                mgr = self.bridge._manager
                hub = getattr(mgr, "video_hub", None) if mgr is not None else None
                if hub is None:
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(self._stop_event.wait(), timeout=2.0)
                    continue
                if hasattr(mgr, "request_keyframe"):
                    mgr.request_keyframe()
                q = hub.subscribe_jpeg()
                try:
                    while not self._stop_event.is_set():
                        try:
                            jpg = await asyncio.wait_for(q.get(), timeout=1.0)
                            self.bridge.set_latest_jpeg(jpg)
                        except TimeoutError:
                            continue
                finally:
                    hub.unsubscribe_jpeg(q)
            return

        base_url = self.bridge.target.daemon_url.rstrip("/")
        ws_url = _http_to_ws_url(base_url, "/ws/video")
        while not self._stop_event.is_set():
            try:
                async with websockets.connect(
                    ws_url,
                    origin=Origin(base_url),
                    max_size=8 * 1024 * 1024,
                    ping_interval=20.0,
                ) as ws:
                    while not self._stop_event.is_set():
                        msg = await ws.recv()
                        if isinstance(msg, bytes) and msg.startswith(b"\xff\xd8"):
                            self.bridge.set_latest_jpeg(msg)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("Video cache /ws/video disconnected (%s); retrying in 2s", exc)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop_event.wait(), timeout=2.0)

    # ------------------------------------------------------------------ local VAD (0 tokens when quiet)

    def _push_activity(self, kind: str, payload: Any) -> None:
        if self._activity_queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._activity_queue.get_nowait()
        self._activity_queue.put_nowait((kind, payload))

    async def _vad_worker(self) -> None:
        """Filter robot mic audio and emit `speech_start` / `speech_chunk` / `speech_end` locally."""
        batch = bytearray()
        target_bytes = _INTERCOM_FRAME_BYTES * _MIC_BATCH_FRAMES
        speech_filter = SpeechBandFilter()
        preroll: collections.deque[bytes] = collections.deque(maxlen=_PREROLL_CHUNKS)
        utterance_buf = bytearray()
        in_speech = False
        onset_hits = 0
        quiet_chunks = 0
        speech_chunks = 0

        async def close_utterance(save_clip: bool) -> None:
            nonlocal in_speech, onset_hits, quiet_chunks, speech_chunks
            if not in_speech:
                return
            in_speech = False
            self._user_speaking = False
            onset_hits = 0
            quiet_chunks = 0
            speech_chunks = 0
            self._push_activity("speech_end", None)
            captured = bytes(utterance_buf)
            utterance_buf.clear()
            if save_clip and len(captured) >= 16000:  # >= 0.5 s
                ts = time.strftime("%H%M%S")
                path = await asyncio.to_thread(
                    self.memory_store.save_wav_clip, f"heard_{ts}_16k.wav", captured, sample_rate=16000
                )
                self._on_event("clip_saved", {"name": path.name})

        while not self._stop_event.is_set():
            try:
                frame = await asyncio.wait_for(self._mic_queue.get(), timeout=0.25)
            except TimeoutError:
                continue

            batch.extend(frame)
            if len(batch) < target_bytes:
                continue

            raw48 = bytes(batch)
            batch.clear()
            cfg = self._get_config()
            if not self._is_ai_live_active(cfg) or not cfg.listen_enabled or cfg.is_quiet_now():
                await close_utterance(save_clip=False)
                preroll.clear()
                continue

            pcm16_raw = pcm48k_to_16k(raw48)
            if not pcm16_raw:
                continue

            if self._is_mic_muted_for_echo():
                await close_utterance(save_clip=False)
                preroll.clear()
                speech_filter.reset()
                self.current_mic_rms = 0.0
                continue

            # Measure speech-band RMS on a side copy for reliable VAD, but send 100% raw unfiltered
            # 16 kHz PCM to Gemini Live (no noise-cancellation / bandpass distortion on voice).
            _, rms = speech_filter.process(pcm16_raw)
            pcm16 = pcm16_raw
            self.current_mic_rms = rms
            thresh = max(cfg.live_vad_rms, self.noise_floor_rms * 2.5)

            if not in_speech:
                preroll.append(pcm16)
                if rms < thresh:
                    self.noise_floor_rms = max(0.005, min(0.035, 0.94 * self.noise_floor_rms + 0.06 * rms))
                if rms >= thresh:
                    onset_hits += 1
                    if onset_hits >= _ONSET_CHUNKS_REQUIRED:
                        in_speech = True
                        self._user_speaking = True
                        onset_hits = 0
                        quiet_chunks = 0
                        speech_chunks = len(preroll)
                        pre_list = list(preroll)
                        utterance_buf.clear()
                        for ch in pre_list:
                            utterance_buf.extend(ch)
                        self._last_activity_at = time.monotonic()
                        self._push_activity("speech_start", pre_list)
                        self._wake_event.set()
                else:
                    onset_hits = 0
            else:
                self._last_activity_at = time.monotonic()
                speech_chunks += 1
                utterance_buf.extend(pcm16)
                self._push_activity("speech_chunk", pcm16)
                if rms < thresh * 0.72:
                    quiet_chunks += 1
                else:
                    quiet_chunks = 0
                if quiet_chunks >= _HANGOVER_CHUNKS or speech_chunks >= _MAX_UTTERANCE_CHUNKS:
                    await close_utterance(save_clip=True)

    # ------------------------------------------------------------------ on-demand Gemini Live session

    async def _maybe_send_camera_snapshot(self, live_session: Any) -> None:
        now = time.monotonic()
        if now - self._last_video_sent_at < _VIDEO_SNAPSHOT_COOLDOWN_SEC:
            return
        jpg = await asyncio.to_thread(self.bridge.read_last_snapshot_bytes)
        if jpg:
            await live_session.send_realtime_input(video=types.Blob(data=jpg, mime_type="image/jpeg"))
            self._last_video_sent_at = now

    async def _uplink_worker(
        self,
        live_session: Any,
        session_stop: asyncio.Event,
        pending_prompts: list[str],
    ) -> None:
        """Forward VAD-gated speech bursts + single camera snapshots + typed chats to Gemini Live."""
        activity_open = False
        while not session_stop.is_set():
            if not self._text_queue.empty():
                text = self._text_queue.get_nowait()
                self._last_activity_at = time.monotonic()
                self._set_mode("thinking")
                pending_prompts.append(text)
                if activity_open:
                    await live_session.send_realtime_input(activity_end=types.ActivityEnd())
                    activity_open = False
                await self._maybe_send_camera_snapshot(live_session)
                await live_session.send_client_content(
                    turns=types.Content(role="user", parts=[types.Part(text=f"[Web UI chat]: {text}")]),
                    turn_complete=True,
                )
                continue

            try:
                kind, payload = await asyncio.wait_for(self._activity_queue.get(), timeout=0.15)
            except TimeoutError:
                continue

            self._last_activity_at = time.monotonic()
            if kind == "speech_start":
                self._set_mode("listening")
                await self._maybe_send_camera_snapshot(live_session)
                if not activity_open:
                    await live_session.send_realtime_input(activity_start=types.ActivityStart())
                    activity_open = True
                for chunk in payload:
                    await live_session.send_realtime_input(
                        audio=types.Blob(data=chunk, mime_type="audio/pcm;rate=16000")
                    )
            elif kind == "speech_chunk":
                if not activity_open:
                    await live_session.send_realtime_input(activity_start=types.ActivityStart())
                    activity_open = True
                await live_session.send_realtime_input(
                    audio=types.Blob(data=payload, mime_type="audio/pcm;rate=16000")
                )
            elif kind == "speech_end" and activity_open:
                self._set_mode("thinking")
                await live_session.send_realtime_input(activity_end=types.ActivityEnd())
                activity_open = False

    async def _continuous_video_worker(self, live_session: Any, session_stop: asyncio.Event) -> None:
        """Stream live camera JPEGs continuously at `live_video_fps` when Full Live mode is active."""
        while not session_stop.is_set():
            cfg = self._get_config()
            interval = 1.0 / max(0.1, cfg.live_video_fps)
            if not cfg.is_quiet_now():
                jpg = await asyncio.to_thread(self.bridge.read_last_snapshot_bytes)
                if jpg:
                    await live_session.send_realtime_input(video=types.Blob(data=jpg, mime_type="image/jpeg"))
                    self._last_video_sent_at = time.monotonic()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(session_stop.wait(), timeout=interval)

    async def _idle_watchdog(self, session_stop: asyncio.Event) -> None:
        """Close the Gemini Live WebSocket back to Standby after `live_idle_timeout_sec` of quiet."""
        while not session_stop.is_set():
            await asyncio.sleep(1.0)
            timeout_s = self._get_config().live_idle_timeout_sec
            if (
                not self._user_speaking
                and not self.is_playing
                and self._activity_queue.empty()
                and self._text_queue.empty()
                and (time.monotonic() - self._last_activity_at) >= timeout_s
            ):
                logger.info("Gemini Live idle for %.0fs — entering zero-token Standby", timeout_s)
                session_stop.set()
                return

    async def _receive_worker(
        self,
        live_session: Any,
        session_stop: asyncio.Event,
        pending_prompts: list[str],
    ) -> None:
        in_parts: list[str] = []
        out_parts: list[str] = []
        turn_tools: list[dict[str, Any]] = []
        latest_usage_total = 0

        while not session_stop.is_set():
            async for msg in live_session.receive():
                if session_stop.is_set():
                    return
                self._last_activity_at = time.monotonic()

                if msg.usage_metadata and msg.usage_metadata.total_token_count:
                    latest_usage_total = int(msg.usage_metadata.total_token_count)

                sc = msg.server_content
                if sc is not None:
                    if sc.interrupted:
                        self.flush_speaker()
                        self._on_event("live_interrupted", {})

                    if sc.input_transcription and sc.input_transcription.text:
                        in_parts.append(sc.input_transcription.text)

                    if sc.output_transcription and sc.output_transcription.text:
                        out_parts.append(sc.output_transcription.text)

                    if sc.model_turn and sc.model_turn.parts:
                        cfg = self._get_config()
                        for part in sc.model_turn.parts:
                            inline = part.inline_data
                            if (
                                inline is not None
                                and inline.data
                                and cfg.voice_enabled
                                and cfg.live_voice_mode == "gemini"
                            ):
                                pcm48 = pcm24k_to_48k(inline.data)
                                if pcm48:
                                    with contextlib.suppress(asyncio.QueueFull):
                                        self._spk_queue.put_nowait(pcm48)

                    if sc.turn_complete:
                        if latest_usage_total > 0:
                            self.last_turn_tokens = latest_usage_total
                            self.live_tokens_used += latest_usage_total
                            if self._on_tokens is not None:
                                self._on_tokens(latest_usage_total)
                            latest_usage_total = 0

                        heard_text = "".join(in_parts).strip()
                        typed_text = pending_prompts.pop(0) if pending_prompts and not heard_text else ""
                        raw_reply = "".join(out_parts).strip()
                        reply_text = "" if raw_reply.startswith("<no speech>") else raw_reply
                        cfg = self._get_config()

                        if reply_text and cfg.voice_enabled and cfg.live_voice_mode == "robot_tts":
                            with contextlib.suppress(Exception):
                                await self.bridge.call("say", {"text": reply_text})

                        if heard_text or typed_text or reply_text or turn_tools:
                            prompt_label = (
                                f"🎙️ {heard_text}" if heard_text else (typed_text or "(live voice/camera)")
                            )
                            record_dict: dict[str, Any] = {
                                "kind": "live",
                                "timestamp": time.time(),
                                "prompt": prompt_label,
                                "reply": reply_text,
                                "tool_calls": list(turn_tools),
                                "tokens": self.last_turn_tokens,
                                "error": None,
                            }
                            await self._on_turn(record_dict)

                        in_parts.clear()
                        out_parts.clear()
                        turn_tools.clear()
                        self._set_mode("warm" if cfg.idle_token_saver else "connected")

                tc = msg.tool_call
                if tc is not None and tc.function_calls:
                    responses: list[types.FunctionResponse] = []
                    for fc in tc.function_calls:
                        fn_name = fc.name or "unknown"
                        fn_args = dict(fc.args or {})
                        self._on_event("status", {"busy": True, "kind": f"live:{fn_name}"})
                        try:
                            result_dict, ok_flag = await self._execute_live_tool(
                                fn_name, fn_args, live_session=live_session
                            )
                        finally:
                            self._on_event("status", {"busy": False, "kind": "live"})

                        tool_entry: dict[str, Any] = {
                            "name": fn_name,
                            "args": fn_args,
                            "output": result_dict,
                            "ok": ok_flag,
                        }
                        turn_tools.append(tool_entry)
                        self._on_event("live_tool", tool_entry)
                        scheduling: types.FunctionResponseScheduling | None = None
                        if fn_name in _NON_BLOCKING_TOOLS:
                            scheduling = (
                                types.FunctionResponseScheduling.SILENT
                                if ok_flag
                                else types.FunctionResponseScheduling.WHEN_IDLE
                            )
                        responses.append(
                            types.FunctionResponse(
                                id=fc.id,
                                name=fn_name,
                                response={"result": result_dict},
                                scheduling=scheduling,
                            )
                        )
                    await live_session.send_tool_response(function_responses=responses)

    async def _run_active_session(self, api_key: str, cfg: PetConfig) -> None:
        try:
            mem_snapshot = await asyncio.to_thread(self.memory_store.read_memory, "MEMORY.md")
        except Exception:
            mem_snapshot = ""

        self._set_mode("connecting")
        bcp47 = _resolve_bcp47_lang(cfg.listen_language)
        client = genai.Client(api_key=api_key, http_options={"api_version": "v1alpha"})
        live_cfg = types.LiveConnectConfig(
            response_modalities=[types.Modality.AUDIO],
            speech_config=types.SpeechConfig(
                language_code=bcp47,
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=cfg.live_voice_name)
                ),
            ),
            input_audio_transcription=types.AudioTranscriptionConfig(language_codes=[bcp47]),
            output_audio_transcription=types.AudioTranscriptionConfig(language_codes=[bcp47]),
            realtime_input_config=types.RealtimeInputConfig(
                automatic_activity_detection=types.AutomaticActivityDetection(disabled=True)
            ),
            context_window_compression=types.ContextWindowCompressionConfig(
                sliding_window=types.SlidingWindow()
            ),
            system_instruction=build_live_system_instruction(cfg, mem_snapshot),
            tools=build_live_tool_declarations(),
            temperature=cfg.temperature,
        )

        session_stop = asyncio.Event()
        pending_prompts: list[str] = []
        self._last_activity_at = time.monotonic()

        async with client.aio.live.connect(model=cfg.live_model, config=live_cfg) as live_session:
            self.connected = True
            self.last_error = None
            logger.info(
                "Gemini Live active (model=%s, voice=%s, idle_token_saver=%s)",
                cfg.live_model,
                cfg.live_voice_name,
                cfg.idle_token_saver,
            )
            self._set_mode("connected" if not cfg.idle_token_saver else ("listening" if self._user_speaking else "warm"))

            subtasks = [
                asyncio.create_task(
                    self._uplink_worker(live_session, session_stop, pending_prompts), name="live-uplink"
                ),
                asyncio.create_task(
                    self._receive_worker(live_session, session_stop, pending_prompts), name="live-recv"
                ),
                asyncio.create_task(session_stop.wait(), name="live-session-stop"),
                asyncio.create_task(self._reload_event.wait(), name="live-reload"),
                asyncio.create_task(self._stop_event.wait(), name="live-stop"),
            ]
            if cfg.idle_token_saver:
                subtasks.append(asyncio.create_task(self._idle_watchdog(session_stop), name="live-idle-watch"))
            else:
                subtasks.append(
                    asyncio.create_task(self._continuous_video_worker(live_session, session_stop), name="live-video")
                )
            try:
                done, _ = await asyncio.wait(subtasks, return_when=asyncio.FIRST_COMPLETED)
                for d in done:
                    if d.get_name() in ("live-uplink", "live-recv"):
                        exc = d.exception()
                        if exc is not None:
                            raise exc
            finally:
                session_stop.set()
                self.connected = False
                for t in subtasks:
                    t.cancel()
                await asyncio.gather(*subtasks, return_exceptions=True)

    async def _live_session_supervisor(self) -> None:
        """Manage Gemini Live lifecycle: Full continuous mode (default) or 0-token Standby when Idle Saver is ON."""
        while not self._stop_event.is_set():
            self._reload_event.clear()
            cfg = self._get_config()
            api_key = resolve_gemini_api_key(self._get_api_key())

            if not self._is_ai_live_active(cfg) or not api_key:
                self.connected = False
                self._set_mode("paused" if not cfg.autonomy_enabled else "off")
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._reload_event.wait(), timeout=1.5)
                continue

            # When Idle Token Saver is ON: stay in 0-token Standby until VAD speech or chat wakes it.
            if cfg.idle_token_saver and self._activity_queue.empty() and self._text_queue.empty():
                self.connected = False
                self._set_mode("standby")
                self._wake_event.clear()
                wake_task = asyncio.create_task(self._wake_event.wait())
                stop_task = asyncio.create_task(self._stop_event.wait())
                reload_task = asyncio.create_task(self._reload_event.wait())
                try:
                    await asyncio.wait(
                        {wake_task, stop_task, reload_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    for t in (wake_task, stop_task, reload_task):
                        t.cancel()
                if self._stop_event.is_set() or self._reload_event.is_set():
                    continue
                if self._activity_queue.empty() and self._text_queue.empty():
                    continue

            try:
                await self._run_active_session(api_key, self._get_config())
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.connected = False
                self.last_error = str(exc)
                logger.warning("Gemini Live session ended (%s); retrying in 2s", exc)
                self._set_mode("standby" if cfg.idle_token_saver else "connecting")
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop_event.wait(), timeout=2.0)
