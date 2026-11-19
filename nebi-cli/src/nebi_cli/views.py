"""JSON views of robot telemetry used in replies: the compact post-action `state` and target dicts."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from nebi.types import SpeedGear
from nebi_cli.errors import STANDING
from nebi_cli.util import where_in_image

if TYPE_CHECKING:
    from nebi.client import AsyncNebiClient
    from nebi_cockpit.app import NebiServerManager


def compact_state(manager: NebiServerManager, client: AsyncNebiClient | None) -> dict[str, Any]:
    if client is None:
        conn = manager.connection_summary()
        return {"connected": False, "stage": conn.get("stage"), "flow": conn.get("flow")}
    t = client.telemetry
    lnk = t.link
    joy = client.motion.joystick
    st: dict[str, Any] = {
        "connected": client.connected,
        "flow": client.flow,
        "posture": t.posture_name,
        "standing": t.posture_name in STANDING,
        "battery": t.battery_soc,
        "charging": t.charging,
        "docked": t.docked,
        "mission": t.mission_name,
        "mode": t.sub_mode_name,
        "locomotion_locked": t.is_locked,
        "speed_mps": t.velocity_mps,
        "height_cm": t.height_cm,
        "obstacle_cm": t.obstacle_cm,
        "gimbal_deg": t.gimbal_pitch_deg,
        "gear": {SpeedGear.SOFT: "soft", SpeedGear.SPORT: "sport"}.get(joy.speed_gear, "standard"),
        "recording": t.recording,
        "tracking": t.tracking_active,
        "locked_target": t.locked_target_id,
        "targets": len(t.targets),
        "faults": [a.code for a in t.hms_alerts],
    }
    if t.active_game:
        st["game"] = {"id": t.active_game, "state": t.game_state, "score": t.game_score}
    if lnk.degraded or not client.connected:
        st["link"] = {"stage": lnk.stage, "degraded": lnk.degraded, "rx_silence_ms": lnk.rx_silence_ms}
    return st


def target_confidence(tg: Any) -> float:
    """Sort key: confidence is None on real-robot targets (firmware reports none)."""
    c = getattr(tg, "confidence", None)
    return float(c) if c is not None else 0.0


def target_dict(tg: Any) -> dict[str, Any]:
    conf = getattr(tg, "confidence", None)
    return {
        "id": tg.target_id,
        "label": tg.label,
        "confidence": round(conf, 2) if conf is not None else None,
        "x": round(tg.cx, 3),
        "y": round(tg.cy, 3),
        "w": round(tg.width, 3),
        "h": round(tg.height, 3),
        "distance_m": round(tg.distance_m, 2),
        "distance_estimated": bool(getattr(tg, "distance_estimated", True)),
        "locked": tg.locked,
        "where": where_in_image(tg.cx),
    }
