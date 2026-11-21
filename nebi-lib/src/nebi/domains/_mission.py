"""Shared `/cmd/mission/request` helper (used by motion, tracking and games)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from nebi.domains._base import int_fields
from nebi.exceptions import NebiError

if TYPE_CHECKING:
    from nebi.transport.zenoh_ucp import ZenohTopicBus


async def request_mission(
    bus: ZenohTopicBus,
    mission_id: int,
    sub_mode: int = 0,
    *,
    mode_params: bytes = b"",
    timeout: float = 2.0,
) -> dict[str, Any]:
    """Switch the scheduler mission; returns the ack fields (result_code 0 = accepted)."""
    kw: dict[str, Any] = {"version": 1, "mission_id": int(mission_id), "sub_mode": int(sub_mode)}
    if mode_params:
        kw["mode_params"] = mode_params
    rsp = await bus.request("/cmd/mission/request", "/cmd/mission/request_ack", timeout=timeout, **kw)
    ack: dict[str, Any] = int_fields(rsp.data, "result_code", "current_mission_id", "current_sub_mode")
    if rsp.data.HasField("pitch_handoff"):
        ack["pitch_handoff_rad"] = float(rsp.data.pitch_handoff.held_pitch_rad)
    return ack


def require_accepted(ack: dict[str, Any], what: str) -> dict[str, Any]:
    """Raise `MISSION_REJECTED` unless the mission ack's result_code is 0."""
    if ack.get("result_code", 0) != 0:
        raise NebiError(
            "MISSION_REJECTED",
            f"Robot rejected the mission switch for {what} (result_code {ack['result_code']}).",
            stage="connected",
            recoverable=True,
            hint="Check posture/charging state and active faults, then retry.",
            details=ack,
        )
    return ack
