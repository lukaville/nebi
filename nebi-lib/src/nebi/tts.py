"""Pluggable text-to-speech for Nebi's speaker (`nebi say`, idle/emote babble).

* `TtsEngine` — the protocol an engine implements: ``name``, ``emotions``, ``presets``, optional ``priority`` and
  ``synthesize(text, *, emotion, babble, preset) -> SpeechAudio``.
* `SpeechAudio` — mono PCM (float32 in -1..1, or int16) + sample rate, plus per-syllable/word ``beats`` (time,
  strength) that drive gimbal nods; the eye-pulse envelope is derived from the audio.
* Registry — `register_engine(engine)` from library code, or `AgentExtensionContext.register_tts_engine(engine)`
  from a nebi extension. Selection: an explicit name (`nebi say --engine NAME`), else env ``PYNEBI_TTS_ENGINE``,
  else the registered engine with the highest ``priority`` (default 10; the built-in engine has 0).
* `ChirpEngine` (built in, numpy only) — a procedural small-robot voice: emotion-shaped chirps for babble and one
  chirp per syllable for text (no real words), so `nebi say` works out of the box.
* ``system`` (built in, `nebi.tts_system`) — the OS's own speech (macOS ``say``, Windows SAPI, Linux espeak-ng /
  espeak / pico2wave); registered only when such a backend exists, with priority 5 (above chirp, below extensions).
* `read_audio_file(path)` — any audio file (WAV, AIFF, MP3, ...) as `SpeechAudio`, decoded with PyAV.

Plain-text helpers (`split_emoji`) are shared by every engine: emoji in the text pick the emotion.
"""

from __future__ import annotations

import os
import re
import threading
import zlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "EMOJI_EMOTIONS",
    "EMOTIONS",
    "EMOTION_COLORS",
    "ENV_ENGINE",
    "ChirpEngine",
    "SpeechAudio",
    "TtsEngine",
    "TtsError",
    "engines",
    "get_engine",
    "read_audio_file",
    "register_engine",
    "register_tts_engine",
    "split_emoji",
    "unregister_engine",
]

ENV_ENGINE = "PYNEBI_TTS_ENGINE"
DEFAULT_PRIORITY = 10
ENVELOPE_HZ = 25.0

EMOTIONS: tuple[str, ...] = ("neutral", "happy", "excited", "curious", "sad", "scared", "sleepy", "grumpy", "love", "uh-oh")
EMOJI_EMOTIONS: dict[str, str] = {
    "😊": "happy", "🙂": "happy", "😀": "happy", "😄": "happy", "🎉": "excited", "🤩": "excited", "👀": "curious",
    "🤔": "curious", "😢": "sad", "😭": "sad", "😱": "scared", "😨": "scared", "😴": "sleepy", "🥱": "sleepy",
    "😠": "grumpy", "😤": "grumpy", "❤️": "love", "❤": "love", "🥰": "love", "😬": "uh-oh",
}
# eye colour (r, g, b) used when the voice pulses direct-LED eyes
EMOTION_COLORS: dict[str, tuple[int, int, int]] = {
    "neutral": (90, 160, 255), "happy": (255, 200, 40), "excited": (255, 120, 20), "curious": (60, 220, 200),
    "sad": (40, 70, 255), "scared": (200, 60, 255), "sleepy": (70, 60, 160), "grumpy": (255, 40, 20),
    "love": (255, 60, 150), "uh-oh": (255, 150, 0),
}
_EMOJI_RE = re.compile("|".join(re.escape(e) for e in sorted(EMOJI_EMOTIONS, key=len, reverse=True)))
_OTHER_EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF☀-➿️]")


class TtsError(Exception):
    """An engine could not render (bad preset, missing model, ...). ValueError is used for unusable text."""


def split_emoji(text: str) -> tuple[str, str | None]:
    """'Ball? 👀' -> ('Ball?', 'curious'): emoji removed, the first known one picks the emotion."""
    m = _EMOJI_RE.search(text)
    emotion = EMOJI_EMOTIONS[m.group(0)] if m else None
    clean = _OTHER_EMOJI_RE.sub("", _EMOJI_RE.sub("", text))
    return " ".join(clean.split()), emotion


