"""Persistent configuration, home-directory memory storage, and chat history for `nebi-ai`."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from pathlib import Path
from typing import Any

DEFAULT_MODEL = "google_genai:gemini-2.5-flash"

MODEL_PRESETS: tuple[dict[str, str], ...] = (
    {"id": "google_genai:gemini-2.5-flash", "label": "Gemini 2.5 Flash (fast, recommended)"},
    {"id": "google_genai:gemini-2.5-pro", "label": "Gemini 2.5 Pro (deep reasoning)"},
    {"id": "openai:gpt-4o", "label": "OpenAI GPT-4o (requires nebi-ai[openai])"},
    {"id": "anthropic:claude-sonnet-4-20250514", "label": "Claude Sonnet 4 (requires nebi-ai[anthropic])"},
    {"id": "ollama:llama3.2", "label": "Ollama Llama 3.2 local (requires nebi-ai[ollama])"},
)

DEFAULT_PERSONALITY = (
    "Curious, affectionate, playful little desk-and-floor companion robot. "
    "Speaks in short pet-like phrases (1-6 words) or cute chirps/babble with matching eye expressions and body language. "
    "Loves greeting familiar people, remembering small details about friends and rooms, and taking naps when battery is low."
)

DEFAULT_MEMORY_TEMPLATE = """\
# Nebi Pet Memory

This directory is your persistent long-term memory across sessions.
Update this file (or create focused files like `/people.md`, `/rooms.md`, `/journal.md`) whenever you learn
something worth remembering about your humans, your home, or your routines.

## Identity
- Name: {pet_name}
- Role: Autonomous companion pet robot

## People I Know
- (Add names, preferences, and fun moments here.)

## Home & Favorite Spots
- (Add rooms, obstacles to watch out for, and dock location notes here.)

