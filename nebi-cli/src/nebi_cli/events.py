"""Robot event log (`nebi events`, long-poll) and the telemetry watcher that turns state diffs into events."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import TYPE_CHECKING, Any

from nebi_cli.views import target_dict

if TYPE_CHECKING:
    from nebi_cockpit.app import NebiServerManager

logger = logging.getLogger(__name__)

WATCH_PERIOD_S = 0.2
TARGET_LOST_AFTER_S = 1.5  # a target must be gone this long before `target_lost` (detections flicker)
BATTERY_BANDS = (5, 10, 20, 30, 50)  # `battery_low` fires when the charge drops into a lower band


class EventLog:
    """Bounded, cursor-addressable event log with long-poll support."""

    def __init__(self, maxlen: int = 1000) -> None:
        self._events: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self._seq = 0
        self._new = asyncio.Event()  # set (and replaced) on every emit; waiters hold the old one

    @property
    def last_seq(self) -> int:
        return self._seq

    def emit(self, type_: str, **data: Any) -> None:
        self._seq += 1
        self._events.append({"seq": self._seq, "ts": round(time.time(), 3), "type": type_, **data})
        self._new.set()
        self._new = asyncio.Event()

    def since(self, seq: int, limit: int = 50) -> list[dict[str, Any]]:
        out = [e for e in self._events if e["seq"] > seq]
        return out[:limit]

    async def wait_for_new(self, seq: int, timeout: float) -> None:
        """Return as soon as an event newer than `seq` exists, or after `timeout`."""
        if self._seq > seq or timeout <= 0:
            return
        try:
            await asyncio.wait_for(self._new.wait(), timeout)
        except asyncio.TimeoutError:
            pass


class TelemetryWatcher:
    """Background task: diffs telemetry every 0.2 s and emits connection/posture/mode/fault/target events."""

    def __init__(self, manager: NebiServerManager, events: EventLog) -> None:
        self._manager = manager
        self._events = events
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="nebi-agent-watcher")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _run(self) -> None:
        prev: dict[str, Any] = {}
        target_seen: dict[int, float] = {}
        target_present: set[int] = set()
        emit = self._events.emit
        while True:
            try:
                await asyncio.sleep(WATCH_PERIOD_S)
                c = self._manager.client
                connected = bool(c and c.connected)
                cur: dict[str, Any] = {"connected": connected}
                if c is not None:
                    t = c.telemetry
                    cur.update(
                        stage=t.link.stage,
                        degraded=t.link.degraded,
                        posture=t.posture_name,
                        mission=f"{t.mission_name}/{t.sub_mode_name}",
                        charging=t.charging,
                        docked=t.docked,
                        locomotion_locked=t.is_locked,
                        recording=t.recording,
                        locked_target=t.locked_target_id,
                        game=t.game_state,
                        battery_band=next((b for b in BATTERY_BANDS if t.battery_soc <= b), 100),
                        faults=tuple(sorted(a.code for a in t.hms_alerts)),
                    )
                    now = time.monotonic()
                    ids = {tg.target_id: tg for tg in t.targets}
                    for tid, tg in ids.items():
                        target_seen[tid] = now
                        if tid not in target_present:
                            target_present.add(tid)
                            emit("target_appeared", **target_dict(tg))
                    for tid in list(target_present):
                        if tid not in ids and now - target_seen.get(tid, 0) > TARGET_LOST_AFTER_S:
                            target_present.discard(tid)
                            emit("target_lost", id=tid)
                if prev:
                    for k, v in cur.items():
                        if k not in prev or prev[k] == v:
                            continue
                        if k == "faults":
                            for code in set(v) - set(prev[k]):
                                alert = next((a for a in c.telemetry.hms_alerts if a.code == code), None) if c else None
                                emit(
                                    "fault_raised",
                                    code=code,
                                    severity=getattr(alert, "severity", ""),
                                    message=getattr(alert, "message_en", ""),
                                )
                            for code in set(prev[k]) - set(v):
                                emit("fault_cleared", code=code)
                        elif k == "battery_band":
                            if v < prev[k] and c is not None:
                                emit("battery_low", battery=c.telemetry.battery_soc)
                        else:
                            emit(f"{k}_changed", old=prev[k], new=v)
                prev = cur
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - watcher must never die
                logger.exception("watch loop error")
