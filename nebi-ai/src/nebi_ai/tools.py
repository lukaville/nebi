"""LangChain tools exposing Nebi robot capabilities safely to the DeepAgents pet agent."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from langchain_core.tools import BaseTool, tool

from nebi_ai.bridge import RobotBridge
from nebi_ai.config import PetConfig


def _fmt(envelope: dict[str, Any]) -> str:
    """Serialize a `nebi` command response compactly for LLM tool output."""
    compact: dict[str, Any] = {"ok": bool(envelope.get("ok"))}
    if envelope.get("ok"):
        result = envelope.get("result")
        if isinstance(result, dict):
            compact["result"] = result
        if envelope.get("warnings"):
            compact["warnings"] = envelope["warnings"]
    else:
        compact["error"] = envelope.get("error")
    if envelope.get("state"):
        compact["state"] = envelope["state"]
    return json.dumps(compact, ensure_ascii=False)


def build_robot_tools(
    bridge: RobotBridge,
    get_config: Callable[[], PetConfig],
    is_autonomous_turn: Callable[[], bool] | None = None,
) -> list[BaseTool]:
    """Create the bounded, safety-aware LangChain toolset backed by `bridge`."""
    check_auto = is_autonomous_turn or (lambda: False)

    @tool
    async def robot_state() -> str:
        """Get the robot's current state: posture, battery %, charging/docked, targets, obstacle distance, faults."""
        return _fmt(await bridge.call("state", {}))

    @tool
    async def robot_care() -> str:
        """Check the pet's needs (battery, faults, Wi-Fi), mood, and care advice."""
        return _fmt(await bridge.call("care", {}))

    @tool
    async def robot_look(annotate: bool = True) -> str:
        """Capture a live camera frame and list detected people/pets with their position and estimated distance."""
        return _fmt(await bridge.call("look", {"annotate": annotate}))

    @tool
    async def robot_say(text: str = "", emotion: str = "happy", babble: bool = False, nod: bool = False) -> str:
        """Speak through the robot's speaker in Nebi's pet voice.

        Prefer 1-6 short words with a clear emotion (neutral, happy, excited, curious, sad, scared, sleepy,
        grumpy, love, uh-oh), or set `babble=True` for cute robot chirps without words.
        """
        cfg = get_config()
        if not cfg.voice_enabled:
            return json.dumps({"ok": False, "error": {"code": "VOICE_DISABLED", "message": "Voice is muted in settings."}})
        args: dict[str, Any] = {"emotion": emotion, "babble": babble, "nod": nod}
        if text:
            args["text"] = text
        return _fmt(await bridge.call("say", args))

    @tool
    async def robot_emote(kind: str = "happy") -> str:
        """Express an emotion with synchronized eyes and voice.

        Supported kinds: greet, happy, curious, love, sad, celebrate.
        """
        return _fmt(await bridge.call("emote", {"kind": kind}))

    @tool
    async def robot_eyes(value: str = "happy", hold_sec: float = 8.0) -> str:
        """Show an expression or color on the robot's LED eyes (e.g. 'happy', 'heart', 'curious', 'blue', '#ff8800', 'release')."""
        if value == "release":
            return _fmt(await bridge.call("eyes", {"action": "release"}))
        return _fmt(await bridge.call("eyes", {"value": value, "hold": hold_sec}))

    @tool
    async def robot_body(gesture: str = "wiggle", intensity: float = 0.7, silent: bool = False) -> str:
        """Perform in-place pet body language (returns to neutral).

        Supported gestures: nod, shake, perk, cower, wiggle, curious, look-at, sleep, wake.
        """
        return _fmt(await bridge.call("body", {"gesture": gesture, "intensity": intensity, "silent": silent}))

    @tool
    async def robot_stand() -> str:
        """Stand up and balance on the robot's wheels."""
        if check_auto() and not get_config().autonomy_allow_drive:
            return json.dumps(
                {"ok": False, "error": {"code": "AUTONOMY_LOCOMOTION_LOCKED", "message": "Enable 'Allow locomotion in autonomy' to stand automatically."}}
            )
        return _fmt(await bridge.call("stand", {}))

    @tool
    async def robot_sit() -> str:
        """Fold the robot's legs into a safe resting sitting pose."""
        return _fmt(await bridge.call("sit", {}))

    @tool
    async def robot_stop() -> str:
        """Immediately stop all robot motion and silence speech."""
        return _fmt(await bridge.call("stop", {}))

    @tool
    async def robot_dock() -> str:
        """Start auto-docking to the charging station."""
        return _fmt(await bridge.call("dock", {}))

    @tool
    async def robot_drive(direction: str = "forward", distance_m: float = 0.4, speed: float = 0.35) -> str:
        """Drive a short bounded distance (`direction`: forward, backward, left, right). Stops early for obstacles."""
        cfg = get_config()
        if check_auto() and not cfg.autonomy_allow_drive:
            return json.dumps(
                {"ok": False, "error": {"code": "AUTONOMY_LOCOMOTION_LOCKED", "message": "Locomotion is disabled during autonomous ticks."}}
            )
        clamped_dist = max(0.05, min(float(distance_m), cfg.max_drive_m))
        clamped_speed = max(0.1, min(float(speed), 0.7))
        return _fmt(await bridge.call("drive", {"direction": direction, "distance": clamped_dist, "speed": clamped_speed}))

    @tool
    async def robot_turn(direction: str = "left", degrees: float = 45.0) -> str:
        """Turn in place (`direction`: left or right, `degrees`: 5 to 180)."""
        cfg = get_config()
        if check_auto() and not cfg.autonomy_allow_drive:
            return json.dumps(
                {"ok": False, "error": {"code": "AUTONOMY_LOCOMOTION_LOCKED", "message": "Locomotion is disabled during autonomous ticks."}}
            )
        clamped_deg = max(5.0, min(float(degrees), 180.0))
        return _fmt(await bridge.call("turn", {"direction": direction, "degrees": clamped_deg}))

    @tool
    async def robot_tilt(degrees: float = 0.0) -> str:
        """Tilt the robot's gimbal camera up (positive) or down (negative) in degrees."""
        clamped = max(-30.0, min(float(degrees), 30.0))
        return _fmt(await bridge.call("tilt", {"degrees": clamped}))

    @tool
    async def robot_targets() -> str:
        """List currently detected people and pets with their IDs, positions, and estimated distances."""
        return _fmt(await bridge.call("targets", {}))

    @tool
    async def robot_track(target_id: int) -> str:
        """Lock the gimbal camera onto a detected target ID from `robot_targets` or `robot_look`."""
        return _fmt(await bridge.call("track", {"target_id": int(target_id)}))

    @tool
    async def robot_follow(mode: str = "trail", distance_m: float = 1.5) -> str:
        """Follow the currently tracked person/pet (`mode`: trail, orbit, side, front)."""
        cfg = get_config()
        if check_auto() and not cfg.autonomy_allow_drive:
            return json.dumps(
                {"ok": False, "error": {"code": "AUTONOMY_LOCOMOTION_LOCKED", "message": "Locomotion is disabled during autonomous ticks."}}
            )
        return _fmt(await bridge.call("follow", {"mode": mode, "distance": float(distance_m)}))

    @tool
    async def robot_unfollow() -> str:
        """Stop following and release target tracking."""
        return _fmt(await bridge.call("unfollow", {}))

    @tool
    async def robot_who() -> str:
        """Recognize known faces currently in front of the camera."""
        return _fmt(await bridge.call("who", {}))

    @tool
    async def robot_meet(name: str) -> str:
        """Learn the face of the person currently in front of the camera under `name`."""
        return _fmt(await bridge.call("meet", {"name": name.strip()}))

    @tool
    async def robot_events() -> str:
        """Read recent robot telemetry events (target appeared/lost, posture/charging changes, faults)."""
        events = await bridge.poll_events()
        return json.dumps({"ok": True, "events": events}, ensure_ascii=False)

    return [
        robot_state,
        robot_care,
        robot_look,
        robot_say,
        robot_emote,
        robot_eyes,
        robot_body,
        robot_stand,
        robot_sit,
        robot_stop,
        robot_dock,
        robot_drive,
        robot_turn,
        robot_tilt,
        robot_targets,
        robot_track,
        robot_follow,
        robot_unfollow,
        robot_who,
        robot_meet,
        robot_events,
    ]
