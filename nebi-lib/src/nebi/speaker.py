"""One voice at a time on Nebi's speaker: the arbiter for the intercom downlink.

The robot has a single speaker stream (`/data/audio/playback_data`). Everything that talks through it — the
cockpit "Speak" panel, `nebi say` (agent API, embedded in the cockpit), idle babble and a person holding the
cockpit's intercom talk button — shares the `Speaker` on the client's intercom (``client.intercom.speaker``):

* `Speaker.begin(owner, label)` starts a *turn*. A new turn interrupts the current one ("new speech wins").
* `Speaker.stop()` interrupts whatever is playing.
* `Speaker.hold_mic(holder, on)` — a person is talking on the intercom: the current turn is interrupted and new
  turns are refused with `SpeakerBusyError` (``SPEAKER_BUSY``) until the mic is released (people win over TTS).
* `Speaker.stream(turn, pcm)` paces s16le 48 kHz PCM to the robot in real time (20 ms frames, small lead) and
  stops at the first frame after the turn was interrupted.

The robot only plays downlink audio while an intercom session is open (`START_RECORD`). `stream()` opens one
when none is running and closes it `AUTO_SESSION_LINGER_S` after the last turn, so back-to-back phrases share a
session. A session the cockpit intercom opened (or later joins, see `adopt_session`) is never closed here.

Most callers only need the one-call helpers (also on the client: ``await robot.say("Hello!")``)::

    await robot.speaker.say("Hello!", emotion="happy")      # render with the default TTS engine and play it
    await robot.speaker.play(speech_audio_or_pcm)           # play a SpeechAudio or s16le 48 kHz mono PCM

`begin()` / `stream()` / `end()` are the low-level pieces they are built from (e.g. to sync eyes to the audio).
"""

from __future__ import annotations

import asyncio
import itertools
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from nebi.exceptions import NebiError

if TYPE_CHECKING:
    from nebi.intercom import Intercom
    from nebi.tts import SpeechAudio

__all__ = ["SPEAKER_BUSY", "Speaker", "SpeakerBusyError", "SpeakerTurn"]

SPEAKER_BUSY = "SPEAKER_BUSY"
DEFAULT_OWNER = "nebi"  # who is talking, as shown in `Speaker.status()`
STREAM_LEAD_S = 0.08  # keep ~4 frames queued on the robot (its jitter buffer is 100-300 ms)
_FRAME_S = 0.02
_FRAME_BYTES = 1920  # 20 ms @ 48 kHz s16le mono (nebi.intercom.INTERCOM_FRAME_BYTES)
AUTO_SESSION_LINGER_S = 3.0  # keep a speaker-opened intercom session this long after the last turn


class SpeakerBusyError(NebiError):
    """The speaker is taken by a person talking on the intercom."""

    def __init__(self, holder: str) -> None:
        super().__init__(
            SPEAKER_BUSY,
            "Someone is talking on the intercom right now, so Nebi cannot speak.",
            stage="audio",
            hint="Release the intercom talk button (or switch the intercom from open mic to push-to-talk), then retry.",
            details={"mic_holder": holder},
        )


@dataclass
class SpeakerTurn:
    """One utterance's claim on the speaker."""

    id: int
    owner: str  # who is speaking: "cockpit", "nebi say", ...
    label: str = ""  # what (text / "babble")
    duration_s: float | None = None
    started: float = field(default_factory=time.monotonic)
    ended: bool = False
    interrupted_by: str | None = None

    @property
    def live(self) -> bool:
        return not self.ended

    def info(self) -> dict[str, Any]:
        elapsed = time.monotonic() - self.started
        out: dict[str, Any] = {"id": self.id, "owner": self.owner, "label": self.label, "elapsed_s": round(elapsed, 2)}
        if self.duration_s is not None:
            out["duration_s"] = round(self.duration_s, 2)
            out["remaining_s"] = round(max(0.0, self.duration_s - elapsed), 2)
        return out


