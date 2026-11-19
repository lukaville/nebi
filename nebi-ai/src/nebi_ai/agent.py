"""DeepAgents-based pet robot agent, persistent memory wiring, and autonomous pet loop."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from deepagents import create_deep_agent
from deepagents.backends import FilesystemBackend
from langchain.chat_models import init_chat_model
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langgraph.checkpoint.memory import MemorySaver

from nebi_ai.bridge import RobotBridge
from nebi_ai.config import MemoryStore, PetConfig
from nebi_ai.tools import build_robot_tools

logger = logging.getLogger(__name__)


@dataclass
class TurnRecord:
    """One completed chat or autonomous pet turn, persisted in `history.jsonl` and streamed to the UI."""

    kind: str  # "chat" | "autonomy"
    timestamp: float
    prompt: str
    reply: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _ensure_provider_env(api_key_override: str | None = None, model: str = "") -> None:
    """Bridge `GEMINI_API_KEY` -> `GOOGLE_API_KEY` and apply optional runtime API key overrides."""
    if api_key_override:
        provider = model.split(":", 1)[0].lower() if ":" in model else "google_genai"
        if provider in ("google_genai", "google", "gemini"):
            os.environ["GOOGLE_API_KEY"] = api_key_override
        elif provider == "openai":
            os.environ["OPENAI_API_KEY"] = api_key_override
        elif provider == "anthropic":
            os.environ["ANTHROPIC_API_KEY"] = api_key_override
    if not os.environ.get("GOOGLE_API_KEY") and os.environ.get("GEMINI_API_KEY"):
        os.environ["GOOGLE_API_KEY"] = os.environ["GEMINI_API_KEY"]


def build_system_prompt(cfg: PetConfig) -> str:
    """Build the pet robot system prompt for DeepAgents."""
    return (
        f"You are {cfg.pet_name}, a small two-wheeled-legged self-balancing pet robot (Nebi / Beni LZ110) "
        "with a gimbal camera, onboard person/pet detection, LED eyes, a speaker, and a charging dock.\n\n"
        f"## Personality\n{cfg.personality}\n\n"
        "## How to behave as a pet\n"
        "- Speak out loud using `robot_say` in short, charming phrases (1-6 words) or `babble=True` chirps, "
        "paired with `robot_eyes`, `robot_emote`, or `robot_body`.\n"
        "- In your text reply to the human chat UI, you may be brief and friendly while staying in character.\n"
        "- Always follow robot safety rules: call `robot_look` before driving into unknown space, keep moves short "
        f"(at most {cfg.max_drive_m:.2f} m per drive), never flip or drive recklessly near edges/people, and sit/dock when battery is low.\n"
        "- If a tool returns an error, read its `hint` and adapt gently rather than looping.\n\n"
        "## Persistent Memory\n"
        "Your filesystem workspace is mapped directly to your persistent home-directory memory folder (`~/.nebi-ai/memories/`). "
        "`/MEMORY.md` is loaded automatically at startup. Whenever you learn a friend's name, preference, room detail, "
        "or memorable moment, update `/MEMORY.md` (or add files like `/people.md`, `/rooms.md`, `/journal.md`) using "
        "`read_file`, `edit_file`, or `write_file` so you remember across restarts.\n"
    )


def build_pet_agent(
    cfg: PetConfig,
    memory_store: MemoryStore,
    bridge: RobotBridge,
    *,
    model: BaseChatModel | str | None = None,
    api_key_override: str | None = None,
    is_autonomous_turn: Callable[[], bool] | None = None,
    checkpointer: Any = None,
) -> Any:
    """Construct a DeepAgents compiled graph with robot tools and home-directory persistent memory."""
    memory_store.ensure_default_memory(pet_name=cfg.pet_name)
    _ensure_provider_env(api_key_override=api_key_override, model=cfg.model)

    resolved_model: BaseChatModel | str = (
        model if model is not None else init_chat_model(cfg.model, temperature=cfg.temperature)
    )

    tools = build_robot_tools(bridge, lambda: cfg, is_autonomous_turn=is_autonomous_turn)
    backend = FilesystemBackend(root_dir=memory_store.memories_dir, virtual_mode=True)
    return create_deep_agent(
        model=resolved_model,
        tools=tools,
        system_prompt=build_system_prompt(cfg),
        memory=["/MEMORY.md"],
        backend=backend,
        checkpointer=checkpointer if checkpointer is not None else MemorySaver(),
        name=cfg.pet_name.lower().replace(" ", "_"),
    )


def _extract_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
        return "\n".join(p for p in parts if p).strip()
    return str(content).strip() if content is not None else ""


def _extract_turn_output(messages: list[BaseMessage], start_index: int) -> tuple[str, list[dict[str, Any]]]:
    new_msgs = messages[start_index:] if start_index < len(messages) else messages[-4:]
    tool_calls: list[dict[str, Any]] = []
    reply = ""
    for msg in new_msgs:
        if isinstance(msg, AIMessage):
            for tc in getattr(msg, "tool_calls", None) or []:
                tool_calls.append({"name": tc.get("name", ""), "args": tc.get("args", {})})
            text = _extract_text(msg.content)
            if text:
                reply = text
    return reply, tool_calls


class PetAgentSession:
    """Manages the DeepAgents pet instance, chat turns, autonomous life loop, and live event subscribers."""

    def __init__(
        self,
        bridge: RobotBridge,
        memory_store: MemoryStore | None = None,
        config: PetConfig | None = None,
        *,
        model_override: BaseChatModel | None = None,
    ) -> None:
        self.bridge = bridge
        self.memory_store = memory_store or MemoryStore()
        self.config = config or self.memory_store.load_config()
        self.memory_store.ensure_default_memory(pet_name=self.config.pet_name)
        self._model_override = model_override
        self._api_key_override: str | None = None
        self._checkpointer = MemorySaver()
        self._agent: Any = None
        self._agent_dirty = True
        self._turn_lock = asyncio.Lock()
        self._in_autonomous_turn = False
        self._autonomy_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._msg_count_by_thread: dict[str, int] = {}
        self.last_tick_at: float | None = None
        self.busy: bool = False

    def _ensure_agent(self) -> Any:
        if self._agent is None or self._agent_dirty:
            self._agent = build_pet_agent(
                self.config,
                self.memory_store,
                self.bridge,
                model=self._model_override,
                api_key_override=self._api_key_override,
                is_autonomous_turn=lambda: self._in_autonomous_turn,
                checkpointer=self._checkpointer,
            )
            self._agent_dirty = False
        return self._agent

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=100)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(q)

    def _broadcast(self, event_type: str, data: dict[str, Any]) -> None:
        payload = {"type": event_type, "timestamp": time.time(), "data": data}
        for q in list(self._subscribers):
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                pass

    def update_config(self, updates: dict[str, Any], *, api_key: str | None = None) -> PetConfig:
        """Apply parameter changes, persist to `config.json`, and mark the agent for rebuild if needed."""
        prev = self.config
        self.config = self.config.with_updates(updates)
        self.memory_store.save_config(self.config)
        if api_key is not None and api_key.strip():
            self._api_key_override = api_key.strip()
            self._agent_dirty = True
        if (
            self.config.model != prev.model
            or self.config.temperature != prev.temperature
            or self.config.personality != prev.personality
            or self.config.pet_name != prev.pet_name
            or self.config.max_drive_m != prev.max_drive_m
        ):
            self._agent_dirty = True
        self._broadcast("config", self.config.to_dict())
        return self.config

    async def _run_turn(self, prompt: str, *, kind: str) -> TurnRecord:
        async with self._turn_lock:
            self.busy = True
            self._in_autonomous_turn = kind == "autonomy"
            self._broadcast("status", {"busy": True, "kind": kind})
            try:
                agent = self._ensure_agent()
                thread_id = self.config.thread_id
                prev_count = self._msg_count_by_thread.get(thread_id, 0)
                result = await agent.ainvoke(
                    {"messages": [{"role": "user", "content": prompt}]},
                    config={"configurable": {"thread_id": thread_id}},
                )
                messages = list(result.get("messages") or [])
                self._msg_count_by_thread[thread_id] = len(messages)
                reply, tool_calls = _extract_turn_output(messages, prev_count)
                record = TurnRecord(
                    kind=kind,
                    timestamp=time.time(),
                    prompt=prompt,
                    reply=reply,
                    tool_calls=tool_calls,
                )
            except Exception as exc:
                logger.exception("Pet agent %s turn failed", kind)
                record = TurnRecord(
                    kind=kind,
                    timestamp=time.time(),
                    prompt=prompt,
                    reply="",
                    error=str(exc),
                )
            finally:
                self._in_autonomous_turn = False
                self.busy = False
                self._broadcast("status", {"busy": False, "kind": kind})

            self.memory_store.append_history(record.to_dict())
            self._broadcast("turn", record.to_dict())
            return record

    async def chat(self, message: str) -> TurnRecord:
        """Send a human chat message to the pet agent."""
        cleaned = message.strip()
        if not cleaned:
            raise ValueError("Chat message must not be empty")
        return await self._run_turn(cleaned, kind="chat")

    async def autonomy_tick(self) -> TurnRecord | None:
        """Execute one autonomous pet perception & reaction tick."""
        if not self.config.autonomy_enabled or self.config.is_quiet_now():
            return None
        state_env = await self.bridge.state()
        events = await self.bridge.poll_events()
        self.last_tick_at = time.time()

        state_summary = json.dumps(state_env.get("state") or state_env.get("result") or {}, ensure_ascii=False)
        events_summary = json.dumps(events[-8:], ensure_ascii=False) if events else "[]"
        prompt = (
            "[AUTONOMOUS PET TICK]\n"
            f"Current robot state: {state_summary}\n"
            f"Recent events since last tick: {events_summary}\n"
            "Decide as a charming pet robot whether to look around, greet someone who appeared, "
            "show subtle eyes/body language, jot something important into `/MEMORY.md`, or simply rest quietly. "
            "Do not spam speech if nothing interesting changed; a brief 1-sentence internal note is fine when resting."
        )
        return await self._run_turn(prompt, kind="autonomy")

    async def emergency_stop(self) -> dict[str, Any]:
        """Immediately halt robot motion and pause autonomous ticks."""
        self.update_config({"autonomy_enabled": False})
        res = await self.bridge.call("stop", {})
        self._broadcast("emergency_stop", {"result": res})
        return res

    def start_autonomy(self) -> None:
        """Start the background autonomous pet loop task."""
        if self._autonomy_task is not None and not self._autonomy_task.done():
            return
        self._stop_event.clear()
        self._autonomy_task = asyncio.create_task(self._autonomy_runner(), name="nebi-ai-autonomy")

    async def stop_autonomy(self) -> None:
        """Stop the background autonomous pet loop task."""
        self._stop_event.set()
        if self._autonomy_task is not None:
            self._autonomy_task.cancel()
            try:
                await self._autonomy_task
            except asyncio.CancelledError:
                pass
            self._autonomy_task = None

    async def _autonomy_runner(self) -> None:
        # Short initial delay so the Web UI / robot connection settles first.
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=3.0)
            return
        except asyncio.TimeoutError:
            pass

        while not self._stop_event.is_set():
            if self.config.autonomy_enabled and not self.config.is_quiet_now() and not self.busy:
                try:
                    await self.autonomy_tick()
                except Exception:
                    logger.exception("Unhandled error in autonomy tick")
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self.config.autonomy_interval_sec)
            except asyncio.TimeoutError:
                continue
