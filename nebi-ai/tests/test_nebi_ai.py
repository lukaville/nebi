"""Unit and end-to-end tests for `nebi-ai` (config, memory, bridge, tools, DeepAgents, web UI, CLI)."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from nebi_ai.agent import PetAgentSession, match_wake_word
from nebi_ai.bridge import RobotBridge, RobotTarget
from nebi_ai.cli import main as cli_main
from nebi_ai.config import DEFAULT_LIVE_MODEL, DEFAULT_MODEL, MemoryStore, PetConfig
from nebi_ai.live import SpeechBandFilter, build_live_tool_declarations, pcm24k_to_48k, pcm48k_to_16k
from nebi_ai.tools import build_robot_tools
from nebi_ai.web import create_web_app


class _FakePetChatModel(BaseChatModel):
    """Deterministic chat model that calls one robot tool on the first step, then replies."""

    tool_to_call: str = "robot_state"
    tool_args: dict[str, Any] = Field(default_factory=dict)
    final_reply: str = "Beep! Happy to see you!"

    @property
    def _llm_type(self) -> str:
        return "fake-pet-chat"

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> _FakePetChatModel:
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        # If the last message is already a tool result, produce the final pet response.
        if messages and isinstance(messages[-1], ToolMessage):
            ai = AIMessage(content=self.final_reply)
        else:
            ai = AIMessage(
                content="",
                tool_calls=[{"name": self.tool_to_call, "args": dict(self.tool_args), "id": "call_1", "type": "tool_call"}],
            )
        return ChatResult(generations=[ChatGeneration(message=ai)])


def test_config_and_memory_store(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "home")
    cfg = store.load_config()
    assert cfg.pet_name == "Beni"
    assert store.config_path.is_file()

    updated = cfg.with_updates({"temperature": 5.0, "max_drive_m": 99.0, "quiet_hours": "22-8"})
    assert updated.temperature == 2.0
    assert updated.max_drive_m == 3.0
    assert updated.is_quiet_now(datetime(2026, 11, 19, 23, 0))
    assert updated.is_quiet_now(datetime(2026, 11, 19, 7, 30))
    assert not updated.is_quiet_now(datetime(2026, 11, 19, 14, 0))

    store.save_config(updated)
    assert store.load_config().quiet_hours == "22-8"

    mem = store.ensure_default_memory("Mochi")
    assert mem.is_file()
    assert "Mochi" in store.read_memory("MEMORY.md")

    store.write_memory("people.md", "- Alex likes blue LEDs\n")
    names = [f["path"] for f in store.list_memories()]
    assert "MEMORY.md" in names and "people.md" in names

    with pytest.raises(ValueError, match="escapes"):
        store.read_memory("../config.json")

    assert store.delete_memory("people.md") is True
    assert store.delete_memory("people.md") is False


@pytest.mark.asyncio
async def test_bridge_tools_and_safety_gates_on_simulator() -> None:
    bridge = RobotBridge(RobotTarget(sim=True))
    await bridge.start()
    try:
        st = await bridge.state()
        assert st["ok"] is True

        cfg = PetConfig(max_drive_m=0.5, voice_enabled=False, autonomy_allow_drive=False)
        auto_flag = {"auto": True}
        tools = {t.name: t for t in build_robot_tools(bridge, lambda: cfg, is_autonomous_turn=lambda: auto_flag["auto"])}

        # Voice disabled blocks robot_say
        say_res = json.loads(await tools["robot_say"].ainvoke({"text": "hi"}))
        assert say_res["ok"] is False
        assert say_res["error"]["code"] == "VOICE_DISABLED"

        # Autonomy locomotion lock blocks robot_drive during autonomous ticks
        drive_res = json.loads(await tools["robot_drive"].ainvoke({"direction": "forward", "distance_m": 0.3}))
        assert drive_res["ok"] is False
        assert drive_res["error"]["code"] == "AUTONOMY_LOCOMOTION_LOCKED"

        # Look returns multimodal content (JSON text + base64 JPEG image_url) and caches a JPEG snapshot
        look_res = await tools["robot_look"].ainvoke({"annotate": True})
        assert isinstance(look_res, list) and len(look_res) == 2
        assert json.loads(look_res[0]["text"])["ok"] is True
        assert look_res[1]["type"] == "image_url"
        assert look_res[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
        snap = bridge.read_last_snapshot_bytes()
        assert snap is not None and snap.startswith(b"\xff\xd8")

        # Emote, eyes, schema, and generic robot_command (plus blocked commands)
        assert json.loads(await tools["robot_emote"].ainvoke({"emotion": "happy"}))["ok"] is True
        assert json.loads(await tools["robot_eyes"].ainvoke({"what": "happy", "hold": 2.0}))["ok"] is True
        assert json.loads(await tools["robot_schema"].ainvoke({"name": "listen"}))["ok"] is True
        assert json.loads(await tools["robot_command"].ainvoke({"command": "state", "args_json": "{}"}))["ok"] is True
        blocked = json.loads(await tools["robot_command"].ainvoke({"command": "estop"}))
        assert blocked["ok"] is False and blocked["error"]["code"] == "COMMAND_BLOCKED"
    finally:
        await bridge.stop()


@pytest.mark.asyncio
async def test_deepagent_chat_and_autonomy_tick(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "home")
    bridge = RobotBridge(RobotTarget(sim=True))
    await bridge.start()
    try:
        fake_llm = _FakePetChatModel(tool_to_call="robot_state", final_reply="Chirp! Battery looks great!")
        session = PetAgentSession(bridge, memory_store=store, model_override=fake_llm)

        rec = await session.chat("How are you feeling?")
        assert rec.error is None
        assert rec.reply == "Chirp! Battery looks great!"
        assert [tc["name"] for tc in rec.tool_calls] == ["robot_state"]
        assert rec.tool_calls[0]["ok"] is True
        assert isinstance(rec.tool_calls[0]["output"], dict)

        # Gemini Live tool dispatch + ask_deep_agent delegation
        eye_out, eye_ok = await session.live._execute_live_tool("robot_eyes", {"what": "happy", "hold": 2.0})
        assert eye_ok is True and eye_out["ok"] is True
        drv_out, drv_ok = await session.live._execute_live_tool("robot_drive", {"direction": "forward", "distance_m": 0.2})
        assert drv_ok is True and drv_out["ok"] is True
        trn_out, trn_ok = await session.live._execute_live_tool("robot_turn", {"direction": "left", "degrees": 30})
        assert trn_ok is True and trn_out["ok"] is True
        tlt_out, tlt_ok = await session.live._execute_live_tool("robot_tilt", {"direction": "center"})
        assert tlt_ok is True and tlt_out["ok"] is True
        snd_out, snd_ok = await session.live._execute_live_tool("robot_sound", {"sound_id": "chirp"})
        assert snd_ok is True and snd_out["ok"] is True

        delegated_out, delegated_ok = await session.live._execute_live_tool(
            "ask_deep_agent", {"task": "Check robot status and remember I said hi"}
        )
        assert delegated_ok is True
        assert delegated_out["reply"] == "Chirp! Battery looks great!"

        tick = await session.autonomy_tick()
        assert tick is not None
        assert tick.kind == "autonomy"
        assert tick.error is None

        history = store.load_history()
        assert len(history) == 3
        assert [h["kind"] for h in history] == ["chat", "deepagent", "autonomy"]

        # Emergency stop pauses autonomy
        await session.emergency_stop()
        assert session.config.autonomy_enabled is False
        assert await session.autonomy_tick() is None
    finally:
        await bridge.stop()


def test_web_ui_endpoints(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "home")
    bridge = RobotBridge(RobotTarget(sim=True))
    fake_llm = _FakePetChatModel(tool_to_call="robot_state", final_reply="Hi from web UI!")
    session = PetAgentSession(bridge, memory_store=store, model_override=fake_llm)
    app = create_web_app(session, manage_lifecycle=True, start_autonomy=False)

    with TestClient(app) as client:
        r_html = client.get("/")
        assert r_html.status_code == 200 and "nebi-ai" in r_html.text

        info = client.get("/api/info").json()
        assert info["target"] == "simulator"
        assert info["config"]["pet_name"] == "Beni"
        assert info["config"]["wake_word_enabled"] is False
        assert info["config"]["idle_token_saver"] is False
        assert info["config"]["model"] == DEFAULT_MODEL
        assert info["config"]["live_model"] == DEFAULT_LIVE_MODEL
        assert info["live"]["mode"] == "standby"
        assert any(p["id"] == "gemini-3.8-live" for p in info["live_model_presets"])

        # Audio clips API
        store.save_wav_clip("sample.wav", b"\x00\x00" * 1600, sample_rate=16000)
        clips = client.get("/api/audio/clips").json()
        assert any(c["name"] == "sample.wav" for c in clips["items"])
        wav_resp = client.get("/api/audio/clips/sample.wav")
        assert wav_resp.status_code == 200 and wav_resp.headers["content-type"] == "audio/wav"
        assert client.get("/api/audio/clips/..%2Fconfig.json").status_code in (400, 404)
        assert client.get("/api/audio/clips/config.json").status_code == 400

        st = client.get("/api/state").json()
        assert st["ok"] is True

        # Update config with an API key override and ensure the secret is never echoed back
        upd = client.post(
            "/api/config",
            json={"updates": {"pet_name": "Pip", "temperature": 0.4}, "api_key": "secret-test-key"},
        ).json()
        assert upd["config"]["pet_name"] == "Pip"
        assert upd["has_api_key"] is True
        assert "secret-test-key" not in json.dumps(upd)

        # Chat turn
        chat = client.post("/api/chat", json={"message": "Hello Pip!"}).json()
        assert chat["reply"] == "Hi from web UI!"
        assert chat["tool_calls"][0]["name"] == "robot_state"

        # Quick action: look + camera JPEG
        assert client.post("/api/action", json={"action": "look"}).json()["ok"] is True
        cam = client.get("/api/camera.jpg")
        assert cam.status_code == 200 and cam.headers["content-type"] == "image/jpeg"

        # Memory CRUD + Session reset (clears chat history while keeping persistent memories)
        assert client.put("/api/memories/notes.md", json={"content": "Likes sunny spots"}).status_code == 200
        assert len(client.get("/api/history").json()["items"]) == 1
        rst = client.post("/api/session/reset?clear_history=true").json()
        assert rst["ok"] is True
        assert client.get("/api/history").json()["items"] == []
        assert client.get("/api/memories/notes.md").json()["content"] == "Likes sunny spots"
        assert client.get("/api/memories/../../etc/passwd").status_code in (400, 404)
        assert client.delete("/api/memories/notes.md").json()["deleted"] is True


def test_cli_memory_commands(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    home = str(tmp_path / "cli_home")
    assert cli_main(["memory", "--memory-dir", home, "list"]) == 0
    assert "MEMORY.md" in capsys.readouterr().out

    assert cli_main(["memory", "--memory-dir", home, "show", "MEMORY.md"]) == 0
    assert "Beni Pet Memory" in capsys.readouterr().out

    assert cli_main(["memory", "--memory-dir", home, "reset"]) == 0
    assert "Reset memories" in capsys.readouterr().out


def test_live_audio_resampling_and_wake_word() -> None:
    # 960 samples (20 ms @ 48 kHz) -> 320 samples (20 ms @ 16 kHz)
    t48 = np.arange(960, dtype=np.float64) / 48000.0
    tone48 = (np.sin(2.0 * np.pi * 440.0 * t48) * 12000).astype("<i2").tobytes()
    out16 = pcm48k_to_16k(tone48)
    assert len(out16) == 320 * 2

    # 480 samples (20 ms @ 24 kHz) -> 960 samples (20 ms @ 48 kHz)
    t24 = np.arange(480, dtype=np.float64) / 24000.0
    tone24 = (np.sin(2.0 * np.pi * 440.0 * t24) * 12000).astype("<i2").tobytes()
    out48 = pcm24k_to_48k(tone24)
    assert len(out48) == 960 * 2
    assert pcm48k_to_16k(b"") == b""
    assert pcm24k_to_48k(b"") == b""

    # Wake word matching
    assert match_wake_word("Beni, please stand up!", "beni, benny, nebi")[0] is True
    assert match_wake_word("hey benny look at me", "beni, benny, nebi")[0] is True
    assert match_wake_word("pass the salt please", "beni, benny, nebi")[0] is False

    # Live tool schema declarations include fast robot tools + ask_deep_agent
    tools = build_live_tool_declarations()
    names = {fd.name for fd in (tools[0].function_declarations or [])}
    assert {"robot_look", "robot_stand", "robot_sit", "robot_eyes", "robot_body", "ask_deep_agent"} <= names

    # SpeechBandFilter suppresses 4.4 kHz charger whine relative to 1 kHz voice
    t16 = np.arange(8000, dtype=np.float64) / 16000.0
    voice_1k = (np.sin(2.0 * np.pi * 1000.0 * t16) * 4000).astype("<i2").tobytes()
    whine_4k4 = (np.sin(2.0 * np.pi * 4400.0 * t16) * 4000).astype("<i2").tobytes()
    f_voice = SpeechBandFilter()
    f_whine = SpeechBandFilter()
    _, rms_voice = f_voice.process(voice_1k)
    _, rms_whine = f_whine.process(whine_4k4)
    assert rms_voice > rms_whine * 2.0
