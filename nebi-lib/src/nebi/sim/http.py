"""Embedded HTTP daemons of the simulated robot: Config agent, Media album server and local OTA upload."""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, File, Form, Request, Response, UploadFile
from fastapi.responses import PlainTextResponse

from nebi.http.media_support import SIM_MEDIA_TOKEN, token_matches_firmware

if TYPE_CHECKING:
    from nebi.sim.media import SimMediaAsset
    from nebi.sim.robot import MockNebiRobot


class _NotFound(Exception):
    """404 with the firmware's plain-text body (`file not found: <id>`)."""


def build_http_app(robot: MockNebiRobot) -> FastAPI:
    """Config agent (`:8010`), media server (`:8084`) and local OTA (`:8088`) routes on one app."""
    app = FastAPI(title="MockNebiRobot Local Daemons")
    started = time.monotonic()

    # 1. Config Agent (:8010)
    @app.get("/config/v1/local/health")
    async def cfg_health() -> dict[str, Any]:
        return {"ok": True, "service": "config_sim"}

    @app.get("/config/v1/local/version")
    async def cfg_version() -> dict[str, Any]:
        return {
            "version": "2.0",
            "schema_revision": 4,
            "firmware_version": "v1.4.2-sim",
            "sn": "LZ110SIM2026X5",
        }

    @app.get("/config/v1/local/status")
    async def cfg_status() -> dict[str, Any]:
        return {"status": "clean", "keys": len(robot.config_store)}

    @app.post("/config/v1/local/query")
    async def cfg_query(payload: dict[str, Any]) -> dict[str, Any]:
        keys = payload.get("keys")
        vals = {k: robot.config_store.get(k) for k in keys if k in robot.config_store} if keys else dict(robot.config_store)
        return {"ok": True, "values": vals}

    @app.post("/config/v1/local/commit")
    async def cfg_commit(payload: dict[str, Any]) -> dict[str, Any]:
        updates = payload.get("values", {})
        for k, v in updates.items():
            robot.config_store[k] = v
        return {"ok": True, "committed": list(updates.keys()), "values": dict(robot.config_store)}

    # 2. Media Server (:8084), firmware-faithful: bearer-token check, then `/media/*` JSON shapes.
    @app.middleware("http")
    async def media_bearer_auth(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        if request.url.path.startswith("/media/"):
            scheme, _, token = request.headers.get("authorization", "").partition(" ")
            if scheme.lower() != "bearer" or not (token.strip() == SIM_MEDIA_TOKEN or token_matches_firmware(token.strip())):
                robot.media_auth_failures += 1
                return PlainTextResponse(
                    "AUTH_REQUIRED", status_code=401, headers={"www-authenticate": "Bearer", "cache-control": "no-store"}
                )
        return await call_next(request)

    def _asset(file_id: str) -> SimMediaAsset:
        asset = robot.media_files.get(file_id)
        if asset is None:
            raise _NotFound(f"file not found: {file_id}")
        return asset

    @app.exception_handler(_NotFound)
    async def media_not_found(_request: Request, exc: _NotFound) -> Response:
        return PlainTextResponse(str(exc), status_code=404)

    @app.get("/media/health")
    async def media_health() -> dict[str, Any]:
        return {"status": "ok", "db_size_bytes": 86016, "uptime_secs": int(time.monotonic() - started)}

    @app.get("/media/index_status")
    async def media_idx() -> dict[str, Any]:
        n = len(robot.media_files)
        return {"total": n, "indexed": n, "pending": 0, "failed": 0}

    @app.get("/media/stats")
    async def media_stats() -> dict[str, Any]:
        assets = list(robot.media_files.values())
        total_b = sum(a.size_bytes for a in assets)
        return {
            "total_videos": sum(1 for a in assets if a.file_type == "video"),
            "total_photos": sum(1 for a in assets if a.file_type == "photo"),
            "total_sessions": len({a.session_id for a in assets}),
            "total_size_bytes": total_b,
            "sdcard_size_bytes": 0,
            "internal_size_bytes": total_b,
        }

    def _session_dict(sid: str, assets: list[SimMediaAsset]) -> dict[str, Any]:
        vids = [a for a in assets if a.file_type == "video"]
        thm = next((a for a in assets if a.file_type in ("thumbnail", "photo")), None)
        t0 = int(min(a.created_at for a in assets) * 1000)
        return {
            "session_id": sid,
            "total_duration_ms": round(sum(a.duration_s for a in vids) * 1000),
            "segment_count": len(vids),
            "thumbnail_path": f"DCIM/100MONDO/{thm.filename}" if thm else None,
            "has_highlights": any(a.highlights for a in vids),
            "status": "complete",
            "started_at": t0,
            "created_at": t0,
        }

    def _sessions() -> dict[str, list[SimMediaAsset]]:
        by_ses: dict[str, list[SimMediaAsset]] = {}
        for a in robot.media_files.values():
            by_ses.setdefault(a.session_id, []).append(a)
        return by_ses

    @app.get("/media/sessions")
    async def media_sessions() -> dict[str, Any]:
        items = [_session_dict(sid, assets) for sid, assets in _sessions().items()]
        items.sort(key=lambda d: d["created_at"], reverse=True)
        return {"items": items, "next_cursor": None, "has_more": False}

    @app.get("/media/sessions/{session_id}")
    async def media_session_one(session_id: str) -> dict[str, Any]:
        assets = _sessions().get(session_id)
        if not assets:
            raise _NotFound(f"session not found: {session_id}")
        vids = sorted((a for a in assets if a.file_type == "video"), key=lambda a: a.created_at)
        segments = []
        offset = 0
        for idx, a in enumerate(vids):
            d = a.firmware_dict()
            segments.append({
                "segment_index": idx, "file_id": d["id"], "download_url": d["download_url"], "duration_ms": d["duration_ms"],
                "container_duration_ms": d["container_duration_ms"], "offset_ms": offset,
                "width": d["width"], "height": d["height"], "fps": d["fps"],
            })
            offset += d["duration_ms"] or 0
        highlights = [{"offset_ms": round(h * 1000)} for a in vids for h in a.highlights]
        return {**_session_dict(session_id, assets), "segments": segments, "highlights": highlights}

    @app.get("/media/files")
    async def media_files(
        limit: int = 50, cursor: str | None = None, file_type: str | None = None, session_id: str | None = None
    ) -> dict[str, Any]:
        items = sorted(robot.media_files.values(), key=lambda x: (x.created_at, int(x.file_id)), reverse=True)
        if file_type:
            items = [a for a in items if a.file_type == file_type]
        if session_id:
            items = [a for a in items if a.session_id == session_id]
        if cursor:  # opaque base64 JSON {"t": created_ms, "i": id}, like the firmware's keyset cursor
            try:
                cur = json.loads(base64.b64decode(cursor))
                key = (int(cur["t"]), int(cur["i"]))
            except (ValueError, KeyError, TypeError):
                return PlainTextResponse("invalid cursor", status_code=400)  # type: ignore[return-value]
            items = [a for a in items if (int(a.created_at * 1000), int(a.file_id)) < key]
        page = items[: max(1, min(limit, 500))]
        more = len(items) > len(page)
        nxt = None
        if more:
            last = page[-1]
            nxt = base64.b64encode(json.dumps({"t": int(last.created_at * 1000), "i": int(last.file_id)}).encode()).decode()
        return {"items": [a.firmware_dict() for a in page], "next_cursor": nxt, "has_more": more}

    @app.get("/media/files/{file_id}")
    async def media_file_meta(file_id: str) -> dict[str, Any]:
        return _asset(file_id).firmware_dict()

    @app.api_route("/media/files/{file_id}/content", methods=["GET", "HEAD"])
    async def media_file_content(file_id: str, request: Request) -> Response:
        asset = _asset(file_id)
        # firmware: thumbnails/LRV go out as octet-stream, photos/videos with their real type
        ctype = asset.mime_type if asset.file_type in ("photo", "video") else "application/octet-stream"
        base_headers = {"Accept-Ranges": "bytes", "Content-Disposition": f'attachment; filename="{asset.filename}"'}
        rng = request.headers.get("range")
        total = len(asset.content)
        if rng and rng.startswith("bytes="):
            spec = rng[6:].split(",", 1)[0].strip().split("-", 1)
            try:
                if not spec[0]:  # suffix range: last N bytes
                    n = int(spec[1])
                    start, end = max(0, total - n), total - 1
                else:
                    start = int(spec[0])
                    end = int(spec[1]) if (len(spec) > 1 and spec[1]) else total - 1
            except ValueError:
                return Response(status_code=416, headers={"Content-Range": f"bytes */{total}"})
            end = min(total - 1, end)
            if start >= total or start > end:
                return Response(status_code=416, headers={"Content-Range": f"bytes */{total}"})
            chunk = asset.content[start : end + 1]
            return Response(
                content=chunk,
                status_code=206,
                media_type=ctype,
                headers={**base_headers, "Content-Range": f"bytes {start}-{end}/{total}"},
            )
        return Response(content=asset.content, media_type=ctype, headers=base_headers)

    @app.delete("/media/files/{file_id}")
    async def media_file_delete(file_id: str) -> dict[str, Any]:
        asset = _asset(file_id)
        # deleting a video also removes its sidecars (firmware: same-stem children / parent_file_id cascade)
        stem = asset.filename.rsplit(".", 1)[0]
        doomed = [asset.file_id]
        if asset.file_type == "video":
            doomed += [k for k, a in robot.media_files.items() if a.file_type in ("thumbnail", "lrv") and a.filename.rsplit(".", 1)[0] == stem]
        for k in doomed:
            robot.media_files.pop(k, None)
        robot.bus.publish("/status/media/catalog_changed", revision=robot.media_counter, storage_kind=1, change_reason=3)
        return {"deleted_count": len(doomed)}

    # 3. Local OTA Upgrade (:8088)
    @app.get("/list")
    async def ota_list() -> dict[str, Any]:
        return {"packages": robot.ota_packages}

    @app.post("/upload")
    async def ota_upload(file: UploadFile = File(...), md5: str = Form(""), size: str = Form("0")) -> dict[str, Any]:  # noqa: B008 (FastAPI idiom)
        raw = await file.read()
        entry = {
            "filename": file.filename or "firmware.bin",
            "bytes": len(raw),
            "md5": md5,
            "uploaded_at": int(time.time()),
        }
        robot.ota_packages.append(entry)
        return {"ok": True, "package": entry}

    @app.post("/v2/upgrades")
    async def ota_v2_create(payload: dict[str, Any]) -> dict[str, Any]:
        uid = f"upg_{len(robot.ota_packages) + 1}"
        return {"upgrade_id": uid, "filename": payload.get("filename", "fw.tar")}

    @app.put("/v2/upgrades/{upgrade_id}/upload")
    async def ota_v2_put(upgrade_id: str, request: Request) -> dict[str, Any]:
        body = await request.body()
        robot.ota_packages.append({"upgrade_id": upgrade_id, "bytes": len(body)})
        return {"ok": True, "upgrade_id": upgrade_id, "bytes": len(body)}

    @app.post("/v2/upgrades/{upgrade_id}/cancel")
    async def ota_v2_cancel(upgrade_id: str) -> dict[str, Any]:
        robot.ota_stage = 0
        return {"ok": True, "upgrade_id": upgrade_id, "cancelled": True}

    return app
