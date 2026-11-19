"""The built-in ``system`` TTS engine: the operating system's own speech, no extra Python dependencies.

Backends (the first one found on this machine is used; none found -> the engine is not registered):

* macOS — ``say`` (voices from ``say -v '?'``);
* Windows — System.Speech (SAPI) through PowerShell (voices from ``GetInstalledVoices()``);
* Linux — ``espeak-ng``, else ``espeak`` (voices from ``--voices``), else ``pico2wave`` (SVOX Pico).

Every backend writes a 16-bit WAV to a temp file that is read with the stdlib ``wave`` module (no ffmpeg); text is
passed on stdin or in an environment variable, never interpolated into a command line or script.

* Presets: ``"default"`` (the OS default voice) first, then the installed voices of the current locale's language;
  any other installed voice is accepted by name too (``preset in engine.presets`` checks all of them).
* Emotions: the same names as the ``chirp`` engine, rendered as speaking-rate and pitch changes (espeak: native
  pitch; other backends: the sample rate is relabelled, which shifts pitch, and the rate is compensated).
* Babble (wordless) is delegated to the ``chirp`` engine: system voices only speak words.
* ``priority = 5``: above ``chirp`` (0), below extension engines (default 10).
* ``PYNEBI_TTS_SYSTEM=0`` keeps the engine unregistered (the test suites set it so they never run real speech).
"""

from __future__ import annotations

import locale
import math
import os
import platform
import re
import shutil
import subprocess
import tempfile
import threading
import wave
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, overload

from nebi.tts import EMOTIONS, ChirpEngine, SpeechAudio, TtsError

__all__ = ["ENV_SYSTEM", "SystemEngine", "detect_backend", "register_system_engine"]

ENV_SYSTEM = "PYNEBI_TTS_SYSTEM"
DEFAULT_VOICE = "default"
MAX_TEXT_CHARS = 2000
MAX_PRESETS = 48
LIST_TIMEOUT_S = 15.0
_RENDER_RATE = 22050  # Hz requested from backends that let us choose

# emotion -> (speaking-rate multiplier, pitch shift in semitones)
EMOTION_PROSODY: dict[str, tuple[float, float]] = {
    "neutral": (1.0, 0.0), "happy": (1.08, 2.0), "excited": (1.2, 3.0), "curious": (1.0, 1.5),
    "sad": (0.85, -2.0), "scared": (1.25, 3.0), "sleepy": (0.75, -3.0), "grumpy": (0.92, -3.0),
    "love": (0.92, 1.0), "uh-oh": (0.95, -1.0),
}


def _render_timeout(text: str) -> float:
    return 20.0 + 0.1 * len(text)


def _locale_lang() -> tuple[str, str]:
    """('en', 'en_GB') from LC_ALL/LC_MESSAGES/LANG or the process locale; ('en', '') when unknown."""
    raw = os.environ.get("LC_ALL") or os.environ.get("LC_MESSAGES") or os.environ.get("LANG") or ""
    if not raw or raw in ("C", "POSIX") or raw.startswith("C."):
        try:
            raw = locale.getlocale()[0] or ""
        except ValueError:
            raw = ""
    tag = raw.split(".")[0].replace("-", "_")
    lang = tag.split("_")[0].lower()
    if not re.fullmatch(r"[a-z]{2,3}", lang):
        return "en", ""
    return lang, tag


# ============================================================================= voice list


class VoiceList(Sequence[str]):
    """The curated presets (iteration / len / index) that also *contains* every installed voice (case-insensitive),
    so ``preset in engine.presets`` accepts any voice by name while UIs list a short, locale-relevant set."""

    def __init__(self, shown: Sequence[str], accepted: Callable[[str], bool]) -> None:
        self._shown = list(shown)
        self._accepted = accepted

    @overload
    def __getitem__(self, i: int) -> str: ...
    @overload
    def __getitem__(self, i: slice) -> list[str]: ...
    def __getitem__(self, i: int | slice) -> str | list[str]:
        return self._shown[i]

    def __len__(self) -> int:
        return len(self._shown)

    def __iter__(self) -> Iterator[str]:
        return iter(self._shown)

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and (name in self._shown or self._accepted(name))

    def __repr__(self) -> str:
        return f"VoiceList({self._shown!r})"


