"""The nebi TTS adapter (`nebi_voice.extension`). Rendering tests skip when the model is not on disk."""

from __future__ import annotations

import threading
from typing import Any

import numpy as np
import pytest

pytest.importorskip("nebi")

from nebi.tts import EMOTIONS as PYNEBI_EMOTIONS
from nebi.tts import SpeechAudio, TtsEngine, TtsError, get_engine, unregister_engine
from nebi_voice import PRESETS, extension, find_model
from nebi_voice import engine as voice_engine
from nebi_voice.engine import ModelUnavailableError
from nebi_voice.extension import NebiVoiceEngine, NebiVoiceExtension

needs_model = pytest.mark.skipif(find_model() is None, reason="tiny-voca model not downloaded (run `nebi-voice download-model`)")


@pytest.fixture
def eng() -> NebiVoiceEngine:
    return NebiVoiceEngine()


def test_engine_contract(eng: NebiVoiceEngine) -> None:
    assert isinstance(eng, TtsEngine)
    assert eng.name == "nebi-voice" and eng.priority == 20
    assert eng.presets[0] == "robot" and sorted(eng.presets) == sorted(PRESETS) and "nebi" in eng.presets
    assert set(PYNEBI_EMOTIONS) <= set(eng.emotions)


def test_unknown_preset_is_tts_error(eng: NebiVoiceEngine) -> None:
    with pytest.raises(TtsError, match="unknown nebi-voice preset"):
        eng.synthesize("Hi", emotion="neutral", babble=False, preset="nope")


def test_extension_registers_default_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    ext = NebiVoiceExtension()
    monkeypatch.setattr(NebiVoiceEngine, "prefetch", lambda self: None)
    try:
        ext.setup_server(None)
        ext.setup_agent(object())  # idempotent: same engine, replace=True
        assert ext.engine is not None and get_engine() is ext.engine and get_engine("nebi-voice") is ext.engine
        assert ext.name == "nebi-voice" and ext.version and "nebi-voice" in ext.guide_markdown()
    finally:
        if ext.engine is not None:
            unregister_engine("nebi-voice", ext.engine)


def test_downloading_model_asks_to_retry(eng: NebiVoiceEngine, monkeypatch: pytest.MonkeyPatch) -> None:
    release = threading.Event()
    eng._prefetch = threading.Thread(target=release.wait, daemon=True)
    eng._prefetch.start()
    monkeypatch.setattr(extension, "DOWNLOAD_WAIT_S", 0.05)
    try:
        with pytest.raises(TtsError, match="voice model downloading"):
            eng.synthesize("Hi", emotion="neutral", babble=False, preset=None)
    finally:
        release.set()


def test_missing_model_is_tts_error_with_hint(eng: NebiVoiceEngine, monkeypatch: pytest.MonkeyPatch) -> None:
    def no_model(*_a: Any, **_k: Any) -> None:
        raise ModelUnavailableError("cannot download the tiny-voca voice model", "check the internet connection")

    monkeypatch.setattr(extension, "RobotVoice", no_model)
    with pytest.raises(TtsError, match="check the internet connection"):
        eng.synthesize("Hi", emotion="neutral", babble=False, preset=None)


def test_offline_download_fails_cleanly(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    target = tmp_path / "tiny-voca"
    assert voice_engine.find_model(target) is None
    with pytest.raises(ModelUnavailableError) as info:
        voice_engine.ensure_model(target, download=False)
    assert "download-model" in info.value.hint
    monkeypatch.setenv(voice_engine.ENV_MODEL_DIR, str(target))
    assert voice_engine.default_model_dir() == target and voice_engine.find_model() is None


@needs_model
def test_speech_audio_conversion(eng: NebiVoiceEngine) -> None:
    a = eng.synthesize("Hello, I am Nebi!", emotion="happy", babble=False, preset="nebi")
    assert isinstance(a, SpeechAudio)
    assert a.mode == "words" and a.text == "Hello, I am Nebi!" and a.emotion == "happy"
    assert a.sample_rate == 24000 and a.audio.dtype == np.float32 and a.duration > 0.5
    assert a.beats and all(isinstance(t, float) and 0.0 <= s <= 1.0 for t, s in a.beats)
    assert a.color is not None and len(a.color) == 3


@needs_model
def test_babble(eng: NebiVoiceEngine) -> None:
    a = eng.synthesize(None, emotion="curious", babble=True, preset=None)
    assert a.mode == "babble" and a.text == "" and a.emotion == "curious" and a.duration > 0.2


@needs_model
def test_no_words_is_value_error_and_unknown_emotion_is_neutral(eng: NebiVoiceEngine) -> None:
    with pytest.raises(ValueError):
        eng.synthesize("...", emotion="neutral", babble=False, preset=None)
    assert eng.synthesize("Hi", emotion="bored", babble=False, preset=None).emotion == "neutral"
