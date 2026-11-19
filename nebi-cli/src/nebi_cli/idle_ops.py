"""Idle "pet life": the IdleLoop background behaviour and the `idle` command.

Reacts to the core robot event log (a person/pet coming into view, battery dropping, new faults) and to the event
streams of extensions (touches, gestures, bumps, ...: AgentExtensionContext.add_event_source). Never drives. It stays completely still while any agent command runs, while the motion lock is held, while
`say` / `listen` are busy, and while the robot is carried. Body moves additionally need the robot in manual camera / idle mode with no follow, tracking, docking or game,
and a known not-carried state. `nebi stop` cancels the loop.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from nebi_cli.errors import STANDING, bad_arg
from nebi_cli.pet_ops import look_at
from nebi_cli.registry import Args, CommandGroup, command

if TYPE_CHECKING:
    from nebi.client import AsyncNebiClient
    from nebi_cli.runtime import AgentRuntime

logger = logging.getLogger(__name__)

DEFAULT_QUIET_HOURS = "22-8"
LOG_KEEP = 50
EVENT_POLL_S = 5.0  # long-poll per event stream per loop iteration
SOURCE_RETRY_S = 5.0  # back-off after an extension event source failed
REACT_COOLDOWN_S = 10.0  # the same kind of event (e.g. from two streams) is reacted to at most this often
STOP_WAIT_S = 3.0  # how long `stop()` waits for the loop to unwind (it re-centres the gimbal on the way out)


def hours_window(spec: str | None) -> tuple[int, int] | None:
    """'22-8' -> (22, 8); None/'' -> None."""
    if not spec:
        return None
    m = re.fullmatch(r"\s*(\d{1,2})\s*-\s*(\d{1,2})\s*", spec)
    if not m:
        raise bad_arg(f"quiet hours must look like 22-8, got {spec!r}")
    return int(m.group(1)) % 24, int(m.group(2)) % 24


def in_window(w: tuple[int, int] | None, hour: int | None = None) -> bool:
    """Whether `hour` (default: now, local time) is inside the window; windows may wrap midnight."""
    if w is None:
        return False
    h = time.localtime().tm_hour if hour is None else hour
    a, b = w
    return a <= h < b if a <= b else (h >= a or h < b)


def autonomy_reason(c: AsyncNebiClient) -> str | None:
    """Why the robot is not under plain manual control (None = manual camera / idle, nothing autonomous)."""
    t = c.telemetry
    if t.docked:
        return "docked"
    if t.tracking_active or t.locked_target_id:
        return "tracking/following a target"
    if t.active_game:
        return "playing a game"
    if t.mission_name not in ("CAMERA", "IDLE"):
        return f"mission {t.mission_name}/{t.sub_mode_name}"
    if t.mission_name == "CAMERA" and t.sub_mode_name != "MANUAL":
        return f"camera mode {t.sub_mode_name}"
    return None


def robot_event_for_idle(e: dict[str, Any]) -> dict[str, Any] | None:
    """Core event-log entry -> the event shape idle reacts to (kind/summary), or None for events it ignores."""
    t = e.get("type")
    if t == "target_appeared":
        return {"kind": "person_appeared", "summary": f"{e.get('label', 'someone')} in view ({e.get('where', '?')})",
                "source": "robot", "target_id": e.get("id")}
    if t == "battery_low":
        return {"kind": "battery_low", "summary": f"battery low ({e.get('battery')}%)", "source": "robot"}
    if t == "fault_raised":
        return {"kind": "fault", "summary": f"fault {e.get('code')}", "source": "robot"}
    return None


@dataclass(frozen=True)
class IdleConfig:
    quiet: tuple[int, int] | None = (22, 8)  # quiet-hours window (no voice, sleepy ambient only)
    level: str = "calm"  # calm | playful (ambient behaviour twice as often)
    speak: bool = True  # babble (False with --silent)


class IdleLoop:
    """The background idle task: reacts to robot/extension events and does occasional ambient behaviour."""

    def __init__(self, rt: AgentRuntime) -> None:
        self._rt = rt
        self._task: asyncio.Task[None] | None = None
        self.config = IdleConfig()
        self.log: list[dict[str, Any]] = []
        self._reacted_at: dict[str, float] = {}

    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self, cfg: IdleConfig) -> None:
        if self.running():
            await self.stop()
        self.config = cfg
        self.log = []
        self._task = asyncio.create_task(self._loop(cfg), name="nebi-agent-idle")

    async def stop(self) -> bool:
        """Cancel the idle loop and wait (briefly) for it to unwind. Returns whether it was running."""
        task = self._task
        if task is None or task.done():
            return False
        task.cancel()
        await asyncio.wait({task}, timeout=STOP_WAIT_S)
        return True

    def blocked(self) -> str | None:
        """Why idle must stay still right now (None = free to act)."""
        rt = self._rt
        if rt.motion.lock.locked():
            return "a motion is running"
        if rt.activity.active:
            return "an agent command is running"
        if rt.activity.busy or rt.speech.speaking():
            return "busy speaking/listening"
        return None

    def _log(self, event: Any, did: str) -> None:
        self.log.append({"t": time.strftime("%H:%M:%S"), "event": event, "did": did})
        self.log = self.log[-LOG_KEEP:]

    async def _poll(self, name: str, since: int) -> tuple[list[dict[str, Any]], int]:
        """One long-poll of one stream ("robot" = the core event log). Never raises (except cancellation)."""
        rt = self._rt
        if name == "robot":
            await rt.events.wait_for_new(since, EVENT_POLL_S)
            raw = rt.events.since(since, 100)
            evs = [ev for ev in (robot_event_for_idle(e) for e in raw) if ev is not None]
            return evs, raw[-1]["seq"] if raw else since
        src = rt.hooks.event_sources[name]
        t0 = time.monotonic()
        try:
            evs, last = await src.poll(since, wait=EVENT_POLL_S)
        except Exception:  # source down: back off and keep living (the other streams and ambient still run)
            logger.debug("idle: %s events unavailable", name, exc_info=True)
            await asyncio.sleep(SOURCE_RETRY_S)
            return [], since
        if not evs and time.monotonic() - t0 < 0.5:
            await asyncio.sleep(1.0)  # the source did not long-poll: don't spin
        return [e for e in evs if isinstance(e, dict)], last

    async def _loop(self, cfg: IdleConfig) -> None:
        rt = self._rt
        cursors: dict[str, int] = {"robot": rt.events.last_seq}
        for name, src in rt.hooks.event_sources.items():
            try:
                cursors[name] = await src.cursor()
            except Exception:  # source down: start from 0, the poll retries
                logger.debug("idle: %s cursor unavailable", name, exc_info=True)
                cursors[name] = 0
        polls: dict[str, asyncio.Task[tuple[list[dict[str, Any]], int]]] = {}
        next_ambient = time.monotonic() + random.uniform(20, 45)
        try:
            while True:
                try:
                    for name, cur in cursors.items():
                        if name not in polls:
                            polls[name] = asyncio.create_task(self._poll(name, cur), name=f"nebi-agent-idle-{name}")
                    done, _ = await asyncio.wait(polls.values(), timeout=EVENT_POLL_S + 1.0,
                                                 return_when=asyncio.FIRST_COMPLETED)
                    evs: list[dict[str, Any]] = []
                    for name, task in list(polls.items()):
                        if task in done:
                            del polls[name]
                            batch, cursors[name] = task.result()
                            evs += batch
                    c = rt.manager.client
                    if c is None or not c.connected:
                        continue
                    if self.blocked():
                        continue
                    carried, _ = await rt.safety.carried_state()
                    if carried:
                        continue
                    body_ok = carried is False and autonomy_reason(c) is None
                    quiet = in_window(cfg.quiet)
                    for e in evs:
                        if self.blocked():
                            break
                        kind = str(e.get("kind"))
                        if time.monotonic() - self._reacted_at.get(kind, -1e9) < REACT_COOLDOWN_S:
                            continue
                        did = await self.react(c, e, quiet, cfg, body_ok)
                        if did:
                            self._reacted_at[kind] = time.monotonic()
                            self._log(e.get("summary"), did)
                            break
                    if time.monotonic() >= next_ambient and not self.blocked():
                        next_ambient = time.monotonic() + random.uniform(25, 70) * (2.0 if cfg.level == "calm" else 1.0)
                        did = await self._ambient(c, quiet, cfg, body_ok)
                        if did:
                            self._log("ambient", did)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # the loop must survive anything
                    logger.debug("idle loop error", exc_info=True)
                    self._log("error", f"{type(exc).__name__}: {exc}")
                    await asyncio.sleep(3.0)
        finally:
            for task in polls.values():
                task.cancel()

    async def _body(self, gesture: str, w: list[str], silent: bool = True) -> None:
        await self._rt.invoke("body", {"_warnings": w, "gesture": gesture, "silent": silent}, source="idle")

    async def react(self, c: AsyncNebiClient, e: dict[str, Any], quiet: bool, cfg: IdleConfig,
                    body_ok: bool) -> str | None:
        """React to one event (robot or extension stream); returns what was done (for the idle log) or None."""
        kind = e.get("kind")
        standing = c.telemetry.posture_name in STANDING
        speak = cfg.speak and not quiet
        w: list[str] = []

        async def express(eyes: str | None, voice: str | None) -> None:
            await self._rt.expression.express(c, eyes=eyes, voice=voice if speak else None, warnings=w)

        if kind == "touched":
            detail = e.get("detail")
            if detail == "head_pat":
                await express("heart", "love")
                return "loved the head pat (eyes, coo)"
            if detail in ("pushed", "shaken", "startled"):
                await express("dizzy", "uh-oh")
                return f"reacted to being {detail}"
            await express("happy", "happy")
            return "happy about the touch"
        if kind == "gesture":
            g = e.get("gesture")
            if g in ("palm", "palm_near"):
                await express("wink", "happy")
                if standing and not quiet and body_ok:
                    await self._body("nod", w)
                    return "waved back (wink + nod)"
                return "waved back (wink)"
            if g == "thumbs_up":
                await express("happy", "excited")
                if standing and not quiet and body_ok:
                    await self._body("wiggle", w)
                return "celebrated the thumbs up"
            return None
        if kind == "person_appeared" and not quiet:
            await express("happy", "curious")
            if standing and body_ok:
                try:
                    await look_at(self._rt, c, "person", w, source="idle")
                except Exception:  # looking at the person is a nicety; the greeting already happened
                    logger.debug("idle look-at failed", exc_info=True)
            return f"noticed {e.get('summary')}"
        if kind == "bumped":
            await express("dizzy", "uh-oh")
            return "said ouch to the bump"
        if kind == "battery_low":
            await express("sleepy", "sleepy")
            return "felt tired (battery low)"
        if kind == "fault":
            await express("dizzy", "uh-oh")
            return f"worried about {e.get('summary')}"
        return None

    async def _ambient(self, c: AsyncNebiClient, quiet: bool, cfg: IdleConfig, body_ok: bool) -> str | None:
        rt = self._rt
        w: list[str] = []
        if quiet:
            if random.random() < 0.3:
                await rt.expression.express(c, eyes="sleepy", voice=None, warnings=w)
                return "dozed (sleepy eyes, quiet hours)"
            return None
        standing = c.telemetry.posture_name in STANDING
        choices = ["babble", "blink"]
        if body_ok:
            choices.append("look_around")
            if standing:
                choices += ["curious", "wiggle"]
        pick = random.choice(choices)
        if pick == "look_around":
            try:
                await rt.invoke("tilt", {"_warnings": w, "direction": random.choice(["up", "down"]), "for": 0.4}, source="idle")
                await asyncio.sleep(1.5)
            finally:  # never leave the gimbal tilted, even if cancelled (nebi stop / idle stop)
                await rt.invoke("tilt", {"_warnings": w, "direction": "center"}, source="idle")
            return "looked around"
        if pick == "blink":
            await rt.expression.express(c, eyes=random.choice(["happy", "wink", "cool"]), voice=None, warnings=w)
            return "blinked"
        if pick == "babble":
            if not cfg.speak:
                return None
            await rt.expression.express(c, eyes=None, voice=random.choice(["happy", "curious", "neutral"]), warnings=w)
            return "babbled to itself"
        await self._body(pick, w, silent=not cfg.speak)
        return f"did a little {pick}"


class IdleCommands(CommandGroup):
    @command("idle")
    async def idle(self, a: Args) -> dict[str, Any]:
        loop = self.rt.idle
        action = a.get("action") or "status"
        running = loop.running()
        if action == "start":
            quiet = a.get("quiet_hours") or DEFAULT_QUIET_HOURS
            cfg = IdleConfig(quiet=hours_window(quiet), level=a.get("level") or "calm", speak=not a.get("silent"))
            await loop.start(cfg)
            return {"idle": "running", "config": {"quiet": quiet, "level": cfg.level, "speak": cfg.speak},
                    "summary": f"idle life started ({cfg.level}; quiet {quiet}): reacts to people, battery and faults"
                    + (" (plus " + ", ".join(self.rt.hooks.event_sources) + " events)" if self.rt.hooks.event_sources else "")
                    + " with eyes, voice and in-place body language; never drives; pauses while you command the robot",
                    "stop_with": "nebi idle stop (or nebi stop)"}
        if action == "stop":
            await loop.stop()
            return {"idle": "stopped", "was_running": running, "summary": "idle life stopped", "recent": loop.log[-10:]}
        log = loop.log
        return {"idle": "running" if running else "stopped", "recent": log[-15:],
                "summary": ("idle life running" if running else "idle life off") + (f"; last: {log[-1]['did']}" if log else "")}