@dataclass(frozen=True)
class Voice:
    name: str  # what the backend accepts (say -v / SelectVoice / espeak -v / pico2wave -l)
    lang: str  # 'en_US', 'en-gb', ... ('' when unknown)


def _curate(voices: Sequence[Voice], lang: str, tag: str) -> list[str]:
    """'default', then voices of the exact locale, then the rest of its language (else English), alphabetical."""
    def norm(code: str) -> str:
        return code.replace("-", "_").lower()

    same = [v for v in voices if norm(v.lang).split("_")[0] == lang]
    if not same:
        same = [v for v in voices if norm(v.lang).startswith("en")]
    exact = sorted({v.name for v in same if tag and norm(v.lang) == tag.lower()}, key=str.lower)
    rest = sorted({v.name for v in same} - set(exact), key=str.lower)
    return [DEFAULT_VOICE, *(exact + rest)[: MAX_PRESETS - 1]]


# ============================================================================= backends


def _run(cmd: list[str], *, what: str, timeout: float, input_text: str | None = None,
         env: dict[str, str] | None = None) -> str:
    """Run a backend command; TtsError (with the tail of stderr) on a missing binary, timeout or failure."""
    try:
        proc = subprocess.run(cmd, input=input_text, capture_output=True, text=True, timeout=timeout, check=False,
                              env=None if env is None else {**os.environ, **env}, encoding="utf-8", errors="replace")
    except FileNotFoundError as exc:
        raise TtsError(f"{what}: {cmd[0]!r} is not installed or not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise TtsError(f"{what}: {cmd[0]} did not finish within {timeout:.0f} s") from exc
    except OSError as exc:
        raise TtsError(f"{what}: could not run {cmd[0]}: {exc}") from exc
    if proc.returncode != 0:
        err = " ".join((proc.stderr or proc.stdout or "").split())[-300:]
        raise TtsError(f"{what}: {cmd[0]} exited with code {proc.returncode}" + (f": {err}" if err else ""))
    return proc.stdout or ""


class Backend:
    """One OS speech program. `render` writes a 16-bit WAV to `out` and returns True when it applied the pitch
    shift itself (otherwise the engine relabels the sample rate)."""

    name = ""
    native_pitch = False

    def __init__(self, exe: str) -> None:
        self.exe = exe

    def list_voices(self) -> list[Voice]:
        raise NotImplementedError

    def accepts(self, name: str, voices: Sequence[Voice]) -> bool:
        return any(v.name.lower() == name.lower() for v in voices)

    def resolve(self, name: str, voices: Sequence[Voice]) -> str:
        """The backend's own spelling of an accepted voice name."""
        return next((v.name for v in voices if v.name.lower() == name.lower()), name)

    def render(self, text: str, voice: str | None, rate: float, semitones: float, out: Path) -> None:
        raise NotImplementedError


class SayBackend(Backend):
    """macOS `say`."""

    name = "say"
    base_wpm = 180
    _LINE = re.compile(r"^(?P<name>.+?)\s+(?P<lang>[a-z]{2,3}_[A-Za-z0-9]+)\s+#")

    def list_voices(self) -> list[Voice]:
        out = _run([self.exe, "-v", "?"], what="listing macOS voices", timeout=LIST_TIMEOUT_S)
        voices: dict[str, Voice] = {}
        for line in out.splitlines():
            m = self._LINE.match(line)
            if m:
                voices.setdefault(m["name"].strip(), Voice(m["name"].strip(), m["lang"]))
        return list(voices.values())

    def render(self, text: str, voice: str | None, rate: float, semitones: float, out: Path) -> None:
        text = text.replace("[[", "[ [").replace("]]", "] ]")  # no embedded `say` commands from user text
        cmd = [self.exe, "-o", str(out), f"--data-format=LEI16@{_RENDER_RATE}", "-r", str(round(self.base_wpm * rate))]
        if voice:
            cmd += ["-v", voice]
        _run([*cmd, "-f", "-"], what="macOS say", timeout=_render_timeout(text), input_text=text)


_SAPI_PREAMBLE = "$ErrorActionPreference = 'Stop'; Add-Type -AssemblyName System.Speech; " \
                 "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
_SAPI_LIST = _SAPI_PREAMBLE + (
    "foreach ($v in $s.GetInstalledVoices()) { if ($v.Enabled) { "
    "[Console]::Out.WriteLine($v.VoiceInfo.Name + \"`t\" + $v.VoiceInfo.Culture.Name) } }; $s.Dispose()"
)
# all inputs come from NEBI_TTS_* environment variables; nothing is interpolated into the script
_SAPI_SPEAK = _SAPI_PREAMBLE + (
    "if ($env:NEBI_TTS_VOICE) { $s.SelectVoice($env:NEBI_TTS_VOICE) }; "
    "$s.Rate = [int]$env:NEBI_TTS_RATE; "
    "$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo([int]$env:NEBI_TTS_HZ, "
    "[System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono); "
    "$s.SetOutputToWaveFile($env:NEBI_TTS_OUT, $fmt); $s.Speak($env:NEBI_TTS_TEXT); $s.Dispose()"
)


class SapiBackend(Backend):
    """Windows System.Speech via PowerShell."""

    name = "sapi"

    def _ps(self, script: str, *, what: str, timeout: float, env: dict[str, str] | None = None) -> str:
        return _run([self.exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
                    what=what, timeout=timeout, env=env)

    def list_voices(self) -> list[Voice]:
        out = self._ps(_SAPI_LIST, what="listing Windows voices", timeout=LIST_TIMEOUT_S)
        voices = []
        for line in out.splitlines():
            name, _, lang = line.strip().partition("\t")
            if name:
                voices.append(Voice(name, lang))
        return voices

    @staticmethod
    def sapi_rate(rate: float) -> int:
        """SAPI Rate is -10..10; +10 is ~3x and -10 ~1/3x the normal speed."""
        return max(-10, min(10, round(10 * math.log(rate) / math.log(3.0))))

    def render(self, text: str, voice: str | None, rate: float, semitones: float, out: Path) -> None:
        env = {"NEBI_TTS_TEXT": text, "NEBI_TTS_VOICE": voice or "", "NEBI_TTS_RATE": str(self.sapi_rate(rate)),
               "NEBI_TTS_HZ": str(_RENDER_RATE), "NEBI_TTS_OUT": str(out)}
        self._ps(_SAPI_SPEAK, what="Windows speech", timeout=_render_timeout(text), env=env)


class EspeakBackend(Backend):
    """Linux `espeak-ng` / `espeak` (pitch is native: -p 0..99, default 50)."""

    native_pitch = True
    base_wpm = 175

    def __init__(self, exe: str, name: str) -> None:
        super().__init__(exe)
        self.name = name

    def list_voices(self) -> list[Voice]:
        out = _run([self.exe, "--voices"], what=f"listing {self.name} voices", timeout=LIST_TIMEOUT_S)
        voices: dict[str, Voice] = {}
        for line in out.splitlines()[1:]:
            cols = line.split()
            if len(cols) >= 2 and cols[0].isdigit():
                voices.setdefault(cols[1], Voice(cols[1], cols[1]))
        return list(voices.values())

    def accepts(self, name: str, voices: Sequence[Voice]) -> bool:
        base, _, variant = name.partition("+")  # 'en+m3', 'en-us+robosoft': any variant of an installed voice
        return super().accepts(base, voices) and bool(re.fullmatch(r"[\w-]*", variant))

    def resolve(self, name: str, voices: Sequence[Voice]) -> str:
        base, plus, variant = name.partition("+")
        return super().resolve(base, voices) + plus + variant

    def render(self, text: str, voice: str | None, rate: float, semitones: float, out: Path) -> None:
        pitch = max(0, min(99, round(50 + 6 * semitones)))
        cmd = [self.exe, "-s", str(round(self.base_wpm * rate)), "-p", str(pitch), "-w", str(out)]
        if voice:
            cmd += ["-v", voice]
        _run([*cmd, "--stdin"], what=self.name, timeout=_render_timeout(text), input_text=text)


class PicoBackend(Backend):
    """Linux `pico2wave` (SVOX Pico): fixed voices, no rate control."""

    name = "pico2wave"
    VOICES = ("en-US", "en-GB", "de-DE", "es-ES", "fr-FR", "it-IT")

    def list_voices(self) -> list[Voice]:
        return [Voice(v, v) for v in self.VOICES]

    def render(self, text: str, voice: str | None, rate: float, semitones: float, out: Path) -> None:
        lang, tag = _locale_lang()
        if not voice:
            want = tag.replace("_", "-")
            voice = next((v for v in self.VOICES if v.lower() == want.lower()), None) or \
                next((v for v in self.VOICES if v.lower().startswith(lang + "-")), "en-US")
        _run([self.exe, "-l", voice, "-w", str(out), "--", text], what="pico2wave", timeout=_render_timeout(text))


def detect_backend(system: str | None = None, which: Callable[[str], str | None] = shutil.which) -> Backend | None:
    """The speech backend available on this machine (None when there is none)."""
    system = system or platform.system()
    if system == "Darwin":
        exe = which("say")
        return SayBackend(exe) if exe else None
    if system == "Windows":
        exe = which("powershell") or which("pwsh")
        return SapiBackend(exe) if exe else None
    for prog in ("espeak-ng", "espeak"):
        exe = which(prog)
        if exe:
            return EspeakBackend(exe, prog)
    exe = which("pico2wave")
    return PicoBackend(exe) if exe else None


# ============================================================================= WAV + beats


def read_wav(path: Path) -> tuple[Any, int]:
    """(float32 mono samples in -1..1, sample rate) of a PCM WAV (8/16/24/32-bit, any channel count)."""
    import numpy as np

    try:
        with wave.open(str(path), "rb") as w:
            ch, width, rate, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
            raw = w.readframes(n)
    except (wave.Error, EOFError, OSError) as exc:
        raise TtsError(f"the speech backend wrote no readable WAV ({exc})") from exc
    if width == 1:
        a = (np.frombuffer(raw, np.uint8).astype(np.float32) - 128.0) / 128.0
    elif width == 2:
        a = np.frombuffer(raw, "<i2").astype(np.float32) / 32768.0
    elif width == 3:
        b = np.frombuffer(raw, np.uint8).reshape(-1, 3).astype(np.int32)
        a = ((b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)) << 8 >> 8).astype(np.float32) / 8388608.0
    elif width == 4:
        a = np.frombuffer(raw, "<i4").astype(np.float32) / 2147483648.0
    else:
        raise TtsError(f"unsupported WAV sample width {width} bytes")
    if ch > 1:
        a = a[: len(a) // ch * ch].reshape(-1, ch).mean(axis=1)
    return a.astype(np.float32), rate


def trim_and_normalize(audio: Any, rate: int, *, pad_s: float = 0.03, peak: float = 0.9) -> Any:
    """Cut leading/trailing silence (keeping `pad_s`) and scale the peak to `peak`."""
    import numpy as np

    loud = np.flatnonzero(np.abs(audio) > 0.01)
    if len(loud) == 0:
        return audio
    pad = int(pad_s * rate)
    out = audio[max(0, loud[0] - pad) : loud[-1] + pad + 1]
    m = float(np.max(np.abs(out)))
    return (out * (peak / m)).astype(np.float32) if m > 0 else out


def speech_beats(audio: Any, rate: int, *, hop_s: float = 0.01, min_gap_s: float = 0.15) -> list[tuple[float, float]]:
    """Syllable-ish beats (time_s, strength 0..1): peaks of the smoothed loudness at least `min_gap_s` apart."""
    import numpy as np

    hop = max(1, int(rate * hop_s))
    n = len(audio) // hop
    if n < 3:
        return []
    rms = np.sqrt(np.mean(audio[: n * hop].reshape(n, hop) ** 2, axis=1))
    k = max(1, round(0.05 / hop_s))
    env = np.convolve(rms, np.ones(k) / k, mode="same")
    top = float(env.max())
    if top <= 0:
        return []
    w = max(1, round(min_gap_s / hop_s / 2))
    beats: list[tuple[float, float]] = []
    for i in range(n):
        v = env[i]
        if v < 0.3 * top or v < env[max(0, i - w) : i + w + 1].max():
            continue
        t = i * hop_s
        if beats and t - beats[-1][0] < min_gap_s:
            continue
        beats.append((round(t, 3), round(float(v / top), 2)))
    return beats


# ============================================================================= engine


class SystemEngine:
    """The OS's built-in speech (see the module docstring)."""

    name = "system"
    emotions: tuple[str, ...] = EMOTIONS
    priority = 5

    def __init__(self, backend: Backend) -> None:
        self.backend = backend
        self._lock = threading.Lock()
        self._voices: list[Voice] | None = None
        self._presets: VoiceList | None = None
        self._chirp = ChirpEngine()

    def voices(self) -> list[Voice]:
        """All installed voices (listed once, then cached; [] when listing fails)."""
        with self._lock:
            if self._voices is None:
                try:
                    self._voices = self.backend.list_voices()
                except TtsError:
                    self._voices = []
            return self._voices

    @property
    def presets(self) -> VoiceList:
        if self._presets is None:
            voices = self.voices()
            lang, tag = _locale_lang()
            self._presets = VoiceList(_curate(voices, lang, tag), lambda n: self.backend.accepts(n, voices))
        return self._presets

    def synthesize(self, text: str | None, *, emotion: str, babble: bool, preset: str | None) -> SpeechAudio:
        if babble:  # system voices only speak words: wordless babble is the chirp engine's
            return self._chirp.synthesize(None, emotion=emotion, babble=True, preset=None)
        text = " ".join((text or "").split())
        if not re.search(r"\w", text):
            raise ValueError("nothing to say (no words in the text)")
        if len(text) > MAX_TEXT_CHARS:
            raise ValueError(f"text too long ({len(text)} characters; at most {MAX_TEXT_CHARS} per utterance)")
        voice: str | None = None
        if preset and preset != DEFAULT_VOICE:
            if preset not in self.presets:
                shown = ", ".join(list(self.presets)[:12])
                raise TtsError(f"unknown voice {preset!r} for the {self.backend.name} backend (e.g. {shown}; "
                               "any installed voice name works)")
            voice = self.backend.resolve(preset, self.voices())
        emotion = emotion if emotion in EMOTION_PROSODY else "neutral"
        rate, semitones = EMOTION_PROSODY[emotion]
        shift = 1.0 if self.backend.native_pitch else 2.0 ** (semitones / 12.0)
        with tempfile.TemporaryDirectory(prefix="nebi-tts-") as tmp:
            out = Path(tmp) / "speech.wav"
            # pitch by relabelling the sample rate also speeds playback up by `shift`: render slower to compensate
            self.backend.render(text, voice, rate / shift, semitones, out)
            audio, sr = read_wav(out)
        if len(audio) == 0:
            raise TtsError(f"the {self.backend.name} backend produced no audio")
        sr = max(1, round(sr * shift))
        audio = trim_and_normalize(audio, sr)
        return SpeechAudio(audio, sr, text=text, emotion=emotion, mode="words", beats=speech_beats(audio, sr))


def register_system_engine(*, backend: Backend | None = None) -> SystemEngine | None:
    """Register `SystemEngine` when this machine has a speech backend (and ``PYNEBI_TTS_SYSTEM`` is not 0)."""
    from nebi.tts import register_engine

    if os.environ.get(ENV_SYSTEM, "").strip().lower() in ("0", "false", "no", "off"):
        return None
    backend = backend or detect_backend()
    if backend is None:
        return None
    engine = SystemEngine(backend)
    register_engine(engine, replace=True)
    return engine
