"""Pluggable TTS (library side): built-in chirp engine and registry selection (name / env / priority).
Extension registration and `nebi say --engine` are tested in nebi-cli."""
from __future__ import annotations

import numpy as np
import pytest

from nebi import tts
from nebi.extensions import SpeechAudio


class FakeEngine:
    name = "fake"
    emotions = ("neutral", "happy")
    presets = ("plain", "loud")
    priority = 50

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[str | None, str, bool, str | None]] = []

    def synthesize(self, text: str | None, *, emotion: str, babble: bool, preset: str | None) -> SpeechAudio:
        self.calls.append((text, emotion, babble, preset))
        if self.fail:
            raise tts.TtsError("model missing")
        audio = (0.3 * np.sin(np.linspace(0, 400, 8000))).astype(np.float32)
        return SpeechAudio(audio, 16000, text=text or "", emotion=emotion, mode="babble" if babble else "words",
                           beats=[(0.0, 1.0), (0.25, 0.6)])




def test_builtin_chirp_engine() -> None:
    eng = tts.get_engine("chirp")
    words = eng.synthesize("Hello there friend?", emotion="curious", babble=False, preset=None)
    assert words.mode == "words" and words.text == "Hello there friend?" and words.sample_rate == 24000
    assert len(words.beats) >= 3 and words.duration > 0.4 and words.envelope and max(words.envelope) == 1.0
    babble = eng.synthesize(None, emotion="sad", babble=True, preset="deep")
    assert babble.mode == "babble" and babble.text == "" and babble.beats
    assert len(babble.pcm16(48000)) == 2 * round(len(babble.audio) * 48000 / babble.sample_rate)
    with pytest.raises(ValueError):
        eng.synthesize("!!!", emotion="happy", babble=False, preset=None)
    with pytest.raises(tts.TtsError):
        eng.synthesize("hi", emotion="happy", babble=True, preset="nope")
    for emotion in tts.EMOTIONS:  # every emotion renders
        assert eng.synthesize(None, emotion=emotion, babble=True, preset=None).duration > 0.1
    assert tts.split_emoji("Ball? 👀") == ("Ball?", "curious")
    assert SpeechAudio(np.array([0, 16384], np.int16), 8000).audio.dtype == np.float32  # int16 accepted


def test_registry_selection(monkeypatch) -> None:
    monkeypatch.delenv(tts.ENV_ENGINE, raising=False)
    assert tts.get_engine().name == "chirp"
    fake = FakeEngine()
    tts.register_engine(fake)
    try:
        assert tts.get_engine().name == "fake"  # higher priority wins
        assert tts.get_engine("chirp").name == "chirp"
        monkeypatch.setenv(tts.ENV_ENGINE, "chirp")
        assert tts.get_engine().name == "chirp"
        with pytest.raises(KeyError, match="registered: fake, chirp"):
            tts.get_engine("nope")
        with pytest.raises(ValueError):
            tts.register_engine(FakeEngine())  # another object under the same name
    finally:
        tts.unregister_engine("fake")
    assert [e.name for e in tts.engines()] == ["chirp"]




