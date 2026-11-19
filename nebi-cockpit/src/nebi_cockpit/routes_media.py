"""Media album proxy routes (the robot's media server, :8084).

`/api/media/files/{id}/content` streams the robot's response through instead of buffering the whole
file, and forwards `Range` requests: status 206, `Content-Range`, `Content-Length` and `Accept-Ranges`
pass through so `<video>` seeking works.

Robot-side failures surface as structured `NebiError` JSON (see `errors.py`): `MEDIA_AUTH_REQUIRED` (bearer
rejected), `MEDIA_UNREACHABLE`, `MEDIA_NOT_FOUND`, `MEDIA_STORAGE_BUSY`, or `NOT_CONNECTED` when there is no link,
so the gallery can tell "empty album" from "cannot read the album".
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from nebi_cockpit.deps import ClientDep

_FORWARD_RESPONSE_HEADERS = ("content-length", "content-encoding", "content-range", "accept-ranges", "last-modified", "etag")
_STREAM_CHUNK_BYTES = 65536

_FILENAME_RE = re.compile(r'filename="?([^";]+)"?')
# The robot's media server sends sidecars (.thm JPEG thumbnails, .lrv low-res MP4 proxies) as application/octet-stream
_EXT_MEDIA_TYPES = {".thm": "image/jpeg", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".lrv": "video/mp4", ".mp4": "video/mp4"}

router = APIRouter(prefix="/api/media", tags=["media"])


def _media_type(headers: Any) -> str:
    ctype = str(headers.get("content-type", "application/octet-stream"))
    if not ctype.startswith("application/octet-stream"):
        return ctype
    m = _FILENAME_RE.search(str(headers.get("content-disposition", "")))
    ext = ("." + m.group(1).rsplit(".", 1)[-1].lower()) if m and "." in m.group(1) else ""
    return _EXT_MEDIA_TYPES.get(ext, ctype)


@router.get("/status")
async def api_media_status(client: ClientDep) -> dict[str, Any]:
    """Album reachability for UIs/agents: `state` is ok, empty, auth_error, unreachable or error (never raises)."""
    return await client.media.probe()


@router.get("/stats")
async def api_media_stats(client: ClientDep) -> dict[str, Any]:
    return await client.media.stats()


@router.get("/files")
async def api_media_files(
    client: ClientDep, limit: int = 50, file_type: str | None = None, cursor: str | None = None, include_aux: bool = False
) -> dict[str, Any]:
    return await client.media.list_files(limit=limit, file_type=file_type, cursor=cursor, include_aux=include_aux)


@router.get("/files/{file_id}")
async def api_media_file(file_id: str, client: ClientDep) -> dict[str, Any]:
    return await client.media.get_file(file_id)


@router.get("/sessions")
async def api_media_sessions(client: ClientDep) -> dict[str, Any]:
    return await client.media.list_sessions()


@router.get("/files/{file_id}/content")
async def api_media_content(file_id: str, request: Request, client: ClientDep) -> Response:
    stack = AsyncExitStack()
    # Errors (unreachable, 401/403, 404, ...) surface as NebiError -> structured JSON via the app handler.
    upstream = await stack.enter_async_context(
        client.media_http.open_content_stream(file_id, byte_range=request.headers.get("range"))  # client URL-encodes
    )
    out_headers = {h: upstream.headers[h] for h in _FORWARD_RESPONSE_HEADERS if h in upstream.headers}
    out_headers.setdefault("accept-ranges", "bytes")

    async def body() -> AsyncIterator[bytes]:
        try:
            # raw bytes: Content-Length is forwarded, so never let httpx undo a content-encoding
            async for chunk in upstream.aiter_raw(_STREAM_CHUNK_BYTES):
                yield chunk
        finally:
            await stack.aclose()

    return StreamingResponse(
        body(),
        status_code=upstream.status_code,
        headers=out_headers,
        media_type=_media_type(upstream.headers),
        background=BackgroundTask(stack.aclose),  # also closes if the body was never iterated
    )


@router.delete("/files/{file_id}")
async def api_media_delete(file_id: str, client: ClientDep) -> dict[str, Any]:
    return await client.media.delete_file(file_id)


@router.post("/format")
async def api_media_format(client: ClientDep) -> dict[str, Any]:
    return await client.media.format_storage()
