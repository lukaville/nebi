"""DeepAgents-based pet robot agent, persistent memory wiring, and autonomous pet loop."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from deepagents import create_deep_agent
from deepagents.backends import FilesystemBackend
from langchain.chat_models import init_chat_model
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import MemorySaver

from nebi_ai.bridge import RobotBridge
from nebi_ai.config import MemoryStore, PetConfig
from nebi_ai.live import GeminiLiveManager, resolve_gemini_api_key
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
    tokens: int = 0
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
        "- **See**: call `robot_look` to capture and visually inspect the live camera frame (the image is returned directly to you) along with detected people/pets.\n"
        "- **Hear**: continuous listening automatically delivers spoken words as `[Heard via microphone]: ...`. Whenever you receive `[Heard via microphone]: ...`, the human just spoke to you out loud — ALWAYS reply out loud via `robot_say` (and optional `robot_eyes`/`robot_body`/`robot_emote`) so they hear you respond! You can also call `robot_listen` explicitly when waiting for an answer.\n"
        "- **Speak & express**: use `robot_say` in short, charming phrases (1-6 words) or `babble=True` chirps, or `robot_sound`, paired with `robot_eyes`, `robot_emote`, and `robot_body`.\n"
        "- **Feel & sense**: use `robot_sense`, `robot_feel`, `robot_care`, `robot_who`, and `robot_events` to stay aware of your body and surroundings. Use `robot_schema` + `robot_command` for any additional extension commands.\n"
        "- In your text reply to the human chat UI, stay brief, warm, and in character.\n"
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
    get_config: Callable[[], PetConfig] | None = None,
    is_autonomous_turn: Callable[[], bool] | None = None,
    transcribe_wav: Callable[[str], Awaitable[str]] | None = None,
    checkpointer: Any = None,
) -> Any:
    """Construct a DeepAgents compiled graph with robot tools and home-directory persistent memory."""
    memory_store.ensure_default_memory(pet_name=cfg.pet_name)
    _ensure_provider_env(api_key_override=api_key_override, model=cfg.model)

    resolved_model: BaseChatModel | str = (
        model if model is not None else init_chat_model(cfg.model, temperature=cfg.temperature)
    )

    tools = build_robot_tools(
        bridge,
        get_config or (lambda: cfg),
        is_autonomous_turn=is_autonomous_turn,
        transcribe_wav=transcribe_wav,
    )
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


def _sanitize_tool_output(content: Any) -> tuple[Any, bool | None]:
    """Convert a `ToolMessage.content` (text or multimodal blocks) into clean JSON-safe output + `ok` flag."""
    text_part = ""
    has_image = False
    if isinstance(content, str):
        text_part = content.strip()
    elif isinstance(content, list):
        chunks: list[str] = []
        for block in content:
            if isinstance(block, str):
                chunks.append(block)
            elif isinstance(block, dict):
                btype = block.get("type")
                if btype == "text":
                    chunks.append(str(block.get("text", "")))
                elif btype in ("image_url", "media", "image"):
                    has_image = True
        text_part = "\n".join(c for c in chunks if c).strip()
    elif content is not None:
        text_part = str(content).strip()

    if text_part:
        try:
            parsed = json.loads(text_part)
            if isinstance(parsed, dict):
                if has_image:
                    parsed = {**parsed, "_attached_image": True}
                ok_val = parsed.get("ok")
                return parsed, (bool(ok_val) if isinstance(ok_val, bool) else None)
            return parsed, None
        except json.JSONDecodeError:
            pass
    if has_image and not text_part:
        return {"_attached_image": True}, True
    return text_part[:4000], None


def _extract_turn_output(messages: list[BaseMessage], start_index: int) -> tuple[str, list[dict[str, Any]], int]:
    new_msgs = messages[start_index:] if start_index < len(messages) else messages[-6:]
    tool_outputs_by_id: dict[str, tuple[Any, bool | None]] = {}
    for msg in new_msgs:
        if isinstance(msg, ToolMessage):
            tc_id = str(getattr(msg, "tool_call_id", "") or "")
            if tc_id:
                tool_outputs_by_id[tc_id] = _sanitize_tool_output(msg.content)

    tool_calls: list[dict[str, Any]] = []
    reply = ""
    turn_tokens = 0
    for msg in new_msgs:
        if isinstance(msg, AIMessage):
            usage = getattr(msg, "usage_metadata", None)
            if isinstance(usage, dict):
                turn_tokens += int(usage.get("total_tokens") or 0)
            for tc in getattr(msg, "tool_calls", None) or []:
                tc_id = str(tc.get("id", "") or "")
                entry: dict[str, Any] = {
                    "name": tc.get("name", ""),
                    "args": tc.get("args", {}),
                }
                if tc_id in tool_outputs_by_id:
                    output_val, ok_val = tool_outputs_by_id[tc_id]
                    entry["output"] = output_val
                    if ok_val is not None:
                        entry["ok"] = ok_val
                tool_calls.append(entry)
            text = _extract_text(msg.content)
            if text:
                reply = text
    return reply, tool_calls, turn_tokens


def match_wake_word(text: str, wake_words_csv: str) -> tuple[bool, str]:
    """Check whether `text` contains any configured wake word (e.g. 'beni', 'beni beni', 'hey beni').

    Returns `(matched, cleaned_text)`. If the utterance contains a wake word, returns `(True, text)`.
    """
    words = [w.strip().lower() for w in wake_words_csv.split(",") if w.strip()]
    if not words:
        return True, text.strip()
    lowered = text.lower()
    for w in words:
        pattern = rf"\b{re.escape(w)}\b"
        if re.search(pattern, lowered):
            return True, text.strip()
    return False, text.strip()


_MAX_THREAD_MESSAGES = 60


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
        self._stt_model: BaseChatModel | None = None
        self._checkpointer = MemorySaver()
        self._agent: Any = None
        self._agent_dirty = True
        self._turn_lock = asyncio.Lock()
        self._listen_lock = asyncio.Lock()
        self._turn_epoch: int = 0
        self._in_autonomous_turn = False
        self._autonomy_task: asyncio.Task[None] | None = None
        self._listen_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._msg_count_by_thread: dict[str, int] = {}
        self._followup_until: float = 0.0
        self._last_autonomy_sig: tuple[Any, ...] | None = None
        self.last_tick_at: float | None = None
        self.busy: bool = False
        self.total_tokens: int = 0
        self.live = GeminiLiveManager(
            bridge=self.bridge,
            memory_store=self.memory_store,
            get_config=lambda: self.config,
            get_api_key=lambda: self._api_key_override,
            delegate_deep_agent=lambda task: self._run_turn(task, kind="deepagent"),
            on_turn=self._record_live_turn,
            on_event=self._broadcast,
            on_tokens=self._add_tokens,
        )

    def _ensure_agent(self) -> Any:
        if self._agent is None or self._agent_dirty:
            self._stt_model = None
            self._agent = build_pet_agent(
                self.config,
                self.memory_store,
                self.bridge,
                model=self._model_override,
                api_key_override=self._api_key_override,
                get_config=lambda: self.config,
                is_autonomous_turn=lambda: self._in_autonomous_turn,
                transcribe_wav=self.transcribe_wav,
                checkpointer=self._checkpointer,
            )
            self._agent_dirty = False
        return self._agent

    async def transcribe_wav(self, wav_path: str) -> str:
        """Transcribe a WAV file using the configured multimodal LLM (e.g. Gemini) when local STT is unavailable."""
        path = Path(wav_path)
        try:
            raw = await asyncio.to_thread(path.read_bytes)
        except OSError:
            return ""
        if not raw:
            return ""
        _ensure_provider_env(api_key_override=self._api_key_override, model=self.config.model)
        if self._model_override is not None:
            llm = self._model_override
        else:
            if self._stt_model is None:
                self._stt_model = init_chat_model(self.config.model)
            llm = self._stt_model
        b64 = base64.b64encode(raw).decode("ascii")
        msg = HumanMessage(
            content=[
                {
                    "type": "text",
                    "text": (
                        "Transcribe verbatim what the human said in this short robot microphone clip. "
                        "Reply with ONLY the spoken words (any language). "
                        "If there is only background noise, fan/motor hum, or no clear human speech, reply with literally: SILENCE"
                    ),
                },
                {"type": "media", "mime_type": "audio/wav", "data": b64},
            ]
        )
        try:
            resp = await llm.ainvoke([msg])
            text = _extract_text(resp.content).strip()
        except Exception:
            logger.debug("Multimodal audio transcription failed", exc_info=True)
            return ""
        if not text or text.upper().strip(".! ") == "SILENCE":
            return ""
        return text

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
            self._stt_model = None
            self._agent_dirty = True
        if (
            self.config.model != prev.model
            or self.config.temperature != prev.temperature
            or self.config.personality != prev.personality
            or self.config.pet_name != prev.pet_name
            or self.config.max_drive_m != prev.max_drive_m
        ):
            self._stt_model = None
            self._agent_dirty = True
        if (
            (api_key is not None and api_key.strip())
            or self.config.autonomy_enabled != prev.autonomy_enabled
            or self.config.live_enabled != prev.live_enabled
            or self.config.live_model != prev.live_model
            or self.config.live_voice_name != prev.live_voice_name
            or self.config.live_voice_mode != prev.live_voice_mode
            or self.config.idle_token_saver != prev.idle_token_saver
            or self.config.pet_name != prev.pet_name
            or self.config.personality != prev.personality
            or self.config.wake_word_enabled != prev.wake_word_enabled
            or self.config.wake_words != prev.wake_words
            or self.config.listen_language != prev.listen_language
            or self.config.max_drive_m != prev.max_drive_m
            or self.config.temperature != prev.temperature
        ):
            self.live.notify_config_changed()
        self._broadcast("config", self.config.to_dict())
        return self.config

    def _add_tokens(self, count: int) -> None:
        if count > 0:
            self.total_tokens += count
            self._broadcast("tokens", {"total_tokens": self.total_tokens, "last_tokens": count})

    async def _record_live_turn(self, record_dict: dict[str, Any]) -> None:
        await asyncio.to_thread(self.memory_store.append_history, record_dict)
        self._broadcast("turn", record_dict)

    async def _run_turn(self, prompt: str, *, kind: str) -> TurnRecord:
        async with self._turn_lock:
            self._turn_epoch += 1
            self.busy = True
            self._in_autonomous_turn = kind == "autonomy"
            self._broadcast("status", {"busy": True, "kind": kind})
            try:
                thread_id = self.config.thread_id
                # Prevent unbounded RAM/context growth in long-running pet sessions: rotate the in-memory
                # LangGraph checkpointer when the thread exceeds _MAX_THREAD_MESSAGES (long-term memory
                # persists in ~/.nebi-ai/memories/MEMORY.md).
                if self._msg_count_by_thread.get(thread_id, 0) >= _MAX_THREAD_MESSAGES:
                    self._checkpointer = MemorySaver()
                    self._msg_count_by_thread[thread_id] = 0
                    self._agent_dirty = True
                agent = self._ensure_agent()
                prev_count = self._msg_count_by_thread.get(thread_id, 0)
                result = await agent.ainvoke(
                    {"messages": [{"role": "user", "content": prompt}]},
                    config={"configurable": {"thread_id": thread_id}},
                )
                messages = list(result.get("messages") or [])
                self._msg_count_by_thread[thread_id] = len(messages)
                reply, tool_calls, turn_tokens = _extract_turn_output(messages, prev_count)
                self._add_tokens(turn_tokens)
                record = TurnRecord(
                    kind=kind,
                    timestamp=time.time(),
                    prompt=prompt,
                    reply=reply,
                    tool_calls=tool_calls,
                    tokens=turn_tokens,
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
                self._turn_epoch += 1
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

    async def autonomy_tick(self, *, force: bool = True) -> TurnRecord | None:
        """Execute one autonomous pet perception & reaction tick.

        When `force=False` (periodic background runner), skips calling the LLM completely (0 tokens)
        if no people/pets are in view, no meaningful telemetry events occurred, and posture/charging
        state has not changed.
        """
        if not self.config.autonomy_enabled or self.config.is_quiet_now() or self.live.is_playing:
            return None
        state_env = await self.bridge.state()
        raw_events = await self.bridge.poll_events()
        self.last_tick_at = time.time()

        st = state_env.get("state") or state_env.get("result") or {}
        # Ignore benign repetitive 0x0000000C warning flaps so idle robot naps cost 0 tokens.
        events = [
            ev
            for ev in raw_events
            if not (ev.get("type") in ("fault_raised", "fault_cleared") and ev.get("code") == "0x0000000C")
        ]
        targets = st.get("targets")
        n_targets = len(targets) if isinstance(targets, list) else int(targets or 0)
        battery = st.get("battery")
        low_batt = isinstance(battery, (int, float)) and battery <= 20 and not st.get("charging")
        sig = (st.get("connected"), st.get("posture"), bool(st.get("charging")), low_batt)

        if not force and self.config.idle_token_saver and not events and n_targets == 0 and sig == self._last_autonomy_sig:
            return None
        self._last_autonomy_sig = sig

        state_summary = json.dumps(st, ensure_ascii=False)
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
        """Immediately halt robot motion, flush Live speaker audio, and pause autonomous ticks."""
        self.live.flush_speaker()
        self.update_config({"autonomy_enabled": False})
        res = await self.bridge.call("stop", {})
        self._broadcast("emergency_stop", {"result": res})
        return res

    async def reset_session(self, *, clear_history: bool = True) -> dict[str, Any]:
        """Clear chat history and restart both the DeepAgents thread and Gemini Live session.

        Long-term memory files in `~/.nebi-ai/memories/` (`MEMORY.md`, etc.) are preserved and
        reloaded into the fresh session.
        """
        self.live.flush_speaker()
        async with self._turn_lock:
            self._checkpointer = MemorySaver()
            self._msg_count_by_thread.clear()
            self._followup_until = 0.0
            self._last_autonomy_sig = None
            self._agent_dirty = True
            if clear_history:
                await asyncio.to_thread(self.memory_store.clear_history)
        self.live.notify_config_changed()
        self._broadcast("session_reset", {"clear_history": clear_history})
        return {"ok": True, "clear_history": clear_history}

    def start_autonomy(self) -> None:
        """Start Gemini Live, the background autonomous pet loop, and fallback microphone listener."""
        self._stop_event.clear()
        self.live.start()
        if self._autonomy_task is None or self._autonomy_task.done():
            self._autonomy_task = asyncio.create_task(self._autonomy_runner(), name="nebi-ai-autonomy")
        if self._listen_task is None or self._listen_task.done():
            self._listen_task = asyncio.create_task(self._listen_runner(), name="nebi-ai-listener")

    async def stop_autonomy(self) -> None:
        """Stop Gemini Live, the background autonomous pet loop, and fallback microphone listener."""
        self._stop_event.set()
        await self.live.stop()
        for task in (self._autonomy_task, self._listen_task):
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._autonomy_task = None
        self._listen_task = None

    async def listen_once(self, seconds: float | None = None, *, keep_intercom: bool = False) -> tuple[dict[str, Any], str]:
        """Record one mic window (serialized via `_listen_lock`), transcribe if needed, and clean up temp WAV."""
        win = self.config.listen_window_sec if seconds is None else seconds
        pet = self.config.pet_name
        listen_args: dict[str, Any] = {
            "seconds": win,
            "no_save": True,
            "keep_intercom": keep_intercom,
            "prompt": f"{pet}, {pet} {pet}, Hey {pet}. Stand up, sit down, look around, come here.",
        }
        if self.config.listen_language.strip():
            listen_args["language"] = self.config.listen_language.strip()
        async with self._listen_lock:
            speech_epoch_before = self.bridge.speech_epoch
            res = await self.bridge.call("listen", listen_args)
            if not res.get("ok"):
                return res, ""
            result = res.get("result") or {}
            wav_path = result.get("wav")
            try:
                # Only discard if the robot actually spoke/emoted during this recording window (prevents self-echo
                # without dropping human speech during silent look/memory ticks).
                if self.bridge.speech_epoch != speech_epoch_before or self.bridge.speaking:
                    return res, ""
                text = str(result.get("text") or "").strip()
                if not text and result.get("speech") is not False and isinstance(wav_path, str):
                    text = await self.transcribe_wav(wav_path)
                return res, text
            finally:
                if isinstance(wav_path, str) and Path(wav_path).name.startswith("listen-"):
                    try:
                        await asyncio.to_thread(Path(wav_path).unlink, True)
                    except OSError:
                        pass

    async def _listen_runner(self) -> None:
        """Continuously listen to the robot microphone and wake the pet agent when speech is heard."""
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=1.5)
            return
        except asyncio.TimeoutError:
            pass

        while not self._stop_event.is_set():
            live_active = self.config.live_enabled and bool(resolve_gemini_api_key(self._api_key_override))
            if (
                not self.config.autonomy_enabled
                or live_active
                or not self.config.listen_enabled
                or self.config.is_quiet_now()
                or self.bridge.speaking
            ):
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=0.4)
                except asyncio.TimeoutError:
                    continue
                return

            try:
                res, text = await self.listen_once(keep_intercom=True)
            except Exception:
                logger.debug("Background listen call failed; backing off", exc_info=True)
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=3.0)
                except asyncio.TimeoutError:
                    continue
                return

            if not res.get("ok"):
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=3.0)
                except asyncio.TimeoutError:
                    continue
                return

            if text:
                in_followup = time.monotonic() < self._followup_until
                if self.config.wake_word_enabled and not in_followup:
                    matched, _ = match_wake_word(text, self.config.wake_words)
                    if not matched:
                        self._broadcast("heard_ignored", {"text": text})
                        continue
                self._broadcast("heard", {"text": text, "followup": in_followup})
                try:
                    await self.chat(f"[Heard via microphone]: {text}")
                    # Keep a conversational follow-up window open after Beni replies so the human
                    # does not need to repeat the wake word on their immediate next sentence.
                    self._followup_until = time.monotonic() + self.config.followup_window_sec
                except Exception:
                    logger.exception("Failed handling heard speech")
                # Brief cooldown after speaking so we don't immediately pick up tail echo.
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=0.8)
                except asyncio.TimeoutError:
                    continue
                return

            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=0.05)
            except asyncio.TimeoutError:
                continue

    async def _autonomy_runner(self) -> None:
        # Short initial delay so the Web UI / robot connection settles first.
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=3.0)
            return
        except asyncio.TimeoutError:
            pass

        while not self._stop_event.is_set():
            # When Gemini Live is active, Live already handles real-time perception & speech directly
            # (delegating to DeepAgents via `ask_deep_agent`), so don't spam parallel 25s autonomy ticks
            # that fight over the robot speaker/camera. Manual "Wake Tick" still works on demand.
            live_owns_loop = self.config.live_enabled and bool(resolve_gemini_api_key(self._api_key_override))
            if (
                self.config.autonomy_enabled
                and not live_owns_loop
                and not self.config.is_quiet_now()
                and not self.busy
            ):
                try:
                    await self.autonomy_tick(force=False)
                except Exception:
                    logger.exception("Unhandled error in autonomy tick")
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self.config.autonomy_interval_sec)
            except asyncio.TimeoutError:
                continue
