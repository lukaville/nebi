"""FastAPI web server and REST/SSE API for the `nebi-ai` pet robot UI."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib import resources
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from nebi_ai.agent import PetAgentSession
from nebi_ai.config import LIVE_MODEL_PRESETS, LIVE_VOICE_PRESETS, MODEL_PRESETS

_QUICK_ACTIONS: dict[str, tuple[str, dict[str, Any]]] = {
    "stand": ("stand", {}),
    "sit": ("sit", {}),
    "dock": ("dock", {}),
    "look": ("look", {"annotate": True}),
    "greet": ("emote", {"emotion": "greet"}),
    "happy": ("emote", {"emotion": "happy"}),
    "wiggle": ("body", {"gesture": "wiggle"}),
}


class ChatBody(BaseModel):
    message: str


class ConfigUpdateBody(BaseModel):
    updates: dict[str, Any] = Field(default_factory=dict)
    api_key: str | None = None


class ActionBody(BaseModel):
    action: str


class MemoryWriteBody(BaseModel):
    content: str


def _has_configured_api_key(model: str, has_override: bool) -> bool:
    if has_override:
        return True
    provider = model.split(":", 1)[0].lower() if ":" in model else "google_genai"
    if provider in ("google_genai", "google", "gemini"):
        return bool(os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY"))
    if provider == "openai":
        return bool(os.environ.get("OPENAI_API_KEY"))
    if provider == "anthropic":
        return bool(os.environ.get("ANTHROPIC_API_KEY"))
    return True  # e.g. ollama / local models


def create_web_app(
    session: PetAgentSession,
    *,
    manage_lifecycle: bool = True,
    start_autonomy: bool = True,
) -> FastAPI:
    """Create the FastAPI application backing the `nebi-ai` Web UI."""

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if manage_lifecycle:
            await session.bridge.start()
            if start_autonomy:
                session.start_autonomy()
        try:
            yield
        finally:
            if manage_lifecycle:
                await session.stop_autonomy()
                await session.bridge.stop()

    app = FastAPI(title="nebi-ai", lifespan=lifespan)
    cached_html = resources.files("nebi_ai").joinpath("web.html").read_text(encoding="utf-8")

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        return HTMLResponse(content=cached_html, headers={"Cache-Control": "no-cache"})

    @app.get("/api/info")
    async def api_info() -> dict[str, Any]:
        return {
            "config": session.config.to_dict(),
            "model_presets": list(MODEL_PRESETS),
            "live_model_presets": list(LIVE_MODEL_PRESETS),
            "live_voice_presets": list(LIVE_VOICE_PRESETS),
            "live": session.live.status_dict(),
            "total_tokens": session.total_tokens,
            "home_dir": str(session.memory_store.home_dir),
            "memories_dir": str(session.memory_store.memories_dir),
            "clips_dir": str(session.memory_store.clips_dir),
            "target": session.bridge.target.label,
            "busy": session.busy,
            "last_tick_at": session.last_tick_at,
            "has_api_key": _has_configured_api_key(session.config.model, bool(session._api_key_override)),
        }

    @app.get("/api/state")
    async def api_state() -> dict[str, Any]:
        try:
            st = await session.bridge.state()
            return {**st, "live": session.live.status_dict(), "total_tokens": session.total_tokens}
        except Exception as exc:
            return {"ok": False, "error": {"code": "STATE_ERROR", "message": str(exc)}}

    @app.get("/api/history")
    async def api_history(limit: int = Query(default=60, ge=1, le=200)) -> dict[str, Any]:
        return {"items": session.memory_store.load_history(limit=limit)}

    @app.post("/api/session/reset")
    async def api_reset_session(clear_history: bool = Query(default=True)) -> dict[str, Any]:
        """Clear chat log and restart both the Gemini Live session and DeepAgent thread (preserving MEMORY.md)."""
        return await session.reset_session(clear_history=clear_history)

    @app.post("/api/chat")
    async def api_chat(body: ChatBody) -> dict[str, Any]:
        if not body.message.strip():
            raise HTTPException(status_code=400, detail="Message must not be empty")
        record = await session.chat(body.message)
        return record.to_dict()

    @app.post("/api/live/text")
    async def api_live_text(body: ChatBody) -> dict[str, Any]:
        if not body.message.strip():
            raise HTTPException(status_code=400, detail="Message must not be empty")
        sent = await session.live.send_text(body.message)
        return {"ok": sent, "live_connected": session.live.connected}

    @app.post("/api/listen")
    async def api_listen(seconds: float = Query(default=8.0, ge=1.0, le=30.0)) -> dict[str, Any]:
        """Listen through the robot microphone, transcribe speech, and send it to the pet agent."""
        heard, text = await session.listen_once(seconds=seconds)
        if not heard.get("ok"):
            return {"ok": False, "heard": heard}
        if not text:
            return {"ok": True, "heard": heard, "text": "", "turn": None}
        record = await session.chat(f"[Heard via microphone]: {text}")
        return {"ok": True, "heard": heard, "text": text, "turn": record.to_dict()}

    @app.post("/api/tick")
    async def api_tick() -> dict[str, Any]:
        record = await session.autonomy_tick()
        if record is None:
            return {"skipped": True, "reason": "autonomy disabled or quiet hours"}
        return record.to_dict()

    @app.post("/api/config")
    async def api_update_config(body: ConfigUpdateBody) -> dict[str, Any]:
        cfg = session.update_config(body.updates, api_key=body.api_key)
        return {
            "config": cfg.to_dict(),
            "has_api_key": _has_configured_api_key(cfg.model, bool(session._api_key_override)),
        }

    @app.post("/api/action")
    async def api_action(body: ActionBody) -> dict[str, Any]:
        act = body.action.strip().lower()
        if act == "stop":
            return await session.emergency_stop()
        if act not in _QUICK_ACTIONS:
            raise HTTPException(status_code=400, detail=f"Unknown quick action: {act!r}")
        cmd, args = _QUICK_ACTIONS[act]
        return await session.bridge.call(cmd, args)

    @app.get("/api/camera.jpg")
    async def api_camera(refresh: bool = Query(default=False)) -> Response:
        raw = None if refresh else await asyncio.to_thread(session.bridge.read_last_snapshot_bytes)
        if raw is None:
            await session.bridge.call("look", {"annotate": True})
            raw = await asyncio.to_thread(session.bridge.read_last_snapshot_bytes)
        if not raw:
            raise HTTPException(status_code=404, detail="No camera snapshot available yet")
        return Response(content=raw, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.get("/api/audio/clips")
    async def api_list_clips() -> dict[str, Any]:
        items = await asyncio.to_thread(session.memory_store.list_clips)
        return {"clips_dir": str(session.memory_store.clips_dir), "items": items}

    @app.get("/api/audio/clips/{filename}")
    async def api_get_clip(filename: str) -> Response:
        try:
            path = session.memory_store.resolve_clip_path(filename)
            raw = await asyncio.to_thread(path.read_bytes)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return Response(content=raw, media_type="audio/wav", headers={"Cache-Control": "no-store"})

    @app.post("/api/audio/record")
    async def api_record_clip(seconds: float = Query(default=5.0, ge=1.0, le=15.0)) -> dict[str, Any]:
        return await session.live.record_diagnostic_clip(seconds=seconds)

    @app.get("/api/memories")
    async def api_list_memories() -> dict[str, Any]:
        return {"memories_dir": str(session.memory_store.memories_dir), "files": session.memory_store.list_memories()}

    @app.get("/api/memories/{rel_path:path}")
    async def api_read_memory(rel_path: str) -> dict[str, Any]:
        try:
            content = session.memory_store.read_memory(rel_path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"path": rel_path, "content": content}

    @app.put("/api/memories/{rel_path:path}")
    async def api_write_memory(rel_path: str, body: MemoryWriteBody) -> dict[str, Any]:
        try:
            session.memory_store.write_memory(rel_path, body.content)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "path": rel_path}

    @app.delete("/api/memories/{rel_path:path}")
    async def api_delete_memory(rel_path: str) -> dict[str, Any]:
        try:
            deleted = session.memory_store.delete_memory(rel_path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "deleted": deleted}

    @app.get("/api/events")
    async def api_events(request: Request) -> StreamingResponse:
        queue = session.subscribe()

        async def event_stream() -> AsyncIterator[str]:
            try:
                hello_payload = {
                    "type": "hello",
                    "data": {
                        "busy": session.busy,
                        "total_tokens": session.total_tokens,
                        "live": session.live.status_dict(),
                    },
                }
                yield f"data: {json.dumps(hello_payload)}\n\n"
                while not session._stop_event.is_set() and not await request.is_disconnected():
                    get_task = asyncio.create_task(queue.get())
                    stop_task = asyncio.create_task(session._stop_event.wait())
                    try:
                        done, pending = await asyncio.wait(
                            {get_task, stop_task},
                            timeout=15.0,
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        for p in pending:
                            p.cancel()
                        if stop_task in done:
                            break
                        if get_task in done:
                            event = get_task.result()
                            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                        else:
                            yield ": keepalive\n\n"
                    except asyncio.CancelledError:
                        get_task.cancel()
                        stop_task.cancel()
                        raise
            finally:
                session.unsubscribe(queue)

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    return app
