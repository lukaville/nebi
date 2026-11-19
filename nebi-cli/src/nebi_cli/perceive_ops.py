"""Perception commands: state, targets, look (camera + extension views), wait, events, info, faults."""

from __future__ import annotations

import asyncio
import io
import logging
import time
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nebi_cli import spec as S
from nebi_cli.conditions import eval_clause, parse_condition
from nebi_cli.errors import CACHE_DIR, AgentError, bad_arg
from nebi_cli.registry import Args, CommandGroup, command
from nebi_cli.util import wait_until
from nebi_cli.views import compact_state, target_dict

if TYPE_CHECKING:
    from nebi.client import AsyncNebiClient
    from nebi_cockpit.app import NebiServerManager

logger = logging.getLogger(__name__)

LOOK_DEFAULT_MAX_WIDTH = 1024
GRID_COLOR = (255, 255, 255)
GRID_LABEL_COLOR = (255, 255, 0)
LOCKED_BOX_COLOR = (255, 60, 60)
TARGET_BOX_COLOR = (60, 255, 120)


async def fresh_frame(manager: NebiServerManager, c: AsyncNebiClient, timeout: float = 3.0) -> tuple[bytes, bool]:
    """Ask for a keyframe and wait for a frame newer than now. Returns (jpeg, fresh)."""
    hub = manager.video_hub
    t0 = time.time()
    try:
        c.camera.request_keyframe()
    except Exception:  # best effort: the next regular keyframe works too, just later
        logger.debug("request_keyframe failed", exc_info=True)
    fresh = await wait_until(lambda: hub.frames_decoded > 0 and hub.last_frame_ts >= t0, timeout)
    if hub.frames_decoded == 0 or not hub.latest_jpeg:
        raise AgentError(
            "NO_VIDEO",
            "No video frames received from the robot yet.",
            hint="Video may be paused. Wait a second and retry, or `nebi up --reconnect`.",
        )
    return hub.latest_jpeg, fresh


# `wait --for <core event>` -> (event-log type, required field values)
CORE_WAIT_MATCH: dict[str, tuple[str, dict[str, Any]]] = {
    "person": ("target_appeared", {}),
    "person_left": ("target_lost", {}),
    "battery_low": ("battery_low", {}),
    "charging": ("charging_changed", {"new": True}),
    "docked": ("docked_changed", {"new": True}),
    "posture": ("posture_changed", {}),
    "fault": ("fault_raised", {}),
}


def core_event_matches(name: str, e: dict[str, Any]) -> bool:
    type_, fields = CORE_WAIT_MATCH[name]
    return e.get("type") == type_ and all(e.get(k) == v for k, v in fields.items())


def event_summary(e: dict[str, Any]) -> str:
    """One line for a robot event-log entry."""
    t = e.get("type", "")
    if t == "target_appeared":
        return f"{e.get('label', 'target')} #{e.get('id')} appeared ({e.get('where', '?')}, {e.get('distance_m', '?')} m)"
    if t == "target_lost":
        return f"target #{e.get('id')} lost"
    if t == "battery_low":
        return f"battery low ({e.get('battery')}%)"
    if t == "fault_raised":
        return f"fault {e.get('code')}: {e.get('message') or ''}".strip()
    if t.endswith("_changed"):
        return f"{t[:-8]}: {e.get('old')} -> {e.get('new')}"
    return t


def _write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