@dataclass
class SpeechAudio:
    """One rendered utterance."""

    audio: Any  # 1-D numpy array: float32 in -1..1, or int16
    sample_rate: int
    text: str = ""  # what was spoken ("" for babble)
    emotion: str = "neutral"
    mode: str = "words"  # "words" | "babble"
    beats: list[tuple[float, float]] = field(default_factory=list)  # (time_s, strength 0..1) per syllable/word
    color: tuple[int, int, int] | None = None  # eye colour; default from EMOTION_COLORS

    def __post_init__(self) -> None:
        import numpy as np

        a = np.asarray(self.audio)
        if a.dtype == np.int16:
            a = a.astype(np.float32) / 32768.0
        self.audio = np.clip(a.astype(np.float32).reshape(-1), -1.0, 1.0)
        if self.color is None:
            self.color = EMOTION_COLORS.get(self.emotion, EMOTION_COLORS["neutral"])

    @property
    def duration(self) -> float:
        return len(self.audio) / float(self.sample_rate)

    @property
    def envelope(self) -> list[float]:
        """Loudness 0..1 at ENVELOPE_HZ (eye pulsing)."""
        import numpy as np

        hop = max(1, int(self.sample_rate / ENVELOPE_HZ))
        n = len(self.audio) // hop
        if n == 0:
            return []
        frames = self.audio[: n * hop].reshape(n, hop)
        rms = np.sqrt(np.mean(frames * frames, axis=1))
        peak = float(rms.max()) or 1.0
        return [round(float(v) / peak, 3) for v in rms]

    def scaled(self, gain: float) -> SpeechAudio:
        import numpy as np

        return SpeechAudio(np.clip(self.audio * float(gain), -1.0, 1.0), self.sample_rate, self.text, self.emotion,
                           self.mode, list(self.beats), self.color)

    def pcm16(self, rate: int) -> bytes:
        """s16le mono at `rate` (linear resampling)."""
        import numpy as np

        x = self.audio
        if rate != self.sample_rate and len(x) > 1:
            n = round(len(x) * rate / self.sample_rate)
            x = np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(np.float32)
        return (np.clip(x, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()

    def timeline(self) -> dict[str, Any]:
        """Body-sync data saved next to `nebi say -o file.wav` (file.json)."""
        return {"text": self.text, "emotion": self.emotion, "mode": self.mode, "sample_rate": self.sample_rate,
                "duration": round(self.duration, 3), "beats": [[round(t, 3), round(s, 2)] for t, s in self.beats],
                "envelope_hz": ENVELOPE_HZ, "envelope": self.envelope}


@runtime_checkable
class TtsEngine(Protocol):
    """A speech engine. Optional attribute ``priority`` (int, default 10) ranks engines for the default choice."""

    @property
    def name(self) -> str: ...

    @property
    def emotions(self) -> Sequence[str]:
        """Supported emotions (others are spoken as "neutral")."""
        ...

    @property
    def presets(self) -> Sequence[str]:
        """Voice presets; the first one is the default."""
        ...

    def synthesize(self, text: str | None, *, emotion: str, babble: bool, preset: str | None) -> SpeechAudio:
        """Render `text` (None/"" with babble=True: wordless babble). Runs in a worker thread.
        Raise ValueError for text it cannot speak, TtsError for anything else."""
        ...


# ============================================================================= registry

_lock = threading.Lock()
_engines: dict[str, TtsEngine] = {}


def register_engine(engine: TtsEngine, *, replace: bool = False) -> None:
    with _lock:
        if engine.name in _engines and not replace and _engines[engine.name] is not engine:
            raise ValueError(f"TTS engine {engine.name!r} is already registered")
        _engines[engine.name] = engine


def unregister_engine(name: str, engine: TtsEngine | None = None) -> None:
    """Remove `name` (only if it is still `engine`, when given)."""
    with _lock:
        if engine is None or _engines.get(name) is engine:
            _engines.pop(name, None)


def _priority(e: TtsEngine) -> int:
    return int(getattr(e, "priority", DEFAULT_PRIORITY))


def engines() -> list[TtsEngine]:
    """Registered engines, best first."""
    with _lock:
        return sorted(_engines.values(), key=lambda e: -_priority(e))


def get_engine(name: str | None = None) -> TtsEngine:
    """By name, else $PYNEBI_TTS_ENGINE, else the best registered. KeyError (listing the names) when unknown."""
    want = name or os.environ.get(ENV_ENGINE, "").strip() or None
    found = engines()
    if want is None:
        if not found:
            raise KeyError("no TTS engine registered")
        return found[0]
    for e in found:
        if e.name == want:
            return e
    raise KeyError(f"unknown TTS engine {want!r} (registered: {', '.join(e.name for e in found) or 'none'})")


# ============================================================================= built-in engine

# emotion -> (base pitch Hz, contour per syllable: start..end ratio, syllable seconds, gap seconds, vibrato depth, brightness)
_STYLE: dict[str, tuple[float, tuple[float, float], float, float, float, float]] = {
    "neutral": (620, (1.0, 0.95), 0.11, 0.05, 0.00, 0.5),
    "happy": (760, (0.95, 1.25), 0.10, 0.04, 0.02, 0.7),
    "excited": (900, (1.0, 1.45), 0.07, 0.03, 0.04, 0.8),
    "curious": (680, (0.9, 1.3), 0.12, 0.06, 0.01, 0.6),
    "sad": (430, (1.05, 0.75), 0.18, 0.09, 0.02, 0.3),
    "scared": (980, (1.1, 0.9), 0.06, 0.03, 0.08, 0.6),
    "sleepy": (360, (1.0, 0.8), 0.22, 0.12, 0.01, 0.2),
    "grumpy": (300, (1.0, 0.9), 0.12, 0.05, 0.03, 0.9),
    "love": (560, (0.95, 1.1), 0.17, 0.07, 0.05, 0.35),
    "uh-oh": (700, (1.0, 0.7), 0.14, 0.08, 0.00, 0.5),
}
_PRESET_PITCH = {"robot": 1.0, "tiny": 1.35, "deep": 0.7}
_VOWELS = re.compile(r"[aeiouyаеёиоуыэюя]+", re.IGNORECASE)


class ChirpEngine:
    """Procedural robot chirps (numpy only): babble, or one chirp per syllable of the text."""

    name = "chirp"
    emotions: tuple[str, ...] = EMOTIONS
    presets: tuple[str, ...] = tuple(_PRESET_PITCH)
    priority = 0
    sample_rate = 24000

    def synthesize(self, text: str | None, *, emotion: str, babble: bool, preset: str | None) -> SpeechAudio:
        import numpy as np

        preset = preset or self.presets[0]
        if preset not in _PRESET_PITCH:
            raise TtsError(f"unknown preset {preset!r} (presets: {', '.join(self.presets)})")
        style = _STYLE.get(emotion, _STYLE["neutral"])
        words = [] if babble else [w for w in re.findall(r"[\w']+", text or "")]
        if not babble and not words:
            raise ValueError("nothing to say (no words in the text)")
        rng = np.random.default_rng(zlib.crc32(f"{text}|{emotion}|{preset}|{babble}".encode()))
        # syllable plan: (pitch scale, strength, gap after)
        plan: list[tuple[float, float, float]] = []
        base, _contour, _syl_s, gap_s, _vib, _bright = style
        if babble:
            n = int(rng.integers(3, 7))
            for i in range(n):
                plan.append((float(rng.uniform(0.85, 1.2)), 1.0 if i == 0 else float(rng.uniform(0.5, 0.9)),
                             gap_s * float(rng.uniform(0.6, 1.6))))
        else:
            question = (text or "").rstrip().endswith("?")
            for wi, w in enumerate(words):
                syl = max(1, len(_VOWELS.findall(w)))
                for si in range(min(syl, 4)):
                    last = wi == len(words) - 1 and si == min(syl, 4) - 1
                    scale = float(rng.uniform(0.9, 1.12)) * (1.25 if (question and last) else 1.0)
                    plan.append((scale, 1.0 if si == 0 else 0.6, gap_s * (2.2 if si == min(syl, 4) - 1 else 0.5)))
        sr = self.sample_rate
        pitch_k = _PRESET_PITCH[preset]
        chunks, beats, t = [], [], 0.04
        chunks.append(np.zeros(int(0.04 * sr), np.float32))
        for i, (scale, strength, gap) in enumerate(plan):
            final = i == len(plan) - 1
            chunks.append(self._chirp(style, base * scale * pitch_k, strength=strength, final=final, emotion=emotion, rng=rng))
            beats.append((round(t, 3), strength))
            t += len(chunks[-1]) / sr
            g = np.zeros(int(gap * sr), np.float32)
            chunks.append(g)
            t += len(g) / sr
        audio = np.concatenate(chunks)
        audio *= 0.8 / max(1e-6, float(np.max(np.abs(audio))))
        return SpeechAudio(audio, sr, text="" if babble else (text or "").strip(),
                           emotion=emotion if emotion in self.emotions else "neutral",
                           mode="babble" if babble else "words", beats=beats)

    def _chirp(self, style: tuple[float, tuple[float, float], float, float, float, float], f0: float, *, strength: float,
               final: bool, emotion: str, rng: Any) -> Any:
        import numpy as np

        _base, (c0, c1), syl_s, _gap, vib, bright = style
        dur = syl_s * float(rng.uniform(0.8, 1.25)) * (1.4 if final else 1.0)
        sr = self.sample_rate
        n = max(16, int(dur * sr))
        tt = np.arange(n) / sr
        u = tt / dur
        if emotion == "uh-oh" and final:  # two-tone drop
            contour = np.where(u < 0.45, 1.05, 0.72)
        else:
            end = c1 * (1.15 if final and emotion == "curious" else 1.0)
            contour = c0 + (end - c0) * u
        freq = f0 * contour * (1.0 + vib * np.sin(2 * np.pi * 7.0 * tt))
        phase = 2 * np.pi * np.cumsum(freq) / sr
        wave = np.sin(phase) + bright * 0.45 * np.sin(2 * phase) + bright * 0.2 * np.sin(3 * phase)
        if emotion == "grumpy":
            wave += 0.35 * np.sign(np.sin(phase * 0.5))
        attack = np.minimum(1.0, tt / 0.012)
        release = np.minimum(1.0, (dur - tt) / 0.03).clip(0.0, 1.0)
        return (wave * attack * release * (0.5 + 0.5 * strength)).astype(np.float32)


def read_audio_file(path: str | os.PathLike[str], *, rate: int | None = None) -> SpeechAudio:
    """Decode any audio file PyAV/FFmpeg can read to mono `SpeechAudio` (at `rate`, default: the file's own rate).
    TtsError when the file is missing or has no decodable audio."""
    import av
    import numpy as np

    try:
        container = av.open(str(path))
    except (OSError, av.error.FFmpegError) as exc:
        raise TtsError(f"cannot open audio file {str(path)!r}: {exc}") from exc
    with container:
        if not container.streams.audio:
            raise TtsError(f"{str(path)!r} has no audio stream")
        stream = container.streams.audio[0]
        out_rate = int(rate or stream.rate or 48000)
        resampler = av.AudioResampler(format="flt", layout="mono", rate=out_rate)
        chunks: list[Any] = []
        try:
            for frame in container.decode(stream):
                chunks += [f.to_ndarray().reshape(-1) for f in resampler.resample(frame)]
            chunks += [f.to_ndarray().reshape(-1) for f in resampler.resample(None)]
        except av.error.FFmpegError as exc:
            raise TtsError(f"cannot decode {str(path)!r}: {exc}") from exc
    if not chunks:
        raise TtsError(f"{str(path)!r} contains no audio samples")
    return SpeechAudio(np.concatenate(chunks).astype(np.float32), out_rate)


register_tts_engine = register_engine  # `nebi.register_tts_engine`

register_engine(ChirpEngine())


def _register_builtin_system_engine() -> None:
    from nebi.tts_system import register_system_engine  # imports this module: keep at the bottom

    register_system_engine()


_register_builtin_system_engine()