## Recent Notes
- Just woke up for the first time!
"""


def default_home_dir() -> Path:
    """Return the `nebi-ai` home directory (`$NEBI_AI_HOME` or `~/.nebi-ai`)."""
    env = os.environ.get("NEBI_AI_HOME", "").strip()
    return Path(env).expanduser() if env else Path.home() / ".nebi-ai"


@dataclass
class PetConfig:
    """User-configurable parameters for the pet agent and autonomous loop."""

    pet_name: str = "Nebi"
    model: str = DEFAULT_MODEL
    temperature: float = 0.6
    personality: str = DEFAULT_PERSONALITY
    autonomy_enabled: bool = True
    autonomy_interval_sec: float = 25.0
    autonomy_allow_drive: bool = False
    listen_enabled: bool = True
    listen_window_sec: float = 5.0
    quiet_hours: str = ""  # e.g. "22-8"
    max_drive_m: float = 1.0
    voice_enabled: bool = True
    thread_id: str = "pet-main"

    def is_quiet_now(self, now: datetime | None = None) -> bool:
        """Return True if the current local hour falls within `quiet_hours` (e.g. `'22-8'`)."""
        spec = self.quiet_hours.strip()
        if not spec or "-" not in spec:
            return False
        parts = spec.split("-", 1)
        try:
            start_h = int(parts[0].strip()) % 24
            end_h = int(parts[1].strip()) % 24
        except ValueError:
            return False
        hour = (now or datetime.now()).hour
        if start_h == end_h:
            return False
        if start_h < end_h:
            return start_h <= hour < end_h
        return hour >= start_h or hour < end_h

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> PetConfig:
        valid = {f.name for f in fields(cls)}
        filtered = {k: v for k, v in raw.items() if k in valid}
        cfg = cls(**filtered)
        cfg.temperature = max(0.0, min(2.0, float(cfg.temperature)))
        cfg.autonomy_interval_sec = max(3.0, min(600.0, float(cfg.autonomy_interval_sec)))
        cfg.listen_window_sec = max(2.0, min(15.0, float(cfg.listen_window_sec)))
        cfg.max_drive_m = max(0.1, min(3.0, float(cfg.max_drive_m)))
        cfg.pet_name = str(cfg.pet_name).strip() or "Nebi"
        cfg.model = str(cfg.model).strip() or DEFAULT_MODEL
        return cfg

    def with_updates(self, updates: dict[str, Any]) -> PetConfig:
        merged = self.to_dict()
        merged.update(updates)
        return self.from_dict(merged)


class MemoryStore:
    """Manages `~/.nebi-ai/` layout: `config.json`, `history.jsonl`, and `memories/`."""

    def __init__(self, home_dir: Path | None = None) -> None:
        self.home_dir = (home_dir or default_home_dir()).expanduser().resolve()
        self.memories_dir = self.home_dir / "memories"
        self.config_path = self.home_dir / "config.json"
        self.history_path = self.home_dir / "history.jsonl"
        self.home_dir.mkdir(parents=True, exist_ok=True)
        self.memories_dir.mkdir(parents=True, exist_ok=True)

    def load_config(self) -> PetConfig:
        if not self.config_path.is_file():
            cfg = PetConfig()
            self.save_config(cfg)
            return cfg
        try:
            raw = json.loads(self.config_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return PetConfig.from_dict(raw)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            pass
        return PetConfig()

    def save_config(self, cfg: PetConfig) -> None:
        self.home_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.config_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cfg.to_dict(), indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.config_path)

    def ensure_default_memory(self, pet_name: str = "Nebi") -> Path:
        """Create `memories/MEMORY.md` on first run if missing."""
        mem_file = self.memories_dir / "MEMORY.md"
        if not mem_file.is_file():
            mem_file.write_text(DEFAULT_MEMORY_TEMPLATE.format(pet_name=pet_name), encoding="utf-8")
        return mem_file

    def _safe_memory_path(self, rel_path: str) -> Path:
        cleaned = rel_path.strip().lstrip("/")
        if not cleaned:
            raise ValueError("Memory path must not be empty")
        candidate = (self.memories_dir / cleaned).resolve()
        try:
            candidate.relative_to(self.memories_dir)
        except ValueError as exc:
            raise ValueError(f"Memory path escapes memories directory: {rel_path!r}") from exc
        return candidate

    def list_memories(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        if not self.memories_dir.is_dir():
            return items
        for path in sorted(self.memories_dir.rglob("*")):
            if not path.is_file() or path.name.startswith("."):
                continue
            rel = path.relative_to(self.memories_dir).as_posix()
            st = path.stat()
            items.append({"path": rel, "size": st.st_size, "mtime": st.st_mtime})
        return items

    def read_memory(self, rel_path: str) -> str:
        target = self._safe_memory_path(rel_path)
        if not target.is_file():
            raise FileNotFoundError(f"Memory file not found: {rel_path}")
        return target.read_text(encoding="utf-8")

    def write_memory(self, rel_path: str, content: str) -> Path:
        target = self._safe_memory_path(rel_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target

    def delete_memory(self, rel_path: str) -> bool:
        target = self._safe_memory_path(rel_path)
        if target.is_file():
            target.unlink()
            return True
        return False

    def reset_memories(self, pet_name: str = "Nebi") -> None:
        for path in list(self.memories_dir.rglob("*")):
            if path.is_file():
                path.unlink()
        self.ensure_default_memory(pet_name=pet_name)

    def append_history(self, record: dict[str, Any]) -> None:
        self.home_dir.mkdir(parents=True, exist_ok=True)
        with self.history_path.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(record, ensure_ascii=False) + "\n")

    def load_history(self, limit: int = 80) -> list[dict[str, Any]]:
        if not self.history_path.is_file():
            return []
        records: list[dict[str, Any]] = []
        for raw_line in self.history_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
                if isinstance(parsed, dict):
                    records.append(parsed)
            except json.JSONDecodeError:
                continue
        return records[-limit:]
