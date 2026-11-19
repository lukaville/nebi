"""Locomotion commands: posture, stop, bounded drive/turn/move/tilt, stunts, dock, modes, follow.

Every moving command goes through the MotionGate: it takes the motion lock, starts a new epoch (so `stop`
preempts it) and holds the sticks with a hard time bound, ending at neutral.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any

from nebi.exceptions import NebiError
from nebi.types import CameraSubMode, SpeedGear, StuntDirection
from nebi_cli import spec as S
from nebi_cli.errors import STANDING, AgentError, bad_arg, parse_xy
from nebi_cli.motion import done_after, neutral, require_mobile
from nebi_cli.registry import Args, CommandGroup, command
from nebi_cli.safety_ops import SpeedCap
from nebi_cli.util import wait_until
from nebi_cli.views import target_confidence

logger = logging.getLogger(__name__)

# Hard safety caps per single command.
MAX_DRIVE_M = 5.0
MAX_DRIVE_S = 20.0
MAX_MOVE_S = 10.0
MAX_TURN_DEG = 720.0

POSTURE_WAIT_S = 8.0  # stand/sit: how long to wait for BALANCING / FOLDED
DEFAULT_CLEARANCE_M = 0.3
GIMBAL_STICK = 0.6  # ry deflection used by `tilt`
STRAFE_SPEED_FACTOR = 0.8  # strafing is slower than driving forward at the same stick deflection

GEAR_SCALE = {SpeedGear.DEFAULT: 1.0, SpeedGear.SOFT: 0.55, SpeedGear.STANDARD: 1.0, SpeedGear.SPORT: 1.45}
GEAR_BY_NAME = {"soft": SpeedGear.SOFT, "standard": SpeedGear.STANDARD, "sport": SpeedGear.SPORT}

FOLLOW_STYLES = {
    "trail": CameraSubMode.FOLLOW_TRAIL,
    "parallel": CameraSubMode.FOLLOW_PARALLEL,
    "lead": CameraSubMode.FOLLOW_LEAD,
    "orbit": CameraSubMode.FOLLOW_ORBIT,
    "spotlight": CameraSubMode.SPOTLIGHT,
}
FLIPS = {"front": StuntDirection.FRONT_FLIP, "back": StuntDirection.BACK_FLIP,
         "left": StuntDirection.LEFT_FLIP, "right": StuntDirection.RIGHT_FLIP}
DRIVE_AXES = {"forward": ("ly", 1), "backward": ("ly", -1), "left": ("lx", -1), "right": ("lx", 1)}


@dataclass
class _Odometer:
    """Distance feedback for `drive`: wheel odometry / integrated velocity, else time x nominal speed."""

    integ: float = 0.0
    last: float = field(default_factory=time.monotonic)
    mode: str = "odometry"  # -> "dead_reckoning" when no feedback arrives
    traveled: float = 0.0


class LocomotionCommands(CommandGroup):
    # ------------------------------------------------------------------ posture / stop

    @command("stand")
    async def stand(self, a: Args) -> dict[str, Any]:
        c = self.client()
        warnings: list[str] = a["_warnings"]
        if c.telemetry.posture_name in STANDING:
            return {"posture": c.telemetry.posture_name, "already": True, "summary": "already standing"}
        await c.motion.stand(force=bool(a.get("force")))
        if a.get("no_wait"):
            return {"sent": True, "summary": "stand command sent"}
        ok = await wait_until(lambda: c.telemetry.posture_name == "BALANCING", POSTURE_WAIT_S)
        if not ok:
            warnings.append(f"Still {c.telemetry.posture_name} after 8s; check `nebi faults` or retry `nebi stand`.")
        return {"posture": c.telemetry.posture_name, "balancing": ok,
                "summary": "standing and balancing" if ok else f"tried to stand; still {c.telemetry.posture_name}"}

    @command("sit")
    async def sit(self, a: Args) -> dict[str, Any]:
        c = self.client()
        warnings: list[str] = a["_warnings"]
        self.motion.new_epoch()
        if c.telemetry.posture_name in ("FOLDED", "FOLDING"):
            return {"posture": c.telemetry.posture_name, "already": True, "summary": "already sitting (folded)"}
        await c.motion.sit()
        if a.get("no_wait"):
            return {"sent": True, "summary": "sit command sent"}
        ok = await wait_until(lambda: c.telemetry.posture_name == "FOLDED", POSTURE_WAIT_S)
        if not ok:
            warnings.append(f"Still {c.telemetry.posture_name} after 8s.")
        return {"posture": c.telemetry.posture_name, "folded": ok,
                "summary": "sat down (folded)" if ok else f"tried to sit; still {c.telemetry.posture_name}"}

    @command("stop")
    async def stop(self, a: Args) -> dict[str, Any]:
        self.motion.new_epoch()
        self.rt.speech.interrupt()  # silence any ongoing `nebi say`
        idle_stopped = await self.rt.idle.stop()
        c = self.client()
        self.motion.new_epoch()  # again: in case the idle loop started something while unwinding
        neutral(c)
        res: dict[str, Any] = {"stopped": True, "idle_stopped": idle_stopped}
        if idle_stopped:
            res["idle_resume"] = "nebi idle start"
        res["summary"] = "stopped all motion and speech" + ("; idle life paused (resume: `nebi idle start`)" if idle_stopped else "")
        return res

    @command("estop")
    async def estop(self, a: Args) -> dict[str, Any]:
        c = self.client()
        self.motion.new_epoch()
        neutral(c)
        await c.motion.emergency_stop()
        self.events.emit("estop")
        return {
            "estop_toggled": True,
            "note": "E-stop is a hardware toggle. Check `nebi state`; `nebi stand` to recover once safe.",
        }

    # ------------------------------------------------------------------ bounded stick moves

    @command("drive")
    async def drive(self, a: Args) -> dict[str, Any]:
        c = self.client()
        warnings: list[str] = a["_warnings"]
        force = bool(a.get("force"))
        require_mobile(c, force, warnings)
        direction = a["direction"]
        dist: float | None = a.get("distance")
        dur: float | None = a.get("for")
        if dist is None and dur is None:
            raise AgentError(
                "MISSING_EXTENT",
                "Give a distance (e.g. `nebi drive forward 1m`) or a duration (`--for 2s`).",
                exit_code=S.EXIT_USAGE,
            )
        if dist is not None:
            dist = float(dist)
            if dist <= 0:
                raise bad_arg("Distance must be > 0.")
            if dist > MAX_DRIVE_M:
                warnings.append(f"Distance capped at {MAX_DRIVE_M} m per command; chain commands for longer trips.")
                dist = MAX_DRIVE_M
        if dur is not None:
            dur = min(float(dur), MAX_DRIVE_S)
        speed = max(0.05, min(1.0, float(a.get("speed") or 0.3)))
        cap: SpeedCap | None = a.get("_speed_cap")
        if cap and speed > cap.limit:
            speed = cap.limit
            warnings.append(f"{cap.reason}: speed capped to {cap.limit}.")
        clearance = a.get("clearance")
        clearance_cm = float(DEFAULT_CLEARANCE_M if clearance is None else clearance) * 100.0
        t = c.telemetry
        if direction == "forward" and clearance_cm > 0 and t.obstacle_cm and t.obstacle_cm < clearance_cm and not force:
            raise AgentError(
                "OBSTACLE_AHEAD",
                f"Obstacle {t.obstacle_cm} cm ahead (clearance {clearance_cm:.0f} cm).",
                hint="`nebi look` to check; turn away, back up, or pass --clearance 0 / --force if it is a false reading.",
                exit_code=S.EXIT_SAFETY,
            )
        axis, sign = DRIVE_AXES[direction]
        axes = {axis: sign * speed}
        max_mps, _ = await self.motion.kinematics(c)
        nominal_mps = speed * max_mps * GEAR_SCALE.get(c.motion.joystick.speed_gear, 1.0) * (STRAFE_SPEED_FACTOR if axis == "lx" else 1.0)
        dur_s = float(dur) if dur is not None else 0.0
        est_s = (dist / nominal_mps) if dist is not None else dur_s
        max_s = min(MAX_DRIVE_S + 2, (est_s * 2.5 + 2.0) if dist is not None else dur_s)

        start_odom = t.session_odom_m
        st = _Odometer()

        async with self.motion.lock:
            epoch = self.motion.new_epoch()

            def until(el: float) -> str | None:
                now = time.monotonic()
                dt, st.last = now - st.last, now
                st.integ += abs(t.velocity_mps) * dt
                odo = max(0.0, c.telemetry.session_odom_m - start_odom)
                fb = max(odo, st.integ)
                if st.mode == "odometry" and el > 1.5 and fb < 0.01:
                    st.mode = "dead_reckoning"
                st.traveled = fb if st.mode == "odometry" else nominal_mps * el
                if direction == "forward" and clearance_cm > 0 and 0 < c.telemetry.obstacle_cm < clearance_cm and not force:
                    return "obstacle"
                if dist is not None:
                    lead = abs(c.telemetry.velocity_mps) * 0.15 if st.mode == "odometry" else 0.0
                    if st.traveled + lead >= dist:
                        return "reached"
                elif el >= dur_s:
                    return "duration"
                return None

            reason, elapsed = await self.motion.hold_axes(c, axes, epoch=epoch, until=until, max_s=max_s)
        await asyncio.sleep(0.35)  # let it brake, then read final odometry
        if st.mode == "odometry":
            st.traveled = max(st.traveled, c.telemetry.session_odom_m - start_odom)
        if st.mode == "dead_reckoning":
            warnings.append("No odometry/velocity feedback; distance is an open-loop estimate. Verify with `nebi look`.")
        if reason == "obstacle":
            warnings.append(f"Stopped early: obstacle at {c.telemetry.obstacle_cm} cm.")
        res = {
            "direction": direction,
            "traveled_m": round(st.traveled, 2),
            "elapsed_s": round(elapsed, 2),
            "stopped_reason": reason,
            "feedback": st.mode,
        }
        if dist is not None:
            res["requested_m"] = round(dist, 2)
        res["speed"] = speed
        res["summary"] = f"drove {direction} {res['traveled_m']} m" + (f" of {dist:.2f} m" if dist is not None else "") + \
            f" ({reason}{', estimated' if st.mode == 'dead_reckoning' else ''})"
        if reason in ("timeout", "link_lost") and dist is not None:
            raise AgentError(
                "DRIVE_INCOMPLETE",
                f"Drove {res['traveled_m']} of {dist:.2f} m before {reason}.",
                hint="Something may be blocking the robot. `nebi look`, then try another direction.",
                exit_code=S.EXIT_TIMEOUT if reason == "timeout" else S.EXIT_UNAVAILABLE,
                details=res,
            )
        return res

    @command("turn")
    async def turn(self, a: Args) -> dict[str, Any]:
        c = self.client()
        warnings: list[str] = a["_warnings"]
        require_mobile(c, bool(a.get("force")), warnings)
        direction = a["direction"]
        if direction == "around":
            async with self.motion.lock:
                self.motion.new_epoch()
                await c.motion.turn_around()
                await asyncio.sleep(1.2)
            return {"direction": "around", "degrees": 180, "method": "quick_turn", "summary": "spun around (~180°)"}
        degrees = a.get("degrees")
        deg = abs(float(90.0 if degrees is None else degrees))
        if deg > MAX_TURN_DEG:
            raise bad_arg("Max 720 degrees per turn.")
        speed = max(0.1, min(1.0, float(a.get("speed") or 0.5)))
        _, max_dps = await self.motion.kinematics(c)
        rate = speed * max_dps * GEAR_SCALE.get(c.motion.joystick.speed_gear, 1.0)
        hold_s = deg / rate + 0.1  # +ramp-up compensation
        axes = {"rx": speed if direction == "right" else -speed}
        async with self.motion.lock:
            epoch = self.motion.new_epoch()
            reason, elapsed = await self.motion.hold_axes(c, axes, epoch=epoch, until=done_after(hold_s), max_s=hold_s + 1)
        await asyncio.sleep(0.25)
        done_deg = deg if reason == "done" else min(deg, max(0.0, (elapsed - 0.1) * rate))
        return {
            "direction": direction,
            "requested_deg": round(deg, 1),
            "estimated_deg": round(done_deg, 1),
            "stopped_reason": "reached" if reason == "done" else reason,
            "method": "open_loop_timed",
            "rate_dps": round(rate, 1),
            "summary": f"turned {direction} ~{done_deg:.0f}° of {deg:.0f}°" + ("" if reason == "done" else f" ({reason})"),
        }

    @command("move")
    async def move(self, a: Args) -> dict[str, Any]:
        c = self.client()
        warnings: list[str] = a["_warnings"]
        axes = {
            "ly": float(a.get("forward") or 0.0),
            "lx": float(a.get("strafe") or 0.0),
            "rx": float(a.get("yaw") or 0.0),
            "ry": float(a.get("pitch") or 0.0),
        }
        for v in axes.values():
            if not -1.0 <= v <= 1.0:
                raise bad_arg(f"Axis values must be within -1..1 (got {v}).")
        if any(axes[k] for k in ("ly", "lx", "rx")):
            require_mobile(c, bool(a.get("force")), warnings)
        cap: SpeedCap | None = a.get("_speed_cap")
        if cap:
            capped = [k for k in ("ly", "lx") if abs(axes[k]) > cap.limit]
            for k in capped:
                axes[k] = math.copysign(cap.limit, axes[k])
            if capped:
                names = {"ly": "forward", "lx": "strafe"}
                warnings.append(f"{cap.reason}: {'/'.join(names[k] for k in capped)} speed capped to {cap.limit}.")
        hold = min(float(a.get("for") or 1.0), MAX_MOVE_S)
        async with self.motion.lock:
            epoch = self.motion.new_epoch()
            reason, elapsed = await self.motion.hold_axes(c, axes, epoch=epoch, until=done_after(hold), max_s=hold + 0.5)
        return {"axes": axes, "held_s": round(elapsed, 2), "stopped_reason": reason,
                "summary": f"held sticks {', '.join(f'{k}={v:g}' for k, v in axes.items() if v) or 'neutral'} for {elapsed:.1f}s ({reason})"}

    @command("tilt")
    async def tilt(self, a: Args) -> dict[str, Any]:
        c = self.client()
        direction = a["direction"]
        if direction == "center":
            # Push toward neutral in whichever direction the gimbal currently leans.
            g = c.telemetry.gimbal_pitch_deg
            if abs(g) < 2:
                return {"gimbal_deg": g, "already_centered": True, "summary": "camera already centred"}
            axes = {"ry": -GIMBAL_STICK if g > 0 else GIMBAL_STICK}
            hold = min(2.0, abs(g) / 30.0)
        else:
            axes = {"ry": GIMBAL_STICK if direction == "up" else -GIMBAL_STICK}
            hold = min(3.0, float(a.get("for") or 0.5))
        async with self.motion.lock:
            epoch = self.motion.new_epoch()
            await self.motion.hold_axes(c, axes, epoch=epoch, until=done_after(hold), max_s=hold + 0.5)
        await asyncio.sleep(0.2)
        g = c.telemetry.gimbal_pitch_deg
        return {"gimbal_deg": g, "summary": f"tilted camera {direction} (gimbal {g:.0f}°)"}

    @command("height")
    async def height(self, a: Args) -> dict[str, Any]:
        c = self.client()
        v = float(a["value"])
        if not 0.0 <= v <= 1.0:
            raise bad_arg("Height must be within 0..1.")
        c.motion.set_axes(height=v)
        await asyncio.sleep(0.6)
        return {"height": v, "height_cm": c.telemetry.height_cm, "summary": f"body height set to {v:g} (~{c.telemetry.height_cm} cm)"}

    @command("gear")
    async def gear(self, a: Args) -> dict[str, Any]:
        c = self.client()
        await c.motion.set_speed_gear(GEAR_BY_NAME[a["gear"]])
        self.motion.forget_kinematics(c)
        return {"gear": a["gear"]}

    # ------------------------------------------------------------------ stunts

    @command("jump")
    async def jump(self, a: Args) -> dict[str, Any]:
        c = self.client()
        require_mobile(c, bool(a.get("force")), a["_warnings"])
        async with self.motion.lock:
            self.motion.new_epoch()
            await c.motion.jump()
            await asyncio.sleep(1.0)
        return {"jumped": True, "summary": "jumped"}

    @command("flip")
    async def flip(self, a: Args) -> dict[str, Any]:
        c = self.client()
        if not a.get("force"):
            raise AgentError(
                "CONFIRM_REQUIRED",
                "Flips are risky (needs ~1.5 m clear flat space, may damage robot or surroundings).",
                hint="Check surroundings with `nebi look`, then re-run with --force if it is really intended.",
                exit_code=S.EXIT_SAFETY,
            )
        require_mobile(c, False, a["_warnings"])  # --force confirms the flip, it does not skip the posture check
        d = FLIPS[a.get("direction") or "back"]
        async with self.motion.lock:
            self.motion.new_epoch()
            await c.motion.flip(d)
            await asyncio.sleep(2.0)
        return {"flip": d.name.lower(), "summary": f"did a {d.name.lower().replace('_', ' ')}"}

    # ------------------------------------------------------------------ dock and modes

    @command("dock")
    async def dock(self, a: Args) -> dict[str, Any]:
        c = self.client()
        self.motion.new_epoch()
        neutral(c)
        r = await c.motion.return_to_dock()
        return {**r, "next": "Await with `nebi wait docked=true --timeout 3m`; cancel with `nebi mode manual`."}

    @command("undock")
    async def undock(self, a: Args) -> dict[str, Any]:
        c = self.client()
        return await c.motion.undock()

    @command("mode")
    async def mode(self, a: Args) -> dict[str, Any]:
        c = self.client()
        self.motion.new_epoch()
        neutral(c)
        mode = a["mode"]
        fn = {
            "manual": c.motion.start_manual_camera_mode,
            "companion": c.motion.start_companion_mode,
            "ego": c.motion.start_ego_mode,
            "calibrate": c.motion.start_self_calibration,
        }[mode]
        r = await fn()
        if mode == "ego":  # the firmware only switches while standing still: report what actually happened
            a["_warnings"].extend(r.pop("warnings", []))
            if r.get("entered") is False:
                a["_warnings"].append(r.get("hint", "The robot did not enter ego mode."))
        return {"mode": mode, **r}

    # ------------------------------------------------------------------ track / follow

    @command("track")
    async def track(self, a: Args) -> dict[str, Any]:
        c = self.client()
        raw = str(a["target"]).strip()
        if "," in raw:
            x, y = parse_xy(raw, what="Point", unit_range=True)
            r = await c.tracking.lock_point(x, y)
            return {"locked_point": [x, y], **r, "summary": f"tracking the point {x:g},{y:g}"}
        try:
            tid = int(raw)
        except ValueError:
            raise bad_arg(f"Target must be an id or x,y (got {raw!r}).", hint="`nebi targets` lists ids.") from None
        ids = [tg.target_id for tg in c.telemetry.targets]
        if ids and tid not in ids:
            a["_warnings"].append(f"Target {tid} is not currently visible (visible: {ids}).")
        r = await c.tracking.lock_target(tid)
        return {"locked_target": tid, **r, "summary": f"tracking target #{tid}"}

    @command("follow")
    async def follow(self, a: Args) -> dict[str, Any]:
        c = self.client()
        t = c.telemetry
        style = a.get("style") or "trail"
        locked = t.locked_target_id
        if not t.tracking_active or not locked:
            people = sorted(
                (tg for tg in t.targets if tg.label == "person"),
                key=lambda tg: (tg.distance_m or 99.0, -target_confidence(tg)),
            ) or sorted(t.targets, key=lambda tg: tg.distance_m or 99.0)
            if not people:
                raise AgentError(
                    "NO_TARGET",
                    "Nothing is locked and no person/pet is visible to follow.",
                    hint="`nebi look --annotate` to see the scene, turn toward someone, then `nebi track <id>`.",
                )
            await c.tracking.lock_target(people[0].target_id)
            locked = people[0].target_id
            a["_warnings"].append(f"Auto-locked nearest target #{locked} ({people[0].label}).")
        dist = a.get("distance")
        r = await c.tracking.set_follow_mode(FOLLOW_STYLES[style], distance_m=float(dist) if dist else None)
        return {"following": locked, "style": style, **r, "stop_with": "nebi unfollow",
                "summary": f"following target #{locked} ({style}); stop with `nebi unfollow`"}

    @command("unfollow")
    async def unfollow(self, a: Args) -> dict[str, Any]:
        c = self.client()
        self.motion.new_epoch()
        try:
            await c.tracking.unlock_target()
        except NebiError:  # nothing locked: switching to manual below is what matters
            logger.debug("unlock_target failed", exc_info=True)
        r = await c.motion.start_manual_camera_mode()
        neutral(c)
        return {"following": None, **r, "summary": "stopped following; manual mode"}
