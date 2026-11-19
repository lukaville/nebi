"""Unit and end-to-end tests for `nebi-ai` (config, memory, bridge, tools, DeepAgents, web UI, CLI)."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from nebi_ai.agent import PetAgentSession
from nebi_ai.bridge import RobotBridge, RobotTarget
from nebi_ai.cli import main as cli_main
from nebi_ai.config import MemoryStore, PetConfig
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
    assert cfg.pet_name == "Nebi"
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

        # Look works and caches a JPEG snapshot
        look_res = json.loads(await tools["robot_look"].ainvoke({"annotate": True}))
        assert look_res["ok"] is True
        snap = bridge.read_last_snapshot_bytes()
        assert snap is not None and snap.startswith(b"\xff\xd8")
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

        tick = await session.autonomy_tick()
        assert tick is not None
        assert tick.kind == "autonomy"
        assert tick.error is None

        history = store.load_history()
        assert len(history) == 2
        assert history[0]["kind"] == "chat"
        assert history[1]["kind"] == "autonomy"

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
        assert info["config"]["pet_name"] == "Nebi"

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

        # Memory CRUD
        assert client.put("/api/memories/notes.md", json={"content": "Likes sunny spots"}).status_code == 200
        assert client.get("/api/memories/notes.md").json()["content"] == "Likes sunny spots"
        assert client.get("/api/memories/../../etc/passwd").status_code in (400, 404)
        assert client.delete("/api/memories/notes.md").json()["deleted"] is True


def test_cli_memory_commands(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    home = str(tmp_path / "cli_home")
    assert cli_main(["memory", "--memory-dir", home, "list"]) == 0
    assert "MEMORY.md" in capsys.readouterr().out

    assert cli_main(["memory", "--memory-dir", home, "show", "MEMORY.md"]) == 0
    assert "Nebi Pet Memory" in capsys.readouterr().out

    assert cli_main(["memory", "--memory-dir", home, "reset"]) == 0
    assert "Reset memories" in capsys.readouterr().out
