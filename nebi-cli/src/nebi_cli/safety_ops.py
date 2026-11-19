"""Safety precheck run before every command, top-level and nested (see spec_model.MotionKind).

Rules:
  * body / stunt commands are refused while the robot reports being carried; only `--ignore-carried` overrides
    that (`--force` never does);
  * when the carried state is unknown body moves get a warning, stunts are refused unless `--ignore-carried`.
    The robot link itself has no lift/carry signal, so the state is unknown unless an extension registered a
    CarriedStateProvider (agent/extensions.py);
  * gimbal / express / rest commands are never blocked by the carried check;
  * `drive` / `move` are speed-capped when a person is close ahead (the precheck sets `_speed_cap`, the
    handlers apply it); people come from an extension's PeopleProvider.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, NamedTuple

from nebi_cli import spec as S
from nebi_cli.errors import AgentError
from nebi_cli.util import num, warn_once

if TYPE_CHECKING:
    from nebi_cli.extensions import AgentHooks

logger = logging.getLogger(__name__)

CARRY_CHECKED = ("body", "stunt")
PEOPLE_SPEED_CAP = 0.3  # max stick deflection for drive/move with a person close ahead
PEOPLE_CLOSE_M = 1.2
PEOPLE_AHEAD = ("ahead", "front-left", "front-right")
CARRIED_TIMEOUT_S = 1.0
NO_CARRIED_SOURCE = "no carried-state source (the robot link reports no lift/carry signal; see `nebi status` extensions)"


class SpeedCap(NamedTuple):
    limit: float
    reason: str


class SafetyGate:
    def __init__(self, hooks: AgentHooks) -> None:
        self._hooks = hooks

    async def carried_state(self) -> tuple[bool | None, str]:
        """(True/False, source) from the extension provider, or (None, why) when it cannot be determined."""
        provider = self._hooks.carried
        if provider is None:
            return None, NO_CARRIED_SOURCE
        try:
            return await asyncio.wait_for(provider.carried_state(), CARRIED_TIMEOUT_S)
        except Exception as exc:  # a broken/slow provider means "unknown", never a crash
            logger.debug("carried-state provider failed", exc_info=True)
            return None, f"carried-state source failed ({type(exc).__name__})"

    async def people_now(self, timeout: float = 0.6) -> list[dict[str, Any]]:
        """People/pets from the extension provider ([] when none is registered or it fails)."""
        provider = self._hooks.people
        if provider is None:
            return []
        try:
            return [p for p in await asyncio.wait_for(provider.people(), timeout) if isinstance(p, dict)]
        except Exception:  # nobody known to be around
            logger.debug("people provider failed", exc_info=True)
            return []

    async def precheck(self, command: str, a: dict[str, Any], *, kind: str | None = None) -> None:
        """Raise BEING_CARRIED / CARRIED_UNKNOWN per the rules above; add warnings and `_speed_cap` to `a`."""
        spec = S.COMMANDS_BY_NAME.get(command)
        if kind is None:
            kind = spec.motion_kind if spec else ""
        if kind not in CARRY_CHECKED:
            return
        if command == "move" and not any(float(a.get(k) or 0.0) for k in ("forward", "strafe", "yaw")):
            return  # gimbal-only move
        warnings: list[str] = a.setdefault("_warnings", [])
        ignore = bool(a.get("ignore_carried"))
        carried, why = await self.carried_state()
        if carried:
            if not ignore:
                raise AgentError(
                    "BEING_CARRIED",
                    "Refusing to move: the robot is being carried / held in the air.",
                    hint="Wait until it is put down (`nebi wait --for put_down` when available). Only if a human is holding it on "
                    "purpose, re-run with --ignore-carried (--force does not override this).",
                    exit_code=S.EXIT_SAFETY,
                )
            warn_once(warnings, "Robot reports being carried; moving anyway because of --ignore-carried.")
        elif carried is None:
            if kind == "stunt" and not ignore:
                raise AgentError(
                    "CARRIED_UNKNOWN",
                    f"Refusing `{command}`: cannot tell whether the robot is being held ({why}).",
                    hint="Re-run with --ignore-carried if you can see the robot is on the floor (a carried-state "
                    "source comes from an installed extension; see `nebi status`).",
                    exit_code=S.EXIT_SAFETY,
                )
            warn_once(warnings, f"carried-check unavailable: {why}")
        if command in ("drive", "move"):
            close = [
                p for p in await self.people_now()
                if num(p.get("distance_m"), 9.0) < PEOPLE_CLOSE_M and p.get("direction") in PEOPLE_AHEAD
            ]
            if close:
                p = close[0]
                a["_speed_cap"] = SpeedCap(PEOPLE_SPEED_CAP, f"A {p.get('type', 'person')} is {p.get('distance_m')} m {p.get('direction')}")
