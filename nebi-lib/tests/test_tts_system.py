"""The built-in `system` TTS engine (OS speech): backends with mocked subprocesses per platform, plus one real
macOS `say` render (skipped where `say` is missing). conftest sets PYNEBI_TTS_SYSTEM=0, so nothing here relies on
the engine being registered implicitly."""
from __future__ import annotations

import shutil
import subprocess
import wave
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from nebi import tts, tts_system
from nebi.tts_system import (
    EspeakBackend,
    PicoBackend,
    SapiBackend,
    SayBackend,
    SystemEngine,
    Voice,
    detect_backend,
    read_wav,
    register_system_engine,
)

SAY_VOICES = """\
Albert              en_US    # Hello! My name is Albert.
Alice               it_IT    # Ciao! Mi chiamo Alice.
Daniel (English (UK)) en_GB    # Hello! My name is Daniel.
Daniel (English (UK)) en_GB    # Hello! My name is Daniel.
Zarvox              en_US    # Hello! My name is Zarvox.
"""
ESPEAK_VOICES = """\
Pty Language       Age/Gender VoiceName          File                 Other Languages
 5  af              --/M      Afrikaans          gmw/af
 2  en-gb           --/M      English_(Great_Britain) gmw/en
 2  en-us           --/M      English_(America)  gmw/en-US
 5  de              --/M      German             gmw/de
"""


def speech_wav(path: Path, rate: int = 22050, seconds: float = 1.0, channels: int = 1) -> None:
    """Three tone bursts ("syllables") with silence around them, like a speech backend would write."""
    t = np.arange(int(rate * seconds)) / rate
    env = sum(np.exp(-(((t - c) / 0.05) ** 2)) for c in (0.25, 0.5, 0.75))
    x = (0.5 * env * np.sin(2 * np.pi * 220 * t) * 32767).astype("<i2")
    if channels > 1:
        x = np.repeat(x, channels)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(x.tobytes())


