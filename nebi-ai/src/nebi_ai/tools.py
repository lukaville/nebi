"""LangChain tools exposing ALL Nebi robot capabilities (vision, hearing, voice, motion, senses, extensions)."""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from typing import Any

from langchain_core.tools import BaseTool, tool

from nebi_ai.bridge import RobotBridge
from nebi_ai.config import PetConfig

# Commands that physically move the robot body and are gated during autonomous ticks unless opted in.
_LOCOMOTION_COMMANDS: frozenset[str] = frozenset(
    {"stand", "drive", "turn", "move", "jump", "flip", "follow", "dock", "undock", "height"}
)

# Destructive/lifecycle commands blocked from generic `robot_command` execution.
_BLOCKED_GENERIC_COMMANDS: frozenset[str] = frozenset({"down", "shutdown", "disconnect", "estop", "flip"})


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


def _locomotion_blocked_error(command: str) -> str:
    return json.dumps(
        {
            "ok": False,
            "error": {
                "code": "AUTONOMY_LOCOMOTION_LOCKED",
                "message": f"Command '{command}' is disabled during autonomous ticks; enable 'Allow Driving During Autonomy' in Parameters.",
            },
        }
    )


def build_robot_tools(
    bridge: RobotBridge,
    get_config: Callable[[], PetConfig],
    is_autonomous_turn: Callable[[], bool] | None = None,
) -> list[BaseTool]:
    """Create the complete, multimodal, safety-aware LangChain toolset backed by `bridge`."""
    check_auto = is_autonomous_turn or (lambda: False)

    def _locomotion_blocked() -> bool:
        return check_auto() and not get_config().autonomy_allow_drive

    # --- Perception: Vision, Hearing, Senses & Telemetry ---

    @tool
    async def robot_look(annotate: bool = True, max_width: int = 640) -> list[dict[str, Any]] | str:
        """Capture a live camera frame and return BOTH the image (so you can see the scene) and detected targets."""
        env = await bridge.call("look", {"annotate": annotate, "max_width": max_width})
        text_summary = _fmt(env)
        img_bytes = bridge.read_last_snapshot_bytes()
        if env.get("ok") and img_bytes:
            b64 = base64.b64encode(img_bytes).decode("ascii")
            return [
                {"type": "text", "text": text_summary},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            ]
        return text_summary

    @tool
    async def robot_listen(seconds: float = 8.0, language: str = "") -> str:
        """Listen through the robot's microphone and transcribe human speech (stops ~1 s after speech ends).

        If the first call returns `stt="loading"`, retry once after a few seconds while the speech model loads.
        """
        args: dict[str, Any] = {"seconds": max(1.0, min(float(seconds), 30.0))}
        if language.strip():
            args["language"] = language.strip()
        return _fmt(await bridge.call("listen", args))

    @tool
    async def robot_state(full: bool = False) -> str:
        """Get the robot's current state: posture, battery %, charging/docked, targets, obstacle distance, faults."""
        return _fmt(await bridge.call("state", {"full": full}))

    @tool
    async def robot_care() -> str:
        """Check the pet's needs (battery, faults, Wi-Fi), mood, and care advice."""
        return _fmt(await bridge.call("care", {}))

    @tool
    async def robot_sense() -> str:
        """Read all available robot & extension senses right now (posture, carried state, people, surroundings)."""
        return _fmt(await bridge.call("sense", {}))

    @tool
    async def robot_feel() -> str:
        """Read body feelings: posture, pitch/roll, whether being carried or shaken, and battery."""
        return _fmt(await bridge.call("feel", {}))

    @tool
    async def robot_targets() -> str:
        """List currently detected people and pets with their IDs, positions, and estimated distances."""
        return _fmt(await bridge.call("targets", {}))

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

    @tool
    async def robot_wait(for_event: str = "", condition: str = "", timeout_s: float = 15.0) -> str:
        """Wait for a robot event (`for_event`: person, person_left, battery_low, charging, docked, posture, fault)
        or a state condition (e.g. `targets>=1`, `posture=BALANCING`)."""
        args: dict[str, Any] = {"timeout": max(1.0, min(float(timeout_s), 60.0))}
        if for_event.strip():
            args["for"] = for_event.strip()
        if condition.strip():
            args["condition"] = condition.strip()
        return _fmt(await bridge.call("wait", args))

    @tool
    async def robot_faults(clear_code: str = "") -> str:
        """List active HMS health faults with recovery hints, or acknowledge a fault by `clear_code`."""
        args: dict[str, Any] = {"clear": clear_code.strip()} if clear_code.strip() else {}
        return _fmt(await bridge.call("faults", args))

    @tool
    async def robot_info() -> str:
        """Get robot identity: serial number, hardware/firmware versions, link flow, and loaded extensions."""
        return _fmt(await bridge.call("info", {}))

    # --- Expression: Voice, Sound Effects, Eyes, Body Language ---

    @tool
    async def robot_say(
        text: str = "",
        emotion: str = "happy",
        babble: bool = False,
        nod: bool = False,
        volume: float | None = None,
    ) -> str:
        """Speak through the robot's speaker in Nebi's pet voice.

        Prefer 1-6 short words with a clear emotion (neutral, happy, excited, curious, sad, scared, sleepy,
        grumpy, love, uh-oh), or set `babble=True` for cute robot chirps without words.
        """
        if not get_config().voice_enabled:
            return json.dumps({"ok": False, "error": {"code": "VOICE_DISABLED", "message": "Voice is muted in settings."}})
        args: dict[str, Any] = {"emotion": emotion, "babble": babble, "nod": nod}
        if text:
            args["text"] = text
        if volume is not None:
            args["volume"] = max(0.0, min(float(volume), 1.5))
        return _fmt(await bridge.call("say", args))

    @tool
    async def robot_sound(name: str) -> str:
        """Play a built-in robot sound effect (e.g. 'meow', 'bark', 'laugh', 'beep', 'trumpet')."""
        if not get_config().voice_enabled:
            return json.dumps({"ok": False, "error": {"code": "VOICE_DISABLED", "message": "Voice is muted in settings."}})
        return _fmt(await bridge.call("sound", {"name": name.strip()}))

    @tool
    async def robot_emote(emotion: str = "happy") -> str:
        """One-shot expressive combo of LED eyes + voice babble/sound.

        Supported emotions: greet, happy, curious, love, sad, celebrate.
        """
        return _fmt(await bridge.call("emote", {"emotion": emotion.strip()}))

    @tool
    async def robot_eyes(
        what: str = "happy",
        hold: float = 8.0,
        repeat: int | None = None,
        brightness: float | None = None,
    ) -> str:
        """Set the LED eyes to an animated expression ('happy', 'cool', 'sleepy', 'love', 'heart', ...)
        or a solid color ('blue', 'red', '#ff8800', 'off', 'release')."""
        args: dict[str, Any] = {"what": what.strip(), "hold": float(hold)}
        if repeat is not None:
            args["repeat"] = int(repeat)
        if brightness is not None:
            args["brightness"] = max(0.0, min(float(brightness), 1.0))
        return _fmt(await bridge.call("eyes", args))

    @tool
    async def robot_body(gesture: str = "wiggle", intensity: float = 0.7, silent: bool = False) -> str:
        """Perform in-place pet body language (returns to neutral).

        Supported gestures: nod, shake, perk, cower, wiggle, curious, look-at, sleep, wake.
        """
        return _fmt(await bridge.call("body", {"gesture": gesture.strip(), "intensity": float(intensity), "silent": silent}))

    # --- Camera & Media ---

    @tool
    async def robot_tilt(direction: str = "center", duration_s: float = 0.4) -> str:
        """Tilt the camera gimbal (`direction`: 'up', 'down', or 'center'; `duration_s`: 0.1 to 2.0)."""
        args: dict[str, Any] = {"direction": direction.strip()}
        if direction.strip() != "center":
            args["for"] = max(0.1, min(float(duration_s), 2.0))
        return _fmt(await bridge.call("tilt", args))

    @tool
    async def robot_zoom(factor: float = 1.0) -> str:
        """Set digital camera zoom factor (`1.0` = no zoom)."""
        return _fmt(await bridge.call("zoom", {"factor": max(1.0, min(float(factor), 4.0))}))

    @tool
    async def robot_photo() -> str:
        """Take a full-resolution photo and store it on the robot."""
        return _fmt(await bridge.call("photo", {}))

    @tool
    async def robot_record(action: str = "start") -> str:
        """Control MP4 video recording on the robot (`action`: 'start', 'stop', or 'highlight')."""
        return _fmt(await bridge.call("record", {"action": action.strip()}))

    # --- Locomotion, Tracking & Modes ---

    @tool
    async def robot_stand() -> str:
        """Stand up and self-balance on wheels (required before driving)."""
        if _locomotion_blocked():
            return _locomotion_blocked_error("stand")
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
        """Autonomously return to the charging dock."""
        if _locomotion_blocked():
            return _locomotion_blocked_error("dock")
        return _fmt(await bridge.call("dock", {}))

    @tool
    async def robot_undock() -> str:
        """Leave the charging dock."""
        if _locomotion_blocked():
            return _locomotion_blocked_error("undock")
        return _fmt(await bridge.call("undock", {}))

    @tool
    async def robot_drive(direction: str = "forward", distance_m: float = 0.4, speed: float = 0.35) -> str:
        """Drive a short bounded distance (`direction`: forward, backward, left, right). Stops early for obstacles."""
        if _locomotion_blocked():
            return _locomotion_blocked_error("drive")
        cfg = get_config()
        clamped_dist = max(0.05, min(float(distance_m), cfg.max_drive_m))
        clamped_speed = max(0.1, min(float(speed), 0.7))
        return _fmt(await bridge.call("drive", {"direction": direction, "distance": clamped_dist, "speed": clamped_speed}))

    @tool
    async def robot_turn(direction: str = "left", degrees: float = 45.0) -> str:
        """Turn in place (`direction`: 'left' or 'right', `degrees`: 5 to 180)."""
        if _locomotion_blocked():
            return _locomotion_blocked_error("turn")
        clamped_deg = max(5.0, min(float(degrees), 180.0))
        return _fmt(await bridge.call("turn", {"direction": direction, "degrees": clamped_deg}))

    @tool
    async def robot_height(value: float = 0.5) -> str:
        """Set standing body height (`0.0` = crouch ~8 cm, `1.0` = tall ~14 cm)."""
        if _locomotion_blocked():
            return _locomotion_blocked_error("height")
        return _fmt(await bridge.call("height", {"value": max(0.0, min(float(value), 1.0))}))

    @tool
    async def robot_jump() -> str:
        """Jump in place (~15-20 cm). Only use when balancing with clear space."""
        if _locomotion_blocked():
            return _locomotion_blocked_error("jump")
        return _fmt(await bridge.call("jump", {}))

    @tool
    async def robot_mode(mode: str = "manual") -> str:
        """Switch robot behaviour mode (`manual`, `companion` / `ego`, etc.)."""
        return _fmt(await bridge.call("mode", {"mode": mode.strip()}))

    @tool
    async def robot_track(target: str) -> str:
        """Lock AI gimbal tracking onto a target ID (e.g. '101') or normalized image point (e.g. '0.5,0.5')."""
        return _fmt(await bridge.call("track", {"target": str(target).strip()}))

    @tool
    async def robot_follow(style: str = "trail", distance_m: float = 1.5) -> str:
        """Autonomously follow the locked target (`style`: trail, orbit, side, front; `distance_m`: meters)."""
        if _locomotion_blocked():
            return _locomotion_blocked_error("follow")
        return _fmt(await bridge.call("follow", {"style": style.strip(), "distance": float(distance_m)}))

    @tool
    async def robot_unfollow() -> str:
        """Stop following/tracking and return to manual mode."""
        return _fmt(await bridge.call("unfollow", {}))

    @tool
    async def robot_idle(action: str = "status", level: str = "playful") -> str:
        """Control the daemon's built-in idle pet reflexes (`action`: 'start', 'stop', 'status'; `level`: calm/playful)."""
        args: dict[str, Any] = {"action": action.strip()}
        if action.strip() == "start":
            args["level"] = level.strip()
        return _fmt(await bridge.call("idle", args))

    # --- Universal Discovery & Extension Access ---

    @tool
    async def robot_schema(name: str = "") -> str:
        """Inspect available `nebi` commands (including any installed extensions) and their arguments."""
        from nebi_cli.spec import schema_dict

        return json.dumps({"ok": True, "result": schema_dict(only=name.strip() or None)}, ensure_ascii=False)

    @tool
    async def robot_command(command: str, args_json: str = "{}") -> str:
        """Invoke any `nebi` command (including extension commands discovered via `robot_schema`) by name."""
        cmd = command.strip().lower()
        if not cmd:
            return json.dumps({"ok": False, "error": {"code": "BAD_ARGUMENT", "message": "command must not be empty"}})
        if cmd in _BLOCKED_GENERIC_COMMANDS:
            return json.dumps({"ok": False, "error": {"code": "COMMAND_BLOCKED", "message": f"Command '{cmd}' is not allowed via generic robot_command."}})
        if cmd in _LOCOMOTION_COMMANDS and _locomotion_blocked():
            return _locomotion_blocked_error(cmd)
        if cmd in ("say", "sound") and not get_config().voice_enabled:
            return json.dumps({"ok": False, "error": {"code": "VOICE_DISABLED", "message": "Voice is muted in settings."}})
        try:
            parsed_args = json.loads(args_json) if args_json.strip() else {}
            if not isinstance(parsed_args, dict):
                raise ValueError("args_json must decode to a JSON object")
        except Exception as exc:
            return json.dumps({"ok": False, "error": {"code": "BAD_ARGUMENT", "message": f"Invalid args_json: {exc}"}})
        return _fmt(await bridge.call(cmd, parsed_args))

    return [
        # Perception & senses
        robot_look,
        robot_listen,
        robot_state,
        robot_care,
        robot_sense,
        robot_feel,
        robot_targets,
        robot_who,
        robot_meet,
        robot_events,
        robot_wait,
        robot_faults,
        robot_info,
        # Expression & voice
        robot_say,
        robot_sound,
        robot_emote,
        robot_eyes,
        robot_body,
        # Camera & media
        robot_tilt,
        robot_zoom,
        robot_photo,
        robot_record,
        # Locomotion & modes
        robot_stand,
        robot_sit,
        robot_stop,
        robot_dock,
        robot_undock,
        robot_drive,
        robot_turn,
        robot_height,
        robot_jump,
        robot_mode,
        robot_track,
        robot_follow,
        robot_unfollow,
        robot_idle,
        # Universal extension access
        robot_schema,
        robot_command,
    ]
