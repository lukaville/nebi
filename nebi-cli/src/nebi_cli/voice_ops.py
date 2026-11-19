"""Voice (a pluggable TTS engine, see nebi.tts, through the robot speaker) and hearing (robot mic + local
mlx-whisper).

The engine is chosen per call (`say --engine`), by $PYNEBI_TTS_ENGINE, or as the best registered one (extensions
register theirs with `AgentExtensionContext.register_tts_engine`); the built-in `chirp` engine always works. When no
engine can render, `say` falls back to a built-in robot sound effect matching the emotion.

`say` and `listen` mark the runtime "busy" (separate from motion) so idle pet life never talks over them.
Automatic babble (`_auto`, used by idle/body) is low priority: it is skipped while any speech or listening is in
progress and never interrupts it; a user `say` interrupts earlier speech (speech epochs, like motion epochs).
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import sys
import tempfile
import time
import wave
from collections.abc import Coroutine
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nebi.speaker import SpeakerBusyError, SpeakerTurn
from nebi.tts import SpeechAudio, TtsEngine, get_engine, split_emoji
from nebi_cli.activity import ActivityTracker
from nebi_cli.errors import CACHE_DIR, STANDING, AgentError, bad_arg
from nebi_cli.motion import neutral
from nebi_cli.registry import Args, CommandGroup, command

if TYPE_CHECKING:
    from nebi.client import AsyncNebiClient

logger = logging.getLogger(__name__)

STT_MODEL = "mlx-community/whisper-large-v3-turbo"
STT_FIRST_LOAD_WAIT_S = 90.0  # how long a `listen` waits for the speech model on first use before returning

ROBOT_RATE = 48000  # robot speaker / mic: 48 kHz s16le mono
STREAM_LEAD_S = 0.08  # keep ~4 frames queued on the robot
LED_ENVELOPE_HZ = 25.0
SILENCE_RMS = 500  # int16 RMS below which a mic chunk counts as quiet (listen end-of-speech)
SILENCE_END_S = 1.2
SPEECH_MIN_RMS = 0.004  # normalized RMS below which a recording is treated as silence (no transcription)
_AUTO_SKIPPED = {"skipped": True, "reason": "speech or listening in progress",
                 "summary": "stayed quiet (something else is speaking/listening)"}


def _load_stt() -> None:
    """Import mlx_whisper and load the model (worker thread). Raises ImportError when not installed."""
    import mlx_whisper  # optional heavy dep (Apple Silicon only)
    import numpy as np

    mlx_whisper.transcribe(np.zeros(16000, np.float32), path_or_hf_repo=STT_MODEL)


def _transcribe(x16: Any, kw: dict[str, Any]) -> dict[str, Any]:
    import mlx_whisper  # optional heavy dep (Apple Silicon only)

    return mlx_whisper.transcribe(x16, **kw)


# emotion -> built-in robot sound effect id (fallback when no TTS engine can render)
FALLBACK_SOUNDS = {"happy": 3, "excited": 5, "curious": 6, "love": 7, "neutral": 3, "uh-oh": 8, "sad": 2,
                   "scared": 8, "sleepy": 7, "grumpy": 8}


def _save_wav(path: Path, pcm: bytes, rate: int = ROBOT_RATE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)


def _write_temp_wav(utt: SpeechAudio) -> str:
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        name = f.name
    _save_wav(Path(name), utt.pcm16(utt.sample_rate), utt.sample_rate)
    return name


def _save_utterance(out: Path, utt: SpeechAudio) -> Path:
    if out.suffix.lower() != ".wav":
        out = out.with_suffix(".wav")
    _save_wav(out, utt.pcm16(utt.sample_rate), utt.sample_rate)
    out.with_suffix(".json").write_text(json.dumps(utt.timeline()))
    return out


def speech_context(c: AsyncNebiClient | None, emotion: str | None, volume: float | None) -> tuple[str | None, float, list[str]]:
    """Pet-like context rules: tired when battery is low, hushed at night, louder for far-away people."""
    notes: list[str] = []
    vol = 1.0 if volume is None else float(volume)
    if volume is None:
        hour = time.localtime().tm_hour
        if hour >= 22 or hour < 7:
            vol *= 0.6
            notes.append("night: quieter")
        if c is not None:
            locked = next((tg for tg in c.telemetry.targets if tg.locked), None)
            # confidence is None on real-robot targets: fall back to the nearest one
            far = locked or (max(c.telemetry.targets, key=lambda tg: (tg.confidence or 0.0, -(tg.distance_m or 99.0)))
                             if c.telemetry.targets else None)
            if far is not None and far.distance_m > 3.0:
                vol = min(1.4, vol * 1.3)
                notes.append(f"listener ~{far.distance_m:.1f} m away: louder")
    if c is not None and emotion in (None, "neutral") and c.telemetry.battery_soc <= 15 and not c.telemetry.charging:
        emotion = "sleepy"
        notes.append(f"battery {c.telemetry.battery_soc}%: tired voice")
    return emotion, vol, notes


class SpeechService:
    """TTS engine choice, the current utterance (speech epoch + playback task) and the speech-to-text model."""

    def __init__(self, activity: ActivityTracker) -> None:
        self._activity = activity
        self._epoch = 0
        self._task: asyncio.Task[Any] | None = None
        self._stt_state: str | None = None  # None (not loaded) | "ready" | "missing"
        self._stt_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------ speech

    def engine(self, name: str | None, preset: str | None) -> TtsEngine:
        """The TTS engine to use (BAD_ARGUMENT for an unknown engine or a preset the engine does not have)."""
        try:
            eng = get_engine(name)
        except KeyError as exc:
            raise bad_arg(str(exc).strip("'\""), hint="Omit --engine for the default; engines come from nebi.tts / extensions.") from None
        if preset and preset not in eng.presets:
            raise bad_arg(f"Unknown voice preset {preset!r} for engine {eng.name!r}.", hint=f"Presets: {', '.join(eng.presets)}")
        return eng

    def speaking(self) -> bool:
        return self._task is not None and not self._task.done()

    def occupied(self) -> bool:
        """Speech playing or say/listen running: automatic babble must stay quiet."""
        return self.speaking() or self._activity.busy

    def interrupt(self) -> None:
        """Silence the current utterance (its playback loops watch the epoch)."""
        self._epoch += 1

    def is_current(self, epoch: int) -> bool:
        return self._epoch == epoch

    def new_utterance(self) -> int:
        self._epoch += 1
        return self._epoch

    def play(self, job: Coroutine[Any, Any, dict[str, Any]]) -> asyncio.Task[dict[str, Any]]:
        """Start playback as the current utterance; marks the runtime busy until it ends."""
        busy = self._activity.begin_busy("say")
        task = asyncio.create_task(job, name="nebi-agent-speech")
        self._task = task

        def _done(t: asyncio.Task[Any]) -> None:
            self._activity.end_busy(busy)
            if not t.cancelled() and t.exception() is not None:
                logger.warning("speech playback failed: %r", t.exception())

        task.add_done_callback(_done)
        return task

    # ------------------------------------------------------------------ speech to text

    def warm_up_stt(self) -> None:
        """Start loading the speech model in the background if that has not happened yet."""
        if self._stt_state is None and self._stt_task is None:
            self._stt_task = asyncio.create_task(asyncio.to_thread(_load_stt), name="nebi-agent-stt-load")

    async def stt_ready(self, wait_s: float) -> bool | None:
        """True when the speech model is loaded; False if still loading after wait_s; None if not installed."""
        if self._stt_state == "ready":
            return True
        if self._stt_state == "missing":
            return None
        if self._stt_task is None:
            self._stt_task = asyncio.create_task(asyncio.to_thread(_load_stt), name="nebi-agent-stt-load")
        done, _ = await asyncio.wait({self._stt_task}, timeout=wait_s)
        if not done:
            return False
        task, self._stt_task = self._stt_task, None
        exc = task.exception()
        if isinstance(exc, ImportError):
            self._stt_state = "missing"
            return None
        if exc is not None:
            raise AgentError("STT_FAILED", f"Loading the speech model failed: {type(exc).__name__}: {exc}",
                             hint="Check network/disk for the model download (see daemon log); retry `nebi listen`.")
        self._stt_state = "ready"
        return True


class VoiceCommands(CommandGroup):
    @property
    def speech(self) -> SpeechService:
        return self.rt.speech

    async def _perform(self, c: AsyncNebiClient | None, utt: SpeechAudio, *, to: str, animate: bool, nod: bool,
                       epoch: int, turn: SpeakerTurn | None = None) -> dict[str, Any]:
        """Stream an utterance to the robot speaker and/or this computer; pulse the LED eyes / nod in sync.
        `turn`: this utterance's claim on the robot speaker (shared with the cockpit's Speak panel / intercom)."""
        speech, motion = self.speech, self.motion
        stats: dict[str, Any] = {"frames_sent": 0}
        tasks: list[asyncio.Task[Any]] = []
        t_start = time.monotonic() + 0.05

        def live() -> bool:  # not interrupted by a newer `say`/stop here, nor by the cockpit / intercom
            return speech.is_current(epoch) and (turn is None or turn.live)

        async def robot_stream(rc: AsyncNebiClient) -> None:
            if turn is None:
                return
            res = await rc.intercom.speaker.stream(turn, utt.pcm16(ROBOT_RATE), t_start=t_start, lead_s=STREAM_LEAD_S,
                                                   alive=lambda: speech.is_current(epoch) and rc.connected)
            stats["frames_sent"] += res["frames_sent"]
            if res.get("interrupted"):
                stats["interrupted"] = True

        async def local_play() -> None:
            wav = await asyncio.to_thread(_write_temp_wav, utt)
            player = ["afplay", wav] if sys.platform == "darwin" else ["aplay", "-q", wav]
            try:
                try:
                    proc = await asyncio.create_subprocess_exec(*player, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except FileNotFoundError:
                    stats["local_error"] = f"{player[0]} not found"
                    return
                while proc.returncode is None:
                    if not live():
                        proc.kill()
                        await proc.wait()
                        stats["interrupted"] = True
                        break
                    try:
                        await asyncio.wait_for(proc.wait(), 0.05)
                    except asyncio.TimeoutError:
                        pass
            finally:
                await asyncio.to_thread(Path(wav).unlink, missing_ok=True)

        async def eyes(rc: AsyncNebiClient) -> None:
            env = utt.envelope
            r, g, b = utt.color or (90, 160, 255)
            step = 2  # update LEDs at 12.5 Hz
            for i in range(0, len(env), step):
                if not live():
                    return
                delay = t_start + i / LED_ENVELOPE_HZ - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
                k = 0.15 + 0.85 * float(env[i])
                await rc.eyes.set_color(int(r * k), int(g * k), int(b * k), 0, hold_sec=0.25)

        async def nods(rc: AsyncNebiClient) -> None:
            # Each nod sets ry for <=0.14 s and returns it to 0 (well inside motion's stick auto-expiry).
            for t_beat, strength in utt.beats:
                if not live():
                    return
                if strength < 0.6 or motion.lock.locked() or rc.telemetry.posture_name not in STANDING:
                    continue
                delay = t_start + t_beat - 0.04 - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
                rc.motion.set_axes(ry=-0.35)
                await asyncio.sleep(0.07)
                rc.motion.set_axes(ry=0.35)
                await asyncio.sleep(0.07)
                rc.motion.set_axes(ry=0.0)

        if to in ("robot", "both") and c is not None:
            tasks.append(asyncio.create_task(robot_stream(c)))
        if to in ("local", "both"):
            tasks.append(asyncio.create_task(local_play()))
        if c is not None and animate:
            tasks.append(asyncio.create_task(eyes(c)))
        if c is not None and nod:
            tasks.append(asyncio.create_task(nods(c)))
        try:
            await asyncio.gather(*tasks)
        finally:
            for t in tasks:  # on cancellation (or one part failing) do not leave the others streaming
                t.cancel()
            if c is not None and turn is not None:
                c.intercom.speaker.end(turn)
            if c is not None and nod and not motion.lock.locked():
                neutral(c)
        return stats

    async def _fallback_sound(self, c: AsyncNebiClient | None, emotion: str, engine: str, exc: Exception, *,
                              auto: bool, warnings: list[str]) -> dict[str, Any]:
        """No engine could render: play the built-in robot sound effect closest to the emotion."""
        why = f"{type(exc).__name__}: {exc}"
        if c is None:
            raise AgentError("VOICE_UNAVAILABLE", f"TTS engine {engine!r} failed ({why}) and no robot is connected for a "
                             "sound-effect fallback.", hint="Try `--engine chirp` (built in), or `nebi up` first.")
        sid = FALLBACK_SOUNDS.get(emotion, 3)
        await c.audio.play_sound_effect(sid)
        if not auto:
            warnings.append(f"TTS engine {engine!r} failed ({why}); played a robot sound instead.")
        return {"spoken": None, "emotion": emotion, "mode": "sound", "engine": engine, "fallback_sound": sid,
                "summary": f"could not speak ({engine} failed); played a {emotion} robot sound instead"}

    def _speaker_busy(self) -> bool:
        """The robot speaker is taken (cockpit speech, or a person on the intercom)."""
        c = self.rt.connected_client()
        return c is not None and c.intercom.speaker.busy

    @command("say")
    async def say(self, a: Args) -> dict[str, Any]:
        speech = self.speech
        warnings: list[str] = a["_warnings"]
        auto = bool(a.get("_auto"))  # internal: idle/body babble, low priority
        if auto and (speech.occupied() or self._speaker_busy()):
            return dict(_AUTO_SKIPPED)
        text = str(a.get("text") or "").strip()
        mode = "babble" if a.get("babble") else "auto"
        to = a.get("to") or "auto"
        c = self.rt.connected_client()
        if to == "auto":
            to = "local" if (c is None or c.flow == "simulator") else "robot"
        if to in ("robot", "both") and c is None:
            self.client()  # raises NOT_CONNECTED with the usual hint
        engine = speech.engine(a.get("engine"), a.get("preset"))
        text, emoji_emotion = split_emoji(text)
        emotion, vol, notes = speech_context(c, a.get("emotion") or emoji_emotion, a.get("volume"))
        emotion = emotion or "neutral"
        if emotion not in engine.emotions:
            warnings.append(f"TTS engine {engine.name!r} has no {emotion!r} voice; speaking neutral.")
            emotion = "neutral"
        babble = mode == "babble" or not text
        t0 = time.perf_counter()
        try:
            utt = await asyncio.to_thread(engine.synthesize, None if babble else text, emotion=emotion, babble=babble,
                                          preset=a.get("preset") or None)
            utt = utt.scaled(vol)
        except ValueError as exc:
            raise bad_arg(str(exc), hint="Give some words, or use --babble.") from exc
        except Exception as exc:  # engine failure: a robot sound still says something
            logger.warning("TTS engine %s failed", engine.name, exc_info=True)
            return await self._fallback_sound(c, emotion, engine.name, exc, auto=auto, warnings=warnings)
        render_ms = (time.perf_counter() - t0) * 1000
        if len(utt.text.split()) > 12:
            warnings.append("Long line for a pet robot; 1-6 words (or --babble) usually feels more natural.")
        res: dict[str, Any] = {
            "spoken": utt.text,
            "emotion": utt.emotion,
            "mode": utt.mode,
            "engine": engine.name,
            "duration_s": round(utt.duration, 2),
            "to": to,
            "render_ms": round(render_ms),
        }
        if notes:
            res["context"] = notes
        if a.get("out"):
            saved = await asyncio.to_thread(_save_utterance, Path(a["out"]), utt)
            res["saved"] = str(saved.resolve())
        if auto and (speech.occupied() or self._speaker_busy()):  # something started while rendering
            return dict(_AUTO_SKIPPED)

        turn: SpeakerTurn | None = None
        if to in ("robot", "both") and c is not None:
            try:  # one voice at a time on the robot: interrupts cockpit speech; refused while the intercom mic is held
                turn = c.intercom.speaker.begin("nebi say", label=utt.text or f"({utt.emotion} babble)",
                                                duration_s=utt.duration)
            except SpeakerBusyError:
                if auto:
                    return dict(_AUTO_SKIPPED)
                raise
        epoch = speech.new_utterance()
        job = speech.play(self._perform(c, utt, to=to, animate=not a.get("no_animate"), nod=bool(a.get("nod")), epoch=epoch,
                                        turn=turn))
        res["summary"] = (f'said "{utt.text}"' if utt.mode != "babble" else f"babbled ({utt.emotion})") + \
            (" (in the background)" if a.get("no_wait") else "")
        if a.get("no_wait"):
            res["playing_in_background"] = True
            return res
        stats = await job
        res.update(stats)
        if stats.get("interrupted"):
            res["summary"] += " (interrupted)"
        return res

    @command("listen")
    async def listen(self, a: Args) -> dict[str, Any]:
        import numpy as np  # heavy; only needed for listen

        c = self.client()
        warnings: list[str] = a["_warnings"]
        secs = max(1.0, min(30.0, float(a.get("seconds") or 6.0)))
        until_silence = not a.get("fixed")
        self.speech.warm_up_stt()  # load the model while recording
        busy = self.activity.begin_busy("listen")
        chunks: list[bytes] = []
        try:
            ic = c.intercom
            unsub = ic.add_pcm_listener(lambda pcm: chunks.append(bytes(pcm)))
            started_here = False
            try:
                if not ic.active:
                    await ic.start()
                    started_here = True
                t0 = time.monotonic()
                heard_at: float | None = None
                quiet_since: float | None = None
                while time.monotonic() - t0 < secs:
                    await asyncio.sleep(0.1)
                    if not until_silence or not chunks:
                        continue
                    tail = np.frombuffer(b"".join(chunks[-10:]), dtype=np.int16).astype(np.float32)
                    loud = tail.size and float(np.sqrt(np.mean(tail * tail))) > SILENCE_RMS
                    if loud:
                        heard_at, quiet_since = heard_at or time.monotonic(), None
                    elif heard_at:
                        quiet_since = quiet_since or time.monotonic()
                        if time.monotonic() - quiet_since > SILENCE_END_S:
                            break
            finally:
                unsub()
                if started_here:
                    try:
                        await ic.stop()
                    except Exception:  # the recording is done; a failing intercom stop is not the caller's problem
                        logger.debug("intercom stop failed", exc_info=True)
            pcm = b"".join(chunks)
            if not pcm:
                raise AgentError("NO_AUDIO", "No microphone audio arrived from the robot.",
                                 hint="The mic streams only while the intercom runs; check `nebi status` and retry.")
            x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
            rms = float(np.sqrt(np.mean(x * x)))
            path = Path(a["out"]) if a.get("out") else CACHE_DIR / "audio" / f"listen-{time.strftime('%Y%m%d-%H%M%S')}.wav"
            await asyncio.to_thread(_save_wav, path, pcm)
            out: dict[str, Any] = {"seconds": round(len(x) / ROBOT_RATE, 1), "loudness_rms": round(rms, 4), "wav": str(path.resolve())}
            if rms < SPEECH_MIN_RMS:
                out.update(text="", speech=False, summary="silence (nothing loud enough to transcribe)")
                return out
            ready = await self.speech.stt_ready(STT_FIRST_LOAD_WAIT_S)
            if ready is None:
                out.update(text=None, stt="missing", summary="recorded audio, but speech-to-text is not installed",
                           hint="pip install mlx-whisper into the nebi venv (Apple Silicon)")
                return out
            if not ready:
                warnings.append("loading speech model (first use, may download ~1.5 GB); retry `nebi listen` in a minute.")
                out.update(text=None, stt="loading", summary="recorded audio; the speech model is still loading, retry shortly")
                return out
            n = len(x) // 3 * 3
            x16 = x[:n].reshape(-1, 3).mean(axis=1)  # 48 kHz -> 16 kHz
            kw: dict[str, Any] = {"path_or_hf_repo": STT_MODEL}
            if a.get("language"):
                kw["language"] = a["language"]
            r = await asyncio.to_thread(_transcribe, x16, kw)
            text = (r.get("text") or "").strip()
            out.update(text=text, language=r.get("language"), speech=bool(text),
                       summary=f'heard: "{text}"' if text else "sound but no words recognised")
            return out
        finally:
            self.activity.end_busy(busy)
