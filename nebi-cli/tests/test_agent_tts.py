"""Pluggable TTS: built-in chirp engine, registry selection (name / env / priority), extension registration,
`say --engine`, and the robot-sound fallback when an engine fails."""
from __future__ import annotations

import numpy as np
from fastapi.testclient import TestClient

from nebi import tts
from nebi_cli import spec as S
from nebi_cli.daemon import create_agent_app
from nebi_cli.extensions import AgentExtensionContext, SpeechAudio


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


class TtsExtension:
    name = "tts-test"

    def __init__(self, engine: FakeEngine) -> None:
        self.engine = engine

    def setup_agent(self, ctx: AgentExtensionContext) -> None:
        ctx.register_tts_engine(self.engine)






def test_extension_engine_and_say_engine_flag(extensions, monkeypatch) -> None:
    monkeypatch.delenv(tts.ENV_ENGINE, raising=False)
    fake = FakeEngine()
    extensions(TtsExtension(fake))
    app, _runtime = create_agent_app(port=0)
    with TestClient(app) as http:

        def call(cmd: str, **args):
            return http.post("/agent/call", json={"command": cmd, "args": args}).json()

        assert call("connect", sim=True)["ok"]
        r = call("say", text="Hi 😊", to="robot")
        assert r["ok"] and r["result"]["engine"] == "fake" and r["result"]["spoken"] == "Hi", r
        assert fake.calls[-1][:3] == ("Hi", "happy", False)
        r = call("say", text="Hi", emotion="scared", to="robot")  # fake has no "scared"
        assert r["ok"] and r["result"]["emotion"] == "neutral" and any("no 'scared' voice" in w for w in r["warnings"])
        r = call("say", text="Hi", engine="chirp", preset="tiny", to="robot")
        assert r["ok"] and r["result"]["engine"] == "chirp"
        assert call("say", text="Hi", engine="nope")["exit_code"] == S.EXIT_USAGE
        assert call("say", text="Hi", preset="tiny")["exit_code"] == S.EXIT_USAGE  # not a preset of "fake"
        # engine failure: a robot sound effect instead
        fake.fail = True
        r = call("say", text="Hi", emotion="happy", to="robot")
        assert r["ok"] and r["result"]["mode"] == "sound" and r["result"]["fallback_sound"] == 3
        assert any("played a robot sound" in w for w in r["warnings"])
        r = call("emote", emotion="happy")
        assert r["ok"], r
    assert [e.name for e in tts.engines()] == ["chirp"]  # unregistered with the extension


def test_say_argument_spec() -> None:
    args = {a.name: a for a in S.COMMANDS_BY_NAME["say"].args}
    assert "engine" in args and args["preset"].default is None


def test_tools_say_renders_through_the_registry(tmp_path, monkeypatch) -> None:
    """`nebi tools say` uses the nebi.tts registry for text and PyAV for audio files (no ffmpeg binary)."""
    import wave

    import pytest

    from nebi_cli.intercom_say import synthesize_pcm

    monkeypatch.delenv(tts.ENV_ENGINE, raising=False)
    pcm = synthesize_pcm(text="Hello robot", engine="chirp", voice="tiny")
    assert len(pcm) > 48000 and len(pcm) % 2 == 0  # > 0.5 s of s16le 48 kHz
    with pytest.raises(RuntimeError, match="no voice 'opera'"):
        synthesize_pcm(text="hi", engine="chirp", voice="opera")
    with pytest.raises(RuntimeError, match="unknown TTS engine"):
        synthesize_pcm(text="hi", engine="nope")
    p = tmp_path / "a.wav"
    with wave.open(str(p), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(np.zeros(16000 * 2, "<i2").tobytes())
    assert abs(len(synthesize_pcm(wav=str(p))) - 96000) < 2000  # 1 s resampled to 48 kHz mono
