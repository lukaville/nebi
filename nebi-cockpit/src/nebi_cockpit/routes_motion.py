"""Motion, autonomy, AI tracking/follow and games routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from nebi_cockpit.deps import ClientDep
from nebi_cockpit.schemas import (
    FlipBody,
    FollowModeBody,
    GameActionBody,
    GameInputBody,
    GameStartBody,
    IntValueBody,
    JumpBody,
    LockPointBody,
    LockTargetBody,
    MissionBody,
    SpeedGearBody,
)

motion = APIRouter(prefix="/api/motion", tags=["motion"])
tracking = APIRouter(prefix="/api/tracking", tags=["tracking"])
games = APIRouter(prefix="/api/games", tags=["games"])

# --- Motion & autonomy ---


@motion.post("/stand")
async def api_stand(client: ClientDep) -> dict[str, Any]:
    return {"ok": True, "seq": await client.motion.stand()}


@motion.post("/sit")
async def api_sit(client: ClientDep) -> dict[str, Any]:
    return {"ok": True, "seq": await client.motion.sit()}


@motion.post("/estop")
async def api_estop(client: ClientDep) -> dict[str, Any]:
    return {"ok": True, "seq": await client.motion.emergency_stop()}


@motion.post("/jump")
async def api_jump(body: JumpBody, client: ClientDep) -> dict[str, Any]:
    return {"ok": True, "seq": await client.motion.jump(height=body.height)}


@motion.post("/flip")
async def api_flip(body: FlipBody, client: ClientDep) -> dict[str, Any]:
    return {"ok": True, "seq": await client.motion.flip(direction=body.direction)}


@motion.post("/turn_around")
async def api_turn_around(client: ClientDep) -> dict[str, Any]:
    return {"ok": True, "seq": await client.motion.turn_around()}


@motion.post("/speed_gear")
async def api_speed_gear(body: SpeedGearBody, client: ClientDep) -> dict[str, Any]:
    return {"ok": True, "seq": await client.motion.set_speed_gear(body.gear)}


@motion.post("/mission")
async def api_set_mission(body: MissionBody, client: ClientDep) -> dict[str, Any]:
    return await client.motion.set_mission(body.mission_id, body.sub_mode)


@motion.post("/dock")
async def api_dock(client: ClientDep) -> dict[str, Any]:
    return await client.motion.return_to_dock()


@motion.post("/undock")
async def api_undock(client: ClientDep) -> dict[str, Any]:
    return await client.motion.undock()


@motion.post("/ego")
async def api_ego(client: ClientDep) -> dict[str, Any]:
    return await client.motion.start_ego_mode()


# --- AI vision tracking & follow ---


@tracking.post("/lock_target")
async def api_lock_target(body: LockTargetBody, client: ClientDep) -> dict[str, Any]:
    return await client.tracking.lock_target(body.target_id)


@tracking.post("/lock_point")
async def api_lock_point(body: LockPointBody, client: ClientDep) -> dict[str, Any]:
    return await client.tracking.lock_point(body.x, body.y)


@tracking.post("/unlock")
async def api_unlock(client: ClientDep) -> dict[str, Any]:
    return await client.tracking.unlock_target()


@tracking.post("/follow_mode")
async def api_follow_mode(body: FollowModeBody, client: ClientDep) -> dict[str, Any]:
    return await client.tracking.set_follow_mode(
        sub_mode=body.sub_mode,
        distance_m=body.distance_m,
        orbit_speed=body.orbit_speed,
    )


@tracking.post("/target_type")
async def api_target_type(body: IntValueBody, client: ClientDep) -> dict[str, Any]:
    return await client.tracking.switch_target_type(body.value)


@tracking.post("/pnl_mode")
async def api_pnl_mode(body: IntValueBody, client: ClientDep) -> dict[str, Any]:
    try:
        return await client.tracking.set_pnl_mode(body.value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@tracking.get("/pnl_mode")
async def api_get_pnl_mode(client: ClientDep) -> dict[str, Any]:
    return await client.tracking.get_pnl_mode()


# --- Games ---


@games.post("/start")
async def api_game_start(body: GameStartBody, client: ClientDep) -> dict[str, Any]:
    return await client.games.start_game(body.game_type)


@games.post("/action")
async def api_game_action(body: GameActionBody, client: ClientDep) -> dict[str, Any]:
    g = client.games
    if body.action == "pause":
        return await g.pause_game()
    if body.action == "resume":
        return await g.resume_game()
    if body.action == "restart":
        return await g.restart_game()
    return await g.stop_game()


@games.post("/input")
async def api_game_input(body: GameInputBody, client: ClientDep) -> dict[str, Any]:
    seq = client.games.send_player_input(
        pitch_deg=body.pitch_deg,
        roll_deg=body.roll_deg,
        yaw_deg=body.yaw_deg,
        jump=body.jump,
        action_pressed=body.action_pressed,
    )
    return {"ok": True, "seq": seq}


router = APIRouter()
for _sub in (motion, tracking, games):
    router.include_router(_sub)
