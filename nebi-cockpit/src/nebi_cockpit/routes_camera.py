"""Camera control and live-video REST routes (snapshot, MJPEG fallback)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Response
from fastapi.responses import StreamingResponse

from nebi.domains.camera import ZOOM_MAX_X, ZOOM_MIN_X
from nebi_cockpit.deps import ClientDep, ManagerDep
from nebi_cockpit.schemas import (
    EisBody,
    ProExposureBody,
    ProfileBody,
    StreamConfigBody,
    ToggleBody,
    TrajectoryPlayBody,
    ZoomBody,
)

video = APIRouter(prefix="/api/video", tags=["video"])
camera = APIRouter(prefix="/api/camera", tags=["camera"])

# --- Live video ---


@video.get("/snapshot.jpg")
async def api_video_snapshot(manager: ManagerDep) -> Response:
    jpg = manager.video_hub.latest_jpeg
    if jpg is None:
        raise HTTPException(status_code=503, detail="No video frame available")
    return Response(content=jpg, media_type="image/jpeg")


@video.get("/mjpeg")
async def api_video_mjpeg(manager: ManagerDep) -> StreamingResponse:
    manager.request_keyframe()
    return StreamingResponse(
        manager.video_hub.mjpeg_generator(),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


# --- Camera ---


@camera.post("/photo")
async def api_photo(client: ClientDep) -> dict[str, Any]:
    """Still photos are unsupported by the robot firmware: `take_photo()` raises -> 501 with a hint."""
    return {"ok": True, "seq": await client.camera.take_photo()}


@camera.post("/record/start")
async def api_rec_start(client: ClientDep) -> dict[str, Any]:
    return await client.camera.start_recording()


@camera.post("/record/stop")
async def api_rec_stop(client: ClientDep) -> dict[str, Any]:
    return await client.camera.stop_recording()


@camera.post("/highlight")
async def api_highlight(client: ClientDep) -> dict[str, Any]:
    return {"ok": True, "seq": await client.camera.mark_highlight()}


@camera.post("/zoom")
async def api_zoom(body: ZoomBody, client: ClientDep) -> dict[str, Any]:
    zoom_x = max(ZOOM_MIN_X, min(ZOOM_MAX_X, body.zoom_x))
    return {"ok": True, "seq": await client.camera.set_zoom(zoom_x), "zoom_x": zoom_x}


@camera.post("/eis")
async def api_eis(body: EisBody, client: ClientDep) -> dict[str, Any]:
    return {"ok": True, "seq": await client.camera.set_eis(body.enabled, body.level)}


@camera.post("/horizon_lock")
async def api_horizon(body: ToggleBody, client: ClientDep) -> dict[str, Any]:
    return {"ok": True, "seq": await client.camera.set_horizon_lock(body.enabled)}


@camera.post("/profile")
async def api_profile(body: ProfileBody, client: ClientDep) -> dict[str, Any]:
    return {"ok": True, "seq": await client.camera.set_profile(body.profile_id)}


@camera.post("/pro_exposure")
async def api_pro_exp(body: ProExposureBody, client: ClientDep) -> dict[str, Any]:
    return await client.camera.set_pro_exposure(
        pro_mode=body.pro_mode,
        shutter_ns=body.shutter_ns,
        iso=body.iso,
        wb_kelvin=body.wb_kelvin,
        ev_compensation=body.ev_compensation,
    )


@camera.post("/pre_record")
async def api_pre_rec(body: ToggleBody, client: ClientDep) -> dict[str, Any]:
    return await client.camera.set_pre_record(body.enabled)


@camera.post("/stream")
async def api_stream_cfg(body: StreamConfigBody, client: ClientDep) -> dict[str, Any]:
    cam = client.camera
    if body.streaming is True:
        await cam.start_live_stream()
    elif body.streaming is False:
        await cam.stop_live_stream()
    if body.bitrate_kbps is not None:
        cam.set_stream_bitrate(body.bitrate_kbps)
    if body.fps is not None:
        cam.set_stream_fps(body.fps)
    if body.request_idr:
        cam.request_keyframe()
    return {"ok": True}


@camera.get("/trajectories")
async def api_trajectories(client: ClientDep) -> dict[str, Any]:
    return {"trajectories": await client.camera.list_trajectory_replays()}


@camera.post("/trajectories/play")
async def api_trajectory_play(body: TrajectoryPlayBody, client: ClientDep) -> dict[str, Any]:
    return {"ok": True, "seq": await client.camera.play_trajectory_replay(body.trajectory_id)}


@camera.post("/trajectories/stop")
async def api_trajectory_stop(client: ClientDep) -> dict[str, Any]:
    return {"ok": True, "seq": await client.camera.stop_trajectory_replay()}


router = APIRouter()
router.include_router(video)
router.include_router(camera)
