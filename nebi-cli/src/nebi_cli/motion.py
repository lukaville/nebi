"""Motion gate: the motion lock, motion epochs and the bounded joystick hold every moving command uses.

  * `lock` serialises commands that drive the joystick or the firmware motion player.
  * `epoch` is bumped by every new motion and by `stop`; a running hold notices the change and returns
    "stopped" (that is how `nebi stop` from another CLI process preempts a drive).
  * Every hold ends with all sticks at neutral (`neutral()`), whatever happens.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from nebi_cli.errors import STANDING, AgentError
from nebi_cli.spec_model import EXIT_FAILED, EXIT_SAFETY
from nebi_cli.util import param_values

if TYPE_CHECKING:
    from nebi.client import AsyncNebiClient

logger = logging.getLogger(__name__)

# Nominal kinematics used for open-loop estimates (overridden by robot params when readable).
DEFAULT_MAX_SPEED_MPS = 2.0  # md_function.max_speed at full stick, standard gear
DEFAULT_MAX_YAW_DPS = 90.0  # md_function.max_yaw_rate at full stick, standard gear
HOLD_TICK_S = 0.04  # joystick re-send period while holding axes (well inside the motion domain's AXIS_TTL)


def done_after(seconds: float) -> Callable[[float], str | None]:
    """`until` callback for MotionGate.hold_axes: stop with "done" after a fixed time."""
    return lambda el: "done" if el >= seconds else None


def neutral(c: AsyncNebiClient) -> None:
    """Centre all sticks (latched state too, so nothing resumes after a reconnect). Never raises."""
    try:
        c.motion.neutral()
    except Exception:  # called from finally blocks; a dead link must not mask the original outcome
        logger.debug("neutral() failed", exc_info=True)


def require_mobile(c: AsyncNebiClient, force: bool, warnings: list[str]) -> None:
    """Refuse locomotion while charging-locked or (unless --force) not standing; warn about autonomous modes."""
    t = c.telemetry
    if t.charging and t.is_locked:
        raise AgentError(
            "ROBOT_CHARGING_LOCKED",
            "Locomotion is locked while the USB-C charging cable is plugged in.",
            hint="Unplug the charging cable (or `nebi undock` if on the dock), then `nebi stand`.",
            exit_code=EXIT_SAFETY,
        )
    if t.posture_name not in STANDING and not force:
        raise AgentError(
            "NOT_STANDING",
            f"Robot is {t.posture_name}, it must be standing/balancing to move.",
            hint="Run `nebi stand` first.",
            exit_code=EXIT_FAILED,
        )
    if t.sub_mode_name not in ("MANUAL", "EGO_INTERACTION") or t.mission_name not in ("CAMERA", "IDLE"):
        warnings.append(
            f"Robot is in {t.mission_name}/{t.sub_mode_name}; manual driving may be overridden. "
            "Run `nebi mode manual` (or `nebi unfollow`) for full manual control."
        )


class MotionGate:
    """Motion lock + epochs + kinematics cache, shared by every command that moves the robot."""

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self._epoch = 0
        self._kin_cache: dict[int, tuple[float, float]] = {}

    @property
    def epoch(self) -> int:
        return self._epoch

    def new_epoch(self) -> int:
        """Start a new motion (or stop): any hold running under an older epoch ends with "stopped"."""
        self._epoch += 1
        return self._epoch

    def is_current(self, epoch: int) -> bool:
        return self._epoch == epoch

    async def hold_axes(
        self,
        c: AsyncNebiClient,
        axes: dict[str, float],
        *,
        epoch: int,
        until: Callable[[float], str | None],
        max_s: float,
    ) -> tuple[str, float]:
        """Hold joystick axes (lx/ly/rx/ry/roll/height) until `until(elapsed)` returns a stop reason, a newer
        epoch, or max_s. Returns (reason, elapsed); the sticks are neutral afterwards in every case."""
        t0 = time.monotonic()
        reason = "timeout"
        try:
            while True:
                el = time.monotonic() - t0
                if self._epoch != epoch:
                    reason = "stopped"
                    break
                if not c.connected:
                    reason = "link_lost"
                    break
                r = until(el)
                if r:
                    reason = r
                    break
                if el >= max_s:
                    reason = "timeout"
                    break
                c.motion.set_axes(lx=axes.get("lx"), ly=axes.get("ly"), rx=axes.get("rx"), ry=axes.get("ry"),
                                  roll=axes.get("roll"), height=axes.get("height"))
                await asyncio.sleep(HOLD_TICK_S)
        finally:
            neutral(c)
        return reason, time.monotonic() - t0

    async def kinematics(self, c: AsyncNebiClient) -> tuple[float, float]:
        """(max_speed_mps, max_yaw_dps) at full stick, standard gear; read once per client from robot params."""
        key = id(c)
        if key not in self._kin_cache:
            speed, yaw = DEFAULT_MAX_SPEED_MPS, DEFAULT_MAX_YAW_DPS
            try:
                vals = param_values(
                    await asyncio.wait_for(c.system.get_parameters(["md_function.max_speed", "md_function.max_yaw_rate"]), 2.5)
                )
                speed = float(vals.get("md_function.max_speed", speed) or speed)
                yaw = float(vals.get("md_function.max_yaw_rate", yaw) or yaw)
                if yaw < 10:  # stored in rad/s on some firmwares
                    yaw = math.degrees(yaw)
            except Exception:  # params unreadable on some firmwares: nominal values are a fine estimate
                logger.debug("kinematics params unavailable; using defaults", exc_info=True)
            self._kin_cache[key] = (max(0.2, speed), max(10.0, yaw))
        return self._kin_cache[key]

    def forget_kinematics(self, c: AsyncNebiClient | None = None) -> None:
        """Drop cached kinematics (for one client after a gear/param change, or all after a reconnect)."""
        if c is None:
            self._kin_cache.clear()
        else:
            self._kin_cache.pop(id(c), None)