class FakeRun:
    """Stands in for subprocess.run: records calls, writes a WAV to the backend's output path."""

    def __init__(self, stdout: str = "", returncode: int = 0, stderr: str = "", exc: BaseException | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.stdout, self.returncode, self.stderr, self.exc = stdout, returncode, stderr, exc

    def __call__(self, cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append({"cmd": cmd, **kw})
        if self.exc is not None:
            raise self.exc
        out = None
        if "-o" in cmd:
            out = cmd[cmd.index("-o") + 1]
        elif "-w" in cmd:
            out = cmd[cmd.index("-w") + 1]
        elif kw.get("env") and "NEBI_TTS_OUT" in kw["env"]:
            out = kw["env"]["NEBI_TTS_OUT"]
        if out and self.returncode == 0:
            speech_wav(Path(out))
        return subprocess.CompletedProcess(cmd, self.returncode, self.stdout, self.stderr)


@pytest.fixture()
def run(monkeypatch: pytest.MonkeyPatch) -> FakeRun:
    fake = FakeRun()
    monkeypatch.setattr(tts_system.subprocess, "run", fake)
    monkeypatch.delenv("LC_ALL", raising=False)  # take precedence over LANG (CI macOS sets LC_ALL=en_US.UTF-8)
    monkeypatch.delenv("LC_MESSAGES", raising=False)
    monkeypatch.setenv("LANG", "en_GB.UTF-8")
    return fake


def test_detect_backend_per_platform() -> None:
    have = {"say": "/usr/bin/say", "powershell": "C:/ps.exe", "espeak": "/usr/bin/espeak", "pico2wave": "/usr/bin/pico2wave"}
    which = have.get
    assert isinstance(detect_backend("Darwin", which), SayBackend)
    assert isinstance(detect_backend("Windows", which), SapiBackend)
    linux = detect_backend("Linux", which)
    assert isinstance(linux, EspeakBackend) and linux.name == "espeak"  # espeak-ng missing -> espeak
    assert isinstance(detect_backend("Linux", {"pico2wave": "/p"}.get), PicoBackend)
    assert detect_backend("Linux", {}.get) is None
    assert detect_backend("Darwin", {}.get) is None


def test_macos_say_backend(run: FakeRun) -> None:
    run.stdout = SAY_VOICES
    eng = SystemEngine(SayBackend("/usr/bin/say"))
    presets = eng.presets
    assert list(presets) == ["default", "Daniel (English (UK))", "Albert", "Zarvox"]  # locale first, deduped, no it_IT
    assert "Alice" in presets and "zarvox" in presets and "Nobody" not in presets  # any installed voice by name
    run.stdout = ""
    utt = eng.synthesize("-r 1 Hello there [[pbas 80]] friend", emotion="neutral", babble=False, preset="zarvox")
    call = run.calls[-1]
    assert call["cmd"][-2:] == ["-f", "-"] and "Hello" not in " ".join(call["cmd"])  # text on stdin, not argv
    assert call["input"] == "-r 1 Hello there [ [pbas 80] ] friend"
    assert call["cmd"][call["cmd"].index("-v") + 1] == "Zarvox" and "-r" in call["cmd"]
    assert call["timeout"] > 0
    assert utt.mode == "words" and utt.sample_rate == 22050  # neutral: no pitch shift
    assert utt.text.startswith("-r 1 Hello") and len(utt.beats) == 3 and utt.duration < 1.0  # silence trimmed
    assert max(abs(utt.audio)) == pytest.approx(0.9, abs=1e-3)
    excited = eng.synthesize("Wow", emotion="excited", babble=False, preset=None)
    assert excited.sample_rate > 22050 and "-v" not in run.calls[-1]["cmd"]  # pitch up by relabelling, default voice
    rate = int(run.calls[-1]["cmd"][run.calls[-1]["cmd"].index("-r") + 1])
    assert rate < round(180 * 1.2)  # rendered slower to compensate for the faster playback


def test_windows_sapi_backend(run: FakeRun) -> None:
    run.stdout = "Microsoft David Desktop\ten-US\nMicrosoft Hazel Desktop\ten-GB\nMicrosoft Hedda\tde-DE\n"
    eng = SystemEngine(SapiBackend("C:/powershell.exe"))
    assert list(eng.presets) == ["default", "Microsoft Hazel Desktop", "Microsoft David Desktop"]
    run.stdout = ""
    evil = "'; Remove-Item C:/ -Recurse; '$(calc)"
    eng.synthesize(evil, emotion="sleepy", babble=False, preset="microsoft hazel desktop")
    call = run.calls[-1]
    script = call["cmd"][-1]
    assert call["cmd"][:2] == ["C:/powershell.exe", "-NoProfile"] and "Remove-Item" not in script
    env = call["env"]
    assert env["NEBI_TTS_TEXT"] == evil and env["NEBI_TTS_VOICE"] == "Microsoft Hazel Desktop"
    assert int(env["NEBI_TTS_RATE"]) < 0 and "$env:NEBI_TTS_TEXT" in script  # sleepy: slower
    assert SapiBackend.sapi_rate(3.0) == 10 and SapiBackend.sapi_rate(1.0) == 0 and SapiBackend.sapi_rate(100) == 10


def test_linux_espeak_backend(run: FakeRun) -> None:
    run.stdout = ESPEAK_VOICES
    eng = SystemEngine(EspeakBackend("/usr/bin/espeak-ng", "espeak-ng"))
    assert list(eng.presets) == ["default", "en-gb", "en-us"]
    assert "de" in eng.presets and "en-us+robosoft" in eng.presets and "xx+m3" not in eng.presets
    run.stdout = ""
    utt = eng.synthesize("Hello", emotion="scared", babble=False, preset="EN-US+m3")
    cmd = run.calls[-1]["cmd"]
    assert cmd[-1] == "--stdin" and run.calls[-1]["input"] == "Hello"
    assert cmd[cmd.index("-v") + 1] == "en-us+m3" and int(cmd[cmd.index("-p") + 1]) > 50  # native pitch
    assert utt.sample_rate == 22050  # no relabelling with native pitch


def test_linux_pico_backend(run: FakeRun) -> None:
    eng = SystemEngine(PicoBackend("/usr/bin/pico2wave"))
    assert list(eng.presets) == ["default", "en-GB", "en-US"]
    eng.synthesize("Hallo", emotion="neutral", babble=False, preset=None)
    cmd = run.calls[-1]["cmd"]
    assert cmd[cmd.index("-l") + 1] == "en-GB" and cmd[-2:] == ["--", "Hallo"]


def test_errors_are_llm_friendly(run: FakeRun) -> None:
    eng = SystemEngine(SayBackend("/usr/bin/say"))
    run.stdout = SAY_VOICES
    with pytest.raises(ValueError, match="nothing to say"):
        eng.synthesize(" !!! ", emotion="neutral", babble=False, preset=None)
    with pytest.raises(ValueError, match="too long"):
        eng.synthesize("a" * 3000, emotion="neutral", babble=False, preset=None)
    with pytest.raises(tts.TtsError, match=r"unknown voice 'Nobody'.*Albert"):
        eng.synthesize("hi", emotion="neutral", babble=False, preset="Nobody")
    run.returncode, run.stderr = 1, "Voice `x' not found"
    with pytest.raises(tts.TtsError, match="exited with code 1: Voice"):
        eng.synthesize("hi", emotion="neutral", babble=False, preset=None)
    run.exc = subprocess.TimeoutExpired(["say"], 20)
    with pytest.raises(tts.TtsError, match="did not finish"):
        eng.synthesize("hi", emotion="neutral", babble=False, preset=None)
    run.exc = FileNotFoundError("say")
    with pytest.raises(tts.TtsError, match="not installed"):
        eng.synthesize("hi", emotion="neutral", babble=False, preset=None)


def test_voice_listing_failure_keeps_default(run: FakeRun) -> None:
    run.returncode = 1
    eng = SystemEngine(SayBackend("/usr/bin/say"))
    assert list(eng.presets) == ["default"] and "default" in eng.presets


def test_babble_and_emotions(run: FakeRun) -> None:
    run.stdout = SAY_VOICES
    eng = SystemEngine(SayBackend("/usr/bin/say"))
    babble = eng.synthesize(None, emotion="happy", babble=True, preset="Albert")
    assert babble.mode == "babble" and babble.emotion == "happy" and not run.calls  # chirp; no subprocess
    assert tuple(eng.emotions) == tts.EMOTIONS
    for emotion in tts.EMOTIONS:
        assert eng.synthesize("hi there", emotion=emotion, babble=False, preset=None).emotion == emotion
    assert eng.synthesize("hi", emotion="bogus", babble=False, preset=None).emotion == "neutral"


def test_registration_and_priority(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(tts.ENV_ENGINE, raising=False)
    monkeypatch.setenv(tts_system.ENV_SYSTEM, "0")
    assert register_system_engine(backend=SayBackend("/usr/bin/say")) is None
    monkeypatch.delenv(tts_system.ENV_SYSTEM)
    eng = register_system_engine(backend=SayBackend("/usr/bin/say"))
    try:
        assert eng is not None and tts.get_engine() is eng  # above chirp
        assert [e.name for e in tts.engines()] == ["system", "chirp"]
    finally:
        tts.unregister_engine("system")
    assert [e.name for e in tts.engines()] == ["chirp"]


def test_read_wav_formats(tmp_path: Path) -> None:
    p = tmp_path / "s.wav"
    speech_wav(p, rate=16000, channels=2)
    a, sr = read_wav(p)
    assert sr == 16000 and a.dtype == np.float32 and len(a) == 16000 and 0.4 < float(np.max(np.abs(a))) <= 0.5
    with wave.open(str(p), "wb") as w:  # 24-bit
        w.setnchannels(1)
        w.setsampwidth(3)
        w.setframerate(8000)
        w.writeframes(b"\x00\x00\x80" + b"\xff\xff\x7f")
    a, _ = read_wav(p)
    assert a.tolist() == pytest.approx([-1.0, 1.0], abs=1e-6)
    (tmp_path / "bad.wav").write_bytes(b"nope")
    with pytest.raises(tts.TtsError, match="no readable WAV"):
        read_wav(tmp_path / "bad.wav")


def test_read_audio_file(tmp_path: Path) -> None:
    p = tmp_path / "s.wav"
    speech_wav(p, rate=22050)
    utt = tts.read_audio_file(p, rate=48000)
    assert utt.sample_rate == 48000 and abs(utt.duration - 1.0) < 0.02
    with pytest.raises(tts.TtsError, match="cannot open"):
        tts.read_audio_file(tmp_path / "missing.wav")


def test_curate_falls_back_to_english() -> None:
    voices = [Voice("A", "en_US"), Voice("B", "fr_FR")]
    assert tts_system._curate(voices, "ja", "ja_JP") == ["default", "A"]


@pytest.mark.skipif(shutil.which("say") is None, reason="macOS `say` not available")
def test_real_macos_say() -> None:
    eng = SystemEngine(SayBackend(shutil.which("say") or "say"))
    assert len(eng.presets) >= 2
    utt = eng.synthesize("Hello, I am Nebi.", emotion="happy", babble=False, preset=None)
    assert 0.4 < utt.duration < 5.0 and utt.beats and utt.envelope
    assert float(np.sqrt(np.mean(utt.audio**2))) > 0.02  # actual sound, not silence
