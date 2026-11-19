"""Activity (moving commands) and busy (say/listen) bookkeeping, for the cockpit pill and the idle loop."""

from __future__ import annotations

import time
from typing import Any


class ActivityTracker:
    def __init__(self) -> None:
        self._seq = 0
        self._active: dict[int, tuple[str, float]] = {}
        self._busy: dict[int, str] = {}
        self._last: dict[str, Any] | None = None

    @property
    def active(self) -> bool:
        """A robot-moving command is running."""
        return bool(self._active)

    @property
    def busy(self) -> bool:
        """`say` / `listen` is running."""
        return bool(self._busy)

    def begin(self, command: str) -> int:
        self._seq += 1
        self._active[self._seq] = (command, time.time())
        return self._seq

    def end(self, tok: int) -> None:
        entry = self._active.pop(tok, None)
        if entry is not None:
            self._last = {"command": entry[0], "ended": time.time()}

    def begin_busy(self, what: str) -> int:
        self._seq += 1
        self._busy[self._seq] = what
        return self._seq

    def end_busy(self, tok: int) -> None:
        self._busy.pop(tok, None)

    def snapshot(self) -> dict[str, Any]:
        out: dict[str, Any] = {"active": bool(self._active), "command": None, "since": None}
        if self._active:
            _, (cmd, since) = max(self._active.items())
            out.update(command=cmd, since=since, commands=[c for c, _ in self._active.values()])
        out["last"] = self._last
        out["busy"] = sorted(set(self._busy.values()))
        return out
