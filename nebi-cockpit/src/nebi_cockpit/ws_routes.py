"""WebSocket routes: `/ws/control` (joystick + telemetry, with dead-man), `/ws/video` (JPEG), `/ws/intercom`.

Origin/Host checks for every socket live in `nebi_cockpit.security.SecurityMiddleware`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from nebi.client import AsyncNebiClient
from nebi.types import DEFAULT_BODY_HEIGHT
from nebi_cockpit.connection_manager import NebiServerManager
from nebi_cockpit.deps import IntercomHubDep, ManagerDep
from nebi_cockpit.intercom_ws import intercom_socket

logger = logging.getLogger(__name__)

STICK_KEYS = ("lx", "ly", "rx", "ry", "roll")
NEUTRAL_EPS = 1e-3
"""|axis| at or below this counts as centred."""

DEADMAN_DEFAULT_S = 0.5
DEADMAN_MIN_S = 0.2
DEADMAN_MAX_S = 5.0
DEADMAN_TICK_S = 0.1
STATUS_PUSH_PERIOD_S = 0.066  # ~15 Hz HUD + connection stage push


class BadInput(ValueError):
    """A control message field that is not an acceptable number / shape."""


def _reject_constant(name: str) -> Any:
    raise BadInput(f"non-finite number {name} not allowed")


def parse_control_message(raw: str) -> dict[str, Any]:
    """`json.loads` that refuses NaN / Infinity (Python accepts them by default)."""
    msg = json.loads(raw, parse_constant=_reject_constant)
    if not isinstance(msg, dict):
        raise BadInput("control message must be a JSON object")
    return msg


def _num(msg: dict[str, Any], key: str, default: float, lo: float, hi: float) -> float:
    v = msg.get(key, default)
    if v is None:
        v = default
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise BadInput(f"{key} must be a number")
    v = float(v)
    if not math.isfinite(v):
        raise BadInput(f"{key} must be finite")
    return min(hi, max(lo, v))


def parse_joystick(msg: dict[str, Any]) -> dict[str, Any]:
    """Validated + clamped joystick fields (sticks/roll in [-1, 1], height in [0, 1])."""
    return {
        "lx": _num(msg, "lx", 0.0, -1.0, 1.0),
        "ly": _num(msg, "ly", 0.0, -1.0, 1.0),
        "rx": _num(msg, "rx", 0.0, -1.0, 1.0),
        "ry": _num(msg, "ry", 0.0, -1.0, 1.0),
        "roll": _num(msg, "roll", 0.0, -1.0, 1.0),
        "height": _num(msg, "height", DEFAULT_BODY_HEIGHT, 0.0, 1.0),
        "roll_mode": bool(msg.get("roll_mode", False)),
        "gamepad_source": bool(msg.get("gamepad_source", False)),
    }


def _is_neutral(axes: dict[str, Any]) -> bool:
    return all(abs(axes[k]) <= NEUTRAL_EPS for k in STICK_KEYS)


def apply_neutral(client: AsyncNebiClient | None) -> bool:
    """Zero lx/ly/rx/ry/roll in the client's latched joystick state, connected or not.
    Returns True once the neutral is really applied (False = retry)."""
    if client is None:
        return True  # nothing latched anywhere; a new client starts neutral
    try:
        motion = client.motion
        motion.joystick.roll = 0.0
        motion.neutral()
        return True
    except Exception as exc:
        logger.warning("dead-man neutral failed (will retry): %s", exc)
        return False


def parse_deadman_ms(raw: str | None) -> float:
    """`?deadman_ms=` -> seconds, clamped to [DEADMAN_MIN_S, DEADMAN_MAX_S]; garbage -> default."""
    try:
        deadman_s = max(DEADMAN_MIN_S, min(DEADMAN_MAX_S, float(raw if raw is not None else DEADMAN_DEFAULT_S * 1000.0) / 1000.0))
    except ValueError:
        return DEADMAN_DEFAULT_S
    return deadman_s if math.isfinite(deadman_s) else DEADMAN_DEFAULT_S


class JoystickDeadman:
    """Per-socket joystick state + dead-man switch for `/ws/control`.

    Tracks the client this socket last drove so a neutral reaches it even after the manager swapped
    clients (reconnect, flow switch). `moving` stays True until a neutral is confirmed applied."""

    def __init__(self, manager: NebiServerManager, timeout_s: float) -> None:
        self._manager = manager
        self.timeout_s = timeout_s
        self.moving = False
        self._last_input = 0.0
        self._driven: AsyncNebiClient | None = None

    def neutral(self) -> None:
        current = self._manager.client
        ok = apply_neutral(current)
        driven = self._driven
        if driven is not None and driven is not current:
            ok = apply_neutral(driven) and ok  # the client we drove was swapped out meanwhile
        if ok:
            self.moving = False
            self._driven = None

    def expired(self) -> bool:
        return self.moving and time.monotonic() - self._last_input > self.timeout_s

    def handle(self, axes: dict[str, Any]) -> None:
        """Apply one validated joystick message."""
        self._last_input = time.monotonic()
        client = self._manager.client
        if _is_neutral(axes):
            self.moving = True  # until neutral is confirmed applied
            self.neutral()
            if client is not None and client.connected:
                # keep height / roll_mode / gamepad flags current
                client.motion.set_axes(**axes)
            return
        if client is None or not client.connected:
            return  # never latch a non-neutral stick while the link is down
        self.moving = True
        self._driven = client
        client.motion.set_axes(**axes)


def _error_text(message: str) -> str:
    return json.dumps({"type": "error", "message": message})


router = APIRouter(tags=["websocket"])


@router.websocket("/ws/video")
async def ws_video(ws: WebSocket, manager: ManagerDep) -> None:
    await ws.accept()
    manager.request_keyframe()
    q = manager.video_hub.subscribe_jpeg()
    try:
        while True:
            jpg = await q.get()
            await ws.send_bytes(jpg)
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # send on a closing socket etc.: the viewer is gone either way
        logger.debug("/ws/video ended: %r", exc)
    finally:
        manager.video_hub.unsubscribe_jpeg(q)


@router.websocket("/ws/control")
async def ws_control(ws: WebSocket, manager: ManagerDep) -> None:
    """Joystick/game control. Dead-man switch: sticks go neutral if no joystick message arrives for
    `deadman_s` (default 0.5 s, `?deadman_ms=` to change) or the socket closes, so a crashed or
    disconnected controller (browser tab, nav script) can never leave the robot driving.

    Neutral is applied to the latched joystick state even while the robot link is down, so nothing
    resumes after a reconnect. A controller holding a stick must keep sending (>= 10 Hz): the motion
    layer also expires stick axes ~0.5 s after the last update."""
    await ws.accept()
    deadman = JoystickDeadman(manager, parse_deadman_ms(ws.query_params.get("deadman_ms")))

    async def deadman_watch() -> None:
        while True:
            await asyncio.sleep(DEADMAN_TICK_S)
            if deadman.expired():
                deadman.neutral()

    async def telemetry_pusher() -> None:
        while True:
            await asyncio.sleep(STATUS_PUSH_PERIOD_S)
            await ws.send_text(json.dumps({"type": "status", **manager.status_dict()}))

    async def handle_joystick(msg: dict[str, Any]) -> None:
        try:
            axes = parse_joystick(msg)
        except BadInput as exc:
            deadman.neutral()  # malformed drive command: fail safe
            await ws.send_text(_error_text(str(exc)))
            return
        deadman.handle(axes)

    async def handle_game_input(msg: dict[str, Any]) -> None:
        client = manager.client
        if client is None or not client.connected:
            return
        try:
            client.games.send_player_input(
                pitch_deg=_num(msg, "pitch_deg", 0.0, -90.0, 90.0),
                roll_deg=_num(msg, "roll_deg", 0.0, -90.0, 90.0),
                yaw_deg=_num(msg, "yaw_deg", 0.0, -180.0, 180.0),
                jump=bool(msg.get("jump", False)),
                action_pressed=bool(msg.get("action_pressed", False)),
            )
        except BadInput as exc:
            await ws.send_text(_error_text(str(exc)))

    async def command_receiver() -> None:
        while True:
            raw = await ws.receive_text()
            try:
                msg = parse_control_message(raw)
            except ValueError as exc:  # includes BadInput (NaN/Infinity) and JSON syntax errors
                if '"joystick"' in raw:
                    deadman.neutral()
                await ws.send_text(_error_text(f"rejected: {exc}"))
                continue
            mtype = msg.get("type")
            if mtype == "joystick":
                await handle_joystick(msg)
            elif mtype == "game_input":
                await handle_game_input(msg)

    tasks = [
        asyncio.create_task(fn(), name=f"nebi-ws-control-{fn.__name__}")
        for fn in (telemetry_pusher, command_receiver, deadman_watch)
    ]
    try:
        await asyncio.gather(*tasks)
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # any loop failing ends the session; the finally block makes it safe
        logger.debug("/ws/control ended: %r", exc)
    finally:
        for t in tasks:
            t.cancel()
        if deadman.moving:
            deadman.neutral()


@router.websocket("/ws/intercom")
async def ws_intercom(ws: WebSocket, manager: ManagerDep, hub: IntercomHubDep) -> None:
    """Full-duplex PCM intercom for the browser cockpit (see `nebi_cockpit.intercom_ws`)."""
    await intercom_socket(ws, hub, lambda: manager.client)


__all__ = ["JoystickDeadman", "apply_neutral", "parse_control_message", "parse_joystick", "router"]