class Speaker:
    """Arbitrates the robot speaker between speech turns and the intercom push-to-talk (see module docstring)."""

    def __init__(self, intercom: Intercom) -> None:
        self._intercom = intercom
        self._ids = itertools.count(1)
        self._turn: SpeakerTurn | None = None
        self._mic_holder: str | None = None
        self.last: SpeakerTurn | None = None  # most recent turn (for status: was it interrupted?)
        self._auto_session = False  # True while the intercom session was opened by stream(), not by a person
        self._auto_stop: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------ state

    @property
    def mic_holder(self) -> str | None:
        return self._mic_holder

    def current(self) -> SpeakerTurn | None:
        t = self._turn
        return t if t is not None and t.live else None

    @property
    def busy(self) -> bool:
        """Speech playing or a person talking: low-priority sounds (idle babble) should stay quiet."""
        return self.current() is not None or self._mic_holder is not None

    def status(self) -> dict[str, Any]:
        cur = self.current()
        out: dict[str, Any] = {"speaking": cur is not None, "mic_held": self._mic_holder is not None,
                               "turn": cur.info() if cur is not None else None}
        if cur is None and self.last is not None:
            out["last"] = {**self.last.info(), "interrupted_by": self.last.interrupted_by}
        return out

    # ------------------------------------------------------------------ turns

    async def play(self, audio: SpeechAudio | bytes, *, owner: str = DEFAULT_OWNER, label: str = "") -> dict[str, Any]:
        """Play `audio` (a `SpeechAudio`, or s16le 48 kHz mono PCM bytes) on the robot speaker and wait until it ends.

        Interrupts whatever is playing; raises `SpeakerBusyError` while a person holds the intercom mic. Returns
        ``{"frames_sent", "duration_s", "interrupted"?}``. `owner` only names who is talking in `status()`."""
        if isinstance(audio, (bytes, bytearray, memoryview)):
            pcm, duration = bytes(audio), len(audio) / (2 * 48000)
            label = label or "audio"
        else:
            pcm, duration = audio.pcm16(48000), audio.duration
            label = label or audio.text or f"({audio.emotion} babble)"
        turn = self.begin(owner, label=label, duration_s=duration)
        try:
            stats = await self.stream(turn, pcm)
        finally:
            self.end(turn)
        return {**stats, "duration_s": round(duration, 3)}

    async def say(self, text: str = "", *, emotion: str = "neutral", babble: bool = False, engine: str | None = None,
                  preset: str | None = None, owner: str = DEFAULT_OWNER) -> dict[str, Any]:
        """Speak `text` (or wordless babble) through the robot speaker with a TTS engine and wait until it ends.

        `engine` defaults to the best registered one (`nebi.tts.get_engine`); emoji in `text` pick the emotion
        when `emotion` is "neutral". Returns the `play()` stats plus ``spoken``, ``emotion``, ``engine``."""
        from nebi.tts import get_engine, split_emoji

        clean, emoji_emotion = split_emoji(text or "")
        mood = emoji_emotion if emotion == "neutral" and emoji_emotion else emotion
        eng = get_engine(engine)
        speech = await asyncio.to_thread(eng.synthesize, None if babble or not clean.strip() else clean,
                                         emotion=mood, babble=babble or not clean.strip(), preset=preset)
        stats = await self.play(speech, owner=owner)
        return {**stats, "spoken": speech.text, "emotion": speech.emotion, "engine": eng.name}

    def begin(self, owner: str, label: str = "", duration_s: float | None = None) -> SpeakerTurn:
        """Claim the speaker (interrupting the current turn). SpeakerBusyError while a person holds the mic."""
        if self._mic_holder is not None:
            raise SpeakerBusyError(self._mic_holder)
        self._interrupt(by=owner)
        turn = SpeakerTurn(next(self._ids), owner, label, duration_s)
        self._turn = self.last = turn
        return turn

    def end(self, turn: SpeakerTurn) -> None:
        """The turn finished (normally or not); releases the speaker if it is still the current turn."""
        turn.ended = True
        if self._turn is turn:
            self._turn = None
            if self._auto_session and self._auto_stop is None:
                try:
                    self._schedule_auto_stop()
                except RuntimeError:  # no running loop (sync caller): stream()'s own schedule covers it
                    pass

    def stop(self, by: str = "stop") -> SpeakerTurn | None:
        """Silence the current turn; returns it (None when nothing was playing)."""
        return self._interrupt(by=by)

    def _interrupt(self, by: str) -> SpeakerTurn | None:
        cur = self.current()
        if cur is not None:
            cur.interrupted_by = by
            self.end(cur)
        return cur

    def hold_mic(self, holder: str, on: bool) -> None:
        """A person starts (on) / stops talking on the intercom. Starting interrupts speech."""
        if on:
            self._interrupt(by="intercom")
            self._mic_holder = holder
        elif self._mic_holder == holder:
            self._mic_holder = None

    # ------------------------------------------------------------------ intercom session

    @property
    def owns_session(self) -> bool:
        """True while the open intercom session was opened by the speaker (and will be closed by it)."""
        return self._auto_session and self._intercom.active

    def adopt_session(self) -> None:
        """Someone else (the cockpit intercom) now uses the open session: never close it from here."""
        self._auto_session = False
        self._cancel_auto_stop()

    def release_session(self) -> bool:
        """The last intercom user left. Returns True when the speaker takes the session over (speech still
        playing; it closes it after the turn), False when the caller should close it now."""
        if self.current() is None:
            return False
        self._auto_session = True
        return True

    def _cancel_auto_stop(self) -> None:
        if self._auto_stop is not None and not self._auto_stop.done():
            self._auto_stop.cancel()
        self._auto_stop = None

    async def _ensure_session(self) -> None:
        self._cancel_auto_stop()
        if not self._intercom.active:
            await self._intercom.start()
            self._auto_session = True

    def _schedule_auto_stop(self) -> None:
        if not self._auto_session:
            return
        self._cancel_auto_stop()
        self._auto_stop = asyncio.get_running_loop().create_task(self._stop_session_later())

    async def _stop_session_later(self) -> None:
        try:
            await self._close_after_linger()
        finally:
            if self._auto_stop is asyncio.current_task():
                self._auto_stop = None

    async def _close_after_linger(self) -> None:
        await asyncio.sleep(AUTO_SESSION_LINGER_S)
        if not self._auto_session or self.current() is not None:
            return  # a turn is still finishing: end() schedules the close again
        self._auto_session = False
        if self._intercom.active:
            try:
                await self._intercom.stop()
            except Exception:  # robot gone: nothing left to close
                self._intercom.detach()

    # ------------------------------------------------------------------ streaming

    async def stream(self, turn: SpeakerTurn, pcm: bytes, *, alive: Callable[[], bool] | None = None,
                     t_start: float | None = None, lead_s: float = STREAM_LEAD_S) -> dict[str, Any]:
        """Send s16le 48 kHz mono `pcm` in real time as `turn`. Stops early when the turn is interrupted or
        `alive()` turns false. Does not end the turn (the caller does, after any tail work)."""
        pcm += b"\x00" * ((-len(pcm)) % _FRAME_BYTES)
        stats: dict[str, Any] = {"frames_sent": 0}
        if not turn.live:
            stats["interrupted"] = True
            return stats
        await self._ensure_session()  # the robot plays downlink audio only inside an intercom session
        try:
            return await self._stream(turn, pcm, stats, alive=alive, t_start=t_start, lead_s=lead_s)
        finally:
            self._schedule_auto_stop()

    async def _stream(self, turn: SpeakerTurn, pcm: bytes, stats: dict[str, Any], *,
                      alive: Callable[[], bool] | None, t_start: float | None, lead_s: float) -> dict[str, Any]:
        ic = self._intercom
        ic.new_talk_session()  # seq 0 -> the robot resets its jitter buffer
        t0 = time.monotonic() if t_start is None else t_start
        for i in range(0, len(pcm), _FRAME_BYTES):
            if not turn.live or (alive is not None and not alive()):
                stats["interrupted"] = True
                return stats
            delay = t0 + (i // _FRAME_BYTES) * _FRAME_S - lead_s - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
                if not turn.live or (alive is not None and not alive()):
                    stats["interrupted"] = True
                    return stats
            ic.send_pcm(pcm[i : i + _FRAME_BYTES])
            stats["frames_sent"] += 1
        # let the robot's buffer drain before the next turn may start a new talk session
        tail = t0 + len(pcm) // _FRAME_BYTES * _FRAME_S - time.monotonic()
        while tail > 0 and turn.live and (alive is None or alive()):
            await asyncio.sleep(min(tail, 0.05))
            tail = t0 + len(pcm) // _FRAME_BYTES * _FRAME_S - time.monotonic()
        if not turn.live:
            stats["interrupted"] = True
        return stats