class PerceiveCommands(CommandGroup):
    @command("state")
    async def state(self, a: Args) -> dict[str, Any]:
        c = self.client()
        if a.get("full"):
            return c.telemetry.to_dict()
        t = c.telemetry
        return {
            "targets": [target_dict(tg) for tg in t.targets],
            "faults": [{"code": h.code, "severity": h.severity, "message": h.message_en} for h in t.hms_alerts],
            "odometry_m": t.session_odom_m,
        }

    @command("targets")
    async def targets(self, a: Args) -> dict[str, Any]:
        c = self.client()
        tg = [target_dict(x) for x in c.telemetry.targets]
        return {
            "count": len(tg),
            "targets": tg,
            "note": "x,y = box center in image (0..1, 0,0 top-left). Lock with `nebi track <id>`, follow with `nebi follow`.",
        }

    # ------------------------------------------------------------------ look

    @command("look")
    async def look(self, a: Args) -> dict[str, Any]:
        view = (a.get("view") or "camera").lower()
        if view != "camera":
            return await self._look_view(view, a)

        from PIL import Image, ImageDraw  # optional heavy dep, only for `look`

        c = self.client()
        hub = self.manager.video_hub
        warnings: list[str] = a["_warnings"]
        jpeg, ok = await fresh_frame(self.manager, c)
        if not ok:
            warnings.append(f"Frame is {time.time() - hub.last_frame_ts:.1f}s old (live video stalled?).")
        try:
            img = Image.open(io.BytesIO(jpeg)).convert("RGB")
        except Exception as exc:
            raise AgentError("BAD_FRAME", f"The latest camera frame could not be decoded ({exc}).",
                             hint="Retry in a second.") from exc
        max_w = int(a.get("max_width") or LOOK_DEFAULT_MAX_WIDTH)
        if img.width > max_w:
            img = img.resize((max_w, round(img.height * max_w / img.width)))
        targets = [target_dict(x) for x in c.telemetry.targets]
        if a.get("annotate"):
            d = ImageDraw.Draw(img)
            W, H = img.size
            for i in (1, 2):
                d.line([(W * i / 3, 0), (W * i / 3, H)], fill=GRID_COLOR, width=1)
                d.line([(0, H * i / 3), (W, H * i / 3)], fill=GRID_COLOR, width=1)
            for gx in range(3):
                for gy in range(3):
                    d.text((W * gx / 3 + 3, H * gy / 3 + 2), f"{(gx + 0.5) / 3:.2f},{(gy + 0.5) / 3:.2f}", fill=GRID_LABEL_COLOR)
            for tg in targets:
                x0, y0 = (tg["x"] - tg["w"] / 2) * W, (tg["y"] - tg["h"] / 2) * H
                x1, y1 = (tg["x"] + tg["w"] / 2) * W, (tg["y"] + tg["h"] / 2) * H
                col = LOCKED_BOX_COLOR if tg["locked"] else TARGET_BOX_COLOR
                d.rectangle([x0, y0, x1, y1], outline=col, width=3)
                d.text((x0 + 4, max(0, y0 - 12)), f"#{tg['id']} {tg['label']} {tg['distance_m']}m", fill=col)
        out = a.get("out")
        path = Path(out) if out else CACHE_DIR / "snapshots" / f"look-{time.strftime('%Y%m%d-%H%M%S')}-{int(time.time() * 1000) % 1000:03d}.jpg"
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        await asyncio.to_thread(_write_bytes, path, buf.getvalue())
        t = c.telemetry
        return {
            "path": str(path.resolve()),
            "width": img.width,
            "height": img.height,
            "age_ms": int((time.time() - hub.last_frame_ts) * 1000),
            "gimbal_deg": t.gimbal_pitch_deg,
            "targets": targets,
            "summary": f"saved camera view {img.width}x{img.height}" + (f" with {len(targets)} target(s): "
                       + ", ".join(f"#{x['id']} {x['label']} {x['where']}" for x in targets[:5]) if targets else ", no targets")
            + " — open `path` to see it",
        }

    async def _look_view(self, view: str, a: Args) -> dict[str, Any]:
        """A view registered by an extension (AgentExtensionContext.add_look_view)."""
        handler = self.rt.hooks.look_views.get(view)
        if handler is None:
            raise AgentError("BAD_ARGUMENT", f"Unknown view {view!r}.", hint="See `nebi schema look` for the views.",
                             exit_code=S.EXIT_USAGE)
        res = await handler(a)
        res.setdefault("view", view)
        return res

    # ------------------------------------------------------------------ wait / events

    @command("wait")
    async def wait(self, a: Args) -> dict[str, Any]:
        timeout = float(a.get("timeout") or 15.0)
        if a.get("for"):
            return await self._wait_for_event(str(a["for"]), timeout)
        if not a.get("condition"):
            raise AgentError(
                "BAD_ARGUMENT",
                "Give a state condition or --for <event>.",
                hint="e.g. `nebi wait posture=BALANCING` or `nebi wait --for petted --timeout 2m`.",
                exit_code=S.EXIT_USAGE,
            )
        c = self.client()
        clauses = parse_condition(str(a["condition"]))

        def snapshot() -> dict[str, Any]:
            cc = self.manager.client or c
            full = cc.telemetry.to_dict()
            full.pop("link", None)
            full.update(compact_state(self.manager, cc))
            return full

        unknown = [f for f, _, _ in clauses if f not in snapshot()]
        if unknown:
            raise AgentError(
                "UNKNOWN_FIELD",
                f"Unknown state field(s): {', '.join(unknown)}.",
                hint="Fields come from `nebi state` / `nebi state --full` (e.g. posture, battery, docked, targets, motion).",
                exit_code=S.EXIT_USAGE,
            )

        def holds() -> bool:
            s = snapshot()
            return all(eval_clause(s.get(f), op, v) for f, op, v in clauses)

        t0 = time.monotonic()
        ok = await wait_until(holds, timeout, poll=0.1)
        s = snapshot()
        observed = {f: s.get(f) for f, _, _ in clauses}
        if not ok:
            raise AgentError(
                "WAIT_TIMEOUT",
                f"Condition {a['condition']!r} not met within {timeout:g}s.",
                hint="Inspect `observed`; increase --timeout or act to make it true.",
                exit_code=S.EXIT_TIMEOUT,
                details={"observed": observed},
            )
        return {"met": True, "waited_s": round(time.monotonic() - t0, 2), "observed": observed}

    async def _wait_for_event(self, what: str, timeout: float) -> dict[str, Any]:
        """`wait --for name[=value]`: an extension's event, else one of the core telemetry events."""
        name, _, val = what.partition("=")
        name = name.strip().lower().replace("-", "_")
        handler = self.rt.hooks.wait_events.get(name)
        if handler is not None:
            return await handler(name, val.strip(), timeout)
        if name not in S.CORE_WAIT_EVENTS:
            raise bad_arg(f"Unknown event {what!r}.",
                          hint="One of: " + ", ".join(f"{k} ({d})" for k, d in S.wait_event_names().items()) + ".")
        log = self.events
        cursor = log.last_seq
        t0 = time.monotonic()
        while True:
            left = timeout - (time.monotonic() - t0)
            if left <= 0:
                raise AgentError("WAIT_TIMEOUT", f"No {S.CORE_WAIT_EVENTS[name]} within {timeout:g}s.",
                                 hint="Increase --timeout, or check `nebi state` / `nebi events`.", exit_code=S.EXIT_TIMEOUT)
            await log.wait_for_new(cursor, left)
            evs = log.since(cursor, 1000)
            hit = next((e for e in evs if core_event_matches(name, e)), None)
            if hit is not None:
                return {"met": True, "event": hit, "summary": event_summary(hit), "waited_s": round(time.monotonic() - t0, 1)}
            if evs:
                cursor = evs[-1]["seq"]

    @command("events")
    async def events_(self, a: Args) -> dict[str, Any]:
        since = int(a.get("since") or 0)
        log = self.events
        if since > log.last_seq:
            # Cursor from a previous daemon instance: restart from the beginning of this one.
            a["_warnings"].append("Cursor is newer than this daemon's log (daemon restarted?); replaying from start.")
            since = 0
        source = a.get("source") or "all"
        wait = float(a.get("wait") or 0.0)
        limit = int(a.get("limit") or 50)
        sources = {n: s for n, s in self.rt.hooks.event_sources.items() if source in ("all", n)}
        out: dict[str, Any] = {}
        if source in ("robot", "all"):
            # In "all" mode the extension streams do the long-poll (when there are any and nothing is new here).
            await log.wait_for_new(since, wait if source == "robot" or not sources else 0.0)
            evs = log.since(since, limit)
            out.update(events=evs, next_since=evs[-1]["seq"] if evs else since)
        ext_wait = wait if source != "all" or not out.get("events") else 0.0
        summaries: list[str] = []

        async def poll(name: str) -> None:
            src = sources[name]
            cur = int(a.get(f"{name}_since") or 0)
            try:
                sev, last = await src.poll(cur, wait=ext_wait, limit=limit)
            except Exception as err:
                if source == name:
                    raise
                code = err.code if isinstance(err, AgentError) else type(err).__name__
                a["_warnings"].append(f"{name} events unavailable ({code}); not included.")
                return
            out[f"{name}_events"] = sev
            out[f"next_{name}_since"] = sev[-1]["seq"] if sev else last
            summaries.extend(str(e.get("summary") or e.get("kind") or "") for e in sev[-8:])

        await asyncio.gather(*(poll(n) for n in sources))
        if summaries:
            out["summary"] = "; ".join(summaries)
        return out

    # ------------------------------------------------------------------ robot info

    @command("info")
    async def info(self, a: Args) -> dict[str, Any]:
        c = self.client()
        t = c.telemetry
        info: dict[str, Any] = {
            "model": "Beni robot (LZ110/LZ100) — two-wheeled-legged self-balancing robot with gimbal camera",
            "serial_number": t.serial_number,
            "hardware_version": t.hardware_version,
            "firmware_version": t.firmware_version,
            "flow": c.flow,
            "host": c.host,
            "encrypted_md_ts": bool(c.bus.md_ts_active),
            "capabilities": {
                "drive": "forward/backward/strafe, yaw, ~0-2 m/s",
                "posture": "stand/sit, height 8-14cm, jump, flips",
                "camera": "gimbal pitch, photo, video, zoom, live FPV",
                "vision": "person/pet detection, tracking, 5 follow styles",
                "dock": "autonomous return-to-dock & charging",
                "led_eyes": bool(t.led_direct_supported),
                "speaker": "built-in sound effects",
            },
        }
        try:
            info["version_rpc"] = await asyncio.wait_for(c.system.get_version(timeout=1.5), 2.0)
        except Exception:  # optional extra; some firmwares do not answer the version RPC
            logger.debug("get_version failed", exc_info=True)
        return info

    @command("faults")
    async def faults(self, a: Args) -> dict[str, Any]:
        c = self.client()
        res: dict[str, Any] = {}
        if a.get("clear"):
            res["cleared"] = await c.system.clear_hms_fault(str(a["clear"]))
            await asyncio.sleep(0.3)
        res["faults"] = [asdict(h) for h in c.telemetry.hms_alerts]
        res["count"] = len(res["faults"])
        return res
