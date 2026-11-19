"""Locomotion, 50 Hz virtual joystick, posture, stunts and scheduler missions."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from nebi.domains._base import DomainController
from nebi.domains._mission import request_mission
from nebi.exceptions import NebiError
from nebi.latch import load_latch, save_latch
from nebi.types import (
    CameraSubMode,
    JoystickState,
    MissionId,
    PetSubMode,
    PostureCmd,
    RobotPostureState,
    RobotTelemetrySnapshot,
    SpeedGear,
    StuntDirection,
)

if TYPE_CHECKING:
    from nebi.transport.ucp import UcpEndpointSession
    from nebi.transport.zenoh_ucp import ZenohTopicBus

# Stick axes (lx, ly, rx, ry) are velocity-like commands: if nobody refreshes them with `set_axes` within this
# window they return to neutral, so a crashed/hung controller cannot leave the robot driving. Anything that
# holds a stick must keep calling set_axes at >= ~5 Hz (the cockpit, agent `_hold_axes` and nebi drive do).
logger = logging.getLogger(__name__)

AXIS_TTL_S = 0.5
# `stand()` only trusts a posture report this recent (otherwise the latch logic could fold a standing robot).
POSTURE_FRESH_S = 1.5

EGO_MODE_NEEDS_STANDING = "EGO_MODE_NEEDS_STANDING"


class MotionController(DomainController):
    """High-level async controller for Nebi locomotion, stunts and missions."""

    def __init__(
        self,
        bus: ZenohTopicBus,
        get_ucp_session: Callable[[], UcpEndpointSession | None] | None = None,
        get_telemetry: Callable[[], RobotTelemetrySnapshot] | None = None,
    ) -> None:
        super().__init__(bus)
        self._get_ucp = get_ucp_session
        self._get_telemetry = get_telemetry
        self.joystick = JoystickState()
        self._stream_task: asyncio.Task[None] | None = None
        self._pulse_ticks: int = 0
        self._pulse_override: JoystickState | None = None
        self._pulse_lock: asyncio.Lock = asyncio.Lock()
        self._tx_seq: int = 0
        # `bit 59` (`stand_up_fold`) is a LATCHED switch, not a pushbutton (verified on an LZ110, V26.10.28.04):
        #   - the first received frame latches the baseline (no action)
        #   - `0 -> 1` edge  => chassis disabled => Fold (Braking -> SoftLand -> Folding -> Done/locked Disabled)
        #   - `1 -> 0` edge  => chassis enabled  => Stand Up (cancels a fold in progress: "arrest fold cleared")
        # So Fold = raise bit 59 and KEEP it raised; Stand = drop it (or pulse 0->1->0 when it is already 0,
        # which is a no-op disable followed by enable on a folded robot). Every outgoing frame must carry the
        # latched value, otherwise an unrelated frame with bit 59 = 0 stands the robot back up.
        #   - `bit 58` (`estop`) is edge-toggled by the robot
        self._estop_bit: bool = False
        self._baseline_sent: bool = False
        # The robot remembers the last bit-59 value it received ACROSS client sessions, so a fresh
        # session must not start from 0 blindly: resetting the latch to 0 while the robot was folded with
        # bit 59 = 1 made it stand up on reconnect (observed 2026-11-16). Persist + reconcile instead.
        self.latch_key: str | None = None      # set by the client: robot host (None = simulator/loopback)
        self.joystick.stand_up_fold = False
        self._latch_reconciled = False
        self._axes_ts = 0.0  # monotonic time sticks were last set by a caller

    def reset_session_state(self) -> None:
        """Reset per-session state when establishing a new UCP session (keeps the bit-59 latch, height, gear)."""
        self._estop_bit = False
        self._baseline_sent = False
        self._pulse_ticks = 0
        self._pulse_override = None
        self.joystick.lx = self.joystick.ly = self.joystick.rx = self.joystick.ry = 0.0  # never resume old motion
        self.joystick.posture = PostureCmd.HOLD
        self.joystick.stand_up_fold = load_latch(self.latch_key)
        self._latch_reconciled = False
        self.joystick.estop = False

    def neutral(self) -> int:
        """Centre all sticks (lx, ly, rx, ry, roll). Updates the latched state even while disconnected, so nothing
        resumes after a reconnect; sends immediately when a link is up."""
        self._zero_axes()
        try:
            return self.send_joystick_once()
        except Exception:  # offline / bus closed: the latched state is what matters
            return 0

    def _zero_axes(self) -> None:
        """Centre every motion axis (both sticks and body roll)."""
        j = self.joystick
        j.lx = j.ly = j.rx = j.ry = j.roll = 0.0

    def _expire_axes(self) -> None:
        j = self.joystick
        if (j.lx or j.ly or j.rx or j.ry) and time.monotonic() - self._axes_ts > AXIS_TTL_S:
            j.lx = j.ly = j.rx = j.ry = 0.0

    def _reconcile_latch(self) -> None:
        """Once per session, on the first FRESH posture report, align the latch with the real posture
        (1 if folded, else 0). Any edge this causes points toward the state the robot is already in."""
        if self._latch_reconciled or self._get_telemetry is None:
            return
        tel = self._get_telemetry()
        ts = getattr(tel, "robot_state_ts", 0.0)
        if not ts or time.monotonic() - ts > 1.0:
            return
        want = tel.robot_state == int(RobotPostureState.FOLDED)
        if tel.robot_state in (int(RobotPostureState.FOLDED), int(RobotPostureState.BALANCING)):
            self.joystick.stand_up_fold = want
            save_latch(self.latch_key, want)
            self._latch_reconciled = True

    @property
    def state(self) -> JoystickState:
        return self.joystick

    def send_joystick_once(self, state: JoystickState | None = None) -> int:
        if not self._latch_reconciled:
            self._reconcile_latch()
        self._expire_axes()
        st = state if state is not None else self.joystick
        # Latched/edge bits always come from the controller, never from a caller-built state: a custom state
        # with stand_up_fold=0 on a folded robot is a 1 -> 0 edge = stand up.
        st.stand_up_fold = self.joystick.stand_up_fold
        st.estop = self._estop_bit
        ucp = self._get_ucp() if self._get_ucp is not None else None
        if ucp is not None:
            self._tx_seq = (self._tx_seq + 1) & 0xFFFFFFFF
            ucp.send_joystick_word(st.pack_wire())
            self._baseline_sent = True
            return self._tx_seq
        if not self._bus.is_open:
            return 0
        return self._bus.publish("/cmd/rc/joystick", joystick=st.pack_wire())

    def set_axes(
        self,
        *,
        lx: float | None = None,
        ly: float | None = None,
        rx: float | None = None,
        ry: float | None = None,
        roll: float | None = None,
        height: float | None = None,
        roll_mode: bool | None = None,
        gamepad_source: bool | None = None,
    ) -> int:
        if roll_mode is not None:
            self.joystick.roll_mode = roll_mode
        if any(v is not None for v in (lx, ly, rx, ry)):
            self._axes_ts = time.monotonic()
        if lx is not None:
            if self.joystick.roll_mode and roll is None:
                self.joystick.lx = 0.0
                self.joystick.roll = lx
            else:
                self.joystick.lx = lx
        if ly is not None:
            self.joystick.ly = ly
        if rx is not None:
            self.joystick.rx = rx
        if ry is not None:
            self.joystick.ry = ry
        if roll is not None:
            self.joystick.roll = roll
        if height is not None:
            self.joystick.height = height
        if gamepad_source is not None:
            self.joystick.gamepad_source = gamepad_source
        return self.send_joystick_once()

    async def start_joystick_stream(self, hz: float = 50.0) -> None:
        """Continuously publish `/cmd/rc/joystick` at `hz` (matches the official app's 20 ms timer)."""
        if self._stream_task is not None and not self._stream_task.done():
            return
        period = 1.0 / max(5.0, min(hz, 100.0))

        async def _loop() -> None:
            last_err = ""
            try:
                while True:
                    try:
                        ucp = self._get_ucp() if self._get_ucp is not None else None
                        if ucp is not None or self._bus.is_open:
                            if self._pulse_ticks > 0 and self._pulse_override is not None:
                                self.send_joystick_once(self._pulse_override)
                                self._pulse_ticks -= 1
                                if self._pulse_ticks <= 0:
                                    self._pulse_override = None
                            else:
                                self.send_joystick_once(self.joystick)
                        last_err = ""
                    except Exception as exc:  # a send failure must not silently kill the stream for good
                        if repr(exc) != last_err:  # log once per distinct error, not 50x per second
                            logger.warning("joystick stream send failed (continuing): %r", exc)
                            last_err = repr(exc)
                    await asyncio.sleep(period)
            except asyncio.CancelledError:
                pass

        self._stream_task = asyncio.create_task(_loop())

    async def stop_joystick_stream(self) -> None:
        if self._stream_task is not None:
            self._stream_task.cancel()
            try:
                await self._stream_task
            except asyncio.CancelledError:
                pass
            self._stream_task = None

    def _trigger_pulse(self, **overrides: Any) -> int:
        st = JoystickState(
            lx=self.joystick.lx,
            ly=self.joystick.ly,
            rx=self.joystick.rx,
            ry=self.joystick.ry,
            roll=self.joystick.roll,
            height=self.joystick.height,
            speed_gear=self.joystick.speed_gear,
            estop=self._estop_bit,
            stand_up_fold=self.joystick.stand_up_fold,
            posture=self.joystick.posture,
            roll_mode=self.joystick.roll_mode,
            gamepad_source=self.joystick.gamepad_source,
        )
        for k, v in overrides.items():
            setattr(st, k, v)
        self._pulse_override = st
        self._pulse_ticks = 5
        return self.send_joystick_once(st)

    async def _ensure_baseline_sent(self) -> None:
        """Ensure the robot has latched an initial joystick baseline (its first frame is never acted on)."""
        if not self._baseline_sent:
            self.send_joystick_once(self.joystick)
            await asyncio.sleep(0.025)
            self.send_joystick_once(self.joystick)
            await asyncio.sleep(0.025)

    async def _set_stand_fold_bit(self, value: bool, posture: PostureCmd, frames: int = 6) -> int:
        """Latch `bit 59` to `value` (and the posture hint) and push a few frames immediately."""
        async with self._pulse_lock:
            self._pulse_override = None
            self._pulse_ticks = 0
            self.joystick.stand_up_fold = value
            self.joystick.posture = posture
            save_latch(self.latch_key, value)
            self._latch_reconciled = True
            seq = 0
            for _ in range(frames):
                seq = self.send_joystick_once(self.joystick)
                await asyncio.sleep(0.020)
            return seq

    def _posture_fresh(self, tel: RobotTelemetrySnapshot) -> bool:
        ts = getattr(tel, "robot_state_ts", 0.0)
        return bool(ts) and time.monotonic() - ts <= POSTURE_FRESH_S

    async def stand(self, *, force: bool = False) -> int:
        """Stand up: drop the latched `bit 59` (1 -> 0 edge = chassis enable)."""
        if not force and self._get_telemetry is not None:
            tel = self._get_telemetry()
            if not self._posture_fresh(tel) and not self.joystick.stand_up_fold:
                # Latch says "not folded" but we have no current posture: the 0 -> 1 -> 0 pulse below would FOLD a
                # robot that is actually standing. Refuse until a posture report arrives.
                raise NebiError(
                    "POSTURE_UNKNOWN",
                    "No recent posture report from the robot, so standing up is not safe to decide.",
                    stage="connected",
                    recoverable=True,
                    hint="Wait a second for telemetry and retry, or pass force=True if you are sure it is folded.",
                )
            if tel.charging and tel.is_locked:
                raise NebiError(
                    "ROBOT_CHARGING_LOCKED",
                    "Robot locomotion is locked while the USB-C charging cable is plugged in.",
                    stage="connected",
                    recoverable=True,
                    hint="Unplug the USB-C charging cable from Nebi before commanding Stand Up.",
                )
            if tel.robot_state in (
                int(RobotPostureState.MOTOR_RESET),
                int(RobotPostureState.JOINT_POS_CTRL),
                int(RobotPostureState.BALANCING),
            ) and not self.joystick.stand_up_fold and self._posture_fresh(tel):
                return self.send_joystick_once()
        self.joystick.lx = self.joystick.ly = self.joystick.rx = self.joystick.ry = 0.0

        async def _sequence() -> int:
            if not self.joystick.stand_up_fold:
                # bit is already 0 (e.g. folded by the remote or at boot): raise it briefly first so the
                # robot sees a 1 -> 0 edge. On a folded robot the rising edge is a no-op.
                await self._set_stand_fold_bit(True, PostureCmd.STAND, frames=5)
            return await self._set_stand_fold_bit(False, PostureCmd.HOLD)

        # Shielded: cancelling between the two steps would persist latch=1 and fold the robot.
        return await asyncio.shield(asyncio.ensure_future(_sequence()))

    async def sit(self, *, force: bool = False) -> int:
        """Fold legs: raise and KEEP `bit 59` (0 -> 1 edge = chassis disable -> fold sequence)."""
        self.joystick.lx = self.joystick.ly = self.joystick.rx = self.joystick.ry = 0.0
        if self.joystick.stand_up_fold and not force:
            return self.send_joystick_once()
        return await self._set_stand_fold_bit(True, PostureCmd.FOLD)

    async def fold(self, *, force: bool = False) -> int:
        return await self.sit(force=force)

    async def emergency_stop(self) -> int:
        """Immediate emergency stop (`bit 58` `estop` edge-toggle + zero every axis)."""
        self._zero_axes()
        await self._ensure_baseline_sent()
        self._estop_bit = not self._estop_bit
        self.joystick.estop = self._estop_bit
        seq = self._trigger_pulse(lx=0.0, ly=0.0, rx=0.0, ry=0.0, roll=0.0)
        for _ in range(3):
            await asyncio.sleep(0.015)
            seq = self.send_joystick_once(self._pulse_override or self.joystick)
        return seq

    async def jump(self, height: float | None = None) -> int:
        """Trigger a jump (`bit 50` `jump_trigger`). The firmware jump height is fixed; `height` is accepted for API
        compatibility and ignored."""
        return self._trigger_pulse(jump_trigger=True)

    async def flip(self, direction: StuntDirection | int = StuntDirection.BACK_FLIP) -> int:
        """Trigger 4-way acrobatic flip (`FRONT_FLIP=1`, `BACK_FLIP=2`, `LEFT_FLIP=3`, `RIGHT_FLIP=4`)."""
        return self._trigger_pulse(stunt=StuntDirection(int(direction)))

    async def turn_around(self) -> int:
        """Trigger automatic 180-degree quick turn (`turn_around = True`)."""
        return self._trigger_pulse(turn_around=True)

    async def set_speed_gear(self, gear: SpeedGear | int) -> int:
        """Set speed / sensitivity gear (`SOFT=1`, `STANDARD=2`, `SPORT=3`)."""
        self.joystick.speed_gear = SpeedGear(int(gear))
        return self.send_joystick_once()

    async def set_mission(
        self,
        mission_id: MissionId | int,
        sub_mode: int = 0,
        mode_params: bytes = b"",
        timeout: float = 2.0,
    ) -> dict[str, Any]:
        """Switch top-level `Scheduler` mission (`1=Camera, 2=Game, 3=Pet, 4=SelfCalibration, 10=Qa, 11=RealRl`)."""
        return await request_mission(self._bus, int(mission_id), int(sub_mode), mode_params=mode_params, timeout=timeout)

    async def start_manual_camera_mode(self) -> dict[str, Any]:
        return await self.set_mission(MissionId.CAMERA, int(CameraSubMode.MANUAL))

    async def start_ego_mode(self, *, force: bool = False, confirm_timeout: float = 2.0) -> dict[str, Any]:
        """Enter pet / ego interaction mode (`MissionId.CAMERA`, `CameraSubMode.EGO_INTERACTION` = 6).

        The firmware accepts the request but only switches while the robot is standing (balancing) and still, so a
        folded robot is refused up front with `EGO_MODE_NEEDS_STANDING` (`force=True` skips the check). After the
        request the scheduler status is watched for `confirm_timeout` s: the result carries `entered` (True/False,
        None = no scheduler status received) and a `hint` when the robot did not switch."""
        warnings: list[str] = []
        tel = self._get_telemetry() if self._get_telemetry is not None else None
        if tel is not None and not force:
            fresh = self._posture_fresh(tel)
            if fresh and tel.robot_state != int(RobotPostureState.BALANCING):
                raise NebiError(
                    EGO_MODE_NEEDS_STANDING,
                    f"Pet mode only starts while the robot is standing; it is {tel.posture_name}.",
                    stage="connected",
                    recoverable=True,
                    hint="Stand the robot first (Stand Up), wait until it is balancing and still, then retry.",
                    details={"posture": tel.posture_name, "robot_state": tel.robot_state},
                )
            if not fresh:
                warnings.append("No recent posture report: pet mode only starts while the robot is standing.")
            elif abs(tel.velocity_mps) > 0.05:
                warnings.append("The robot is moving: pet mode only starts once it stands still.")
            if tel.charging and tel.is_locked:
                warnings.append("Locomotion is locked while charging: pet mode may not start until the cable is unplugged.")
        ack = await self.set_mission(MissionId.CAMERA, int(CameraSubMode.EGO_INTERACTION))
        entered = await self._wait_for_mode(int(MissionId.CAMERA), int(CameraSubMode.EGO_INTERACTION), confirm_timeout)
        res: dict[str, Any] = {**ack, "entered": entered, "warnings": warnings}
        if tel is not None:
            res["mission"] = f"{tel.mission_name}/{tel.sub_mode_name}"
            res["posture"] = tel.posture_name
        if entered is False:
            res["hint"] = ("The robot acknowledged the request but stayed in "
                           f"{res.get('mission', 'its current mode')}. Pet mode only starts while standing and still: "
                           "stand the robot, stop driving, and retry.")
        return res

    async def _wait_for_mode(self, mission_id: int, sub_mode: int, timeout: float) -> bool | None:
        """Poll telemetry until the scheduler reports `mission_id/sub_mode` (True), the timeout passes (False), or
        no scheduler status was ever received (None)."""
        if self._get_telemetry is None or timeout <= 0:
            return None
        deadline = time.monotonic() + timeout
        while True:
            tel = self._get_telemetry()
            if tel.mission_id == mission_id and tel.sub_mode == sub_mode and "/status/scheduler/status" in tel.topic_ts:
                return True
            if time.monotonic() >= deadline:
                return False if "/status/scheduler/status" in tel.topic_ts else None
            await asyncio.sleep(0.1)

    async def start_companion_mode(self) -> dict[str, Any]:
        return await self.set_mission(MissionId.PET, int(PetSubMode.COMPANION))

    async def return_to_dock(self) -> dict[str, Any]:
        """Trigger autonomous ArUco + IR docking & charging (`MissionId.PET`, `PetSubMode.DOCK`)."""
        return await self.set_mission(MissionId.PET, int(PetSubMode.DOCK))

    async def undock(self) -> dict[str, Any]:
        """Exit charging dock (`MissionId.PET`, `PetSubMode.UNDOCK`)."""
        return await self.set_mission(MissionId.PET, int(PetSubMode.UNDOCK))

    async def start_self_calibration(self) -> dict[str, Any]:
        return await self.set_mission(MissionId.SELF_CALIBRATION, 0)

    async def calibrate_joystick(self, action: int = 1, receiver_type: int = 0, receiver_index: int = 0) -> int:
        return self._bus.publish(
            "/cmd/rc/joystick_calib_req",
            action=action,
            receiver_type=receiver_type,
            receiver_index=receiver_index,
        )

    async def set_real_rl_control(
        self,
        inference_mode: int = 1,
        rollout_recording: bool = False,
        duration_ms: int = 10000,
        exploration_scale: float = 0.0,
        timeout: float = 2.0,
    ) -> dict[str, Any]:
        return await self._request_ints(
            "/cmd/real_rl/control_request",
            "/cmd/real_rl/control_ack",
            ("result_code", "requested_inference_mode"),
            timeout=timeout,
            version=1,
            inference_mode=inference_mode,
            rollout_recording_requested=1 if rollout_recording else 0,
            recording_duration_ms=duration_ms,
            client_session_id=1,
            exploration_scale=exploration_scale,
        )
