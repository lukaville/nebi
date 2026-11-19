"""Media album browsing, MP4/JPEG downloads, session queries (`:8084/media/*`), and SD storage ops."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nebi.domains._base import DomainController
from nebi.exceptions import NebiError

if TYPE_CHECKING:
    from nebi.http.clients import MediaHttpClient
    from nebi.transport.zenoh_ucp import ZenohTopicBus


class MediaController(DomainController):
    """High-level async controller for Nebi's captured photos, MP4 recordings, and SD/internal storage."""

    def __init__(self, bus: ZenohTopicBus, media_http: MediaHttpClient) -> None:
        super().__init__(bus)
        self._http = media_http

    def rebind(self, bus: ZenohTopicBus, media_http: MediaHttpClient | None = None) -> None:
        super().rebind(bus)
        if media_http is not None:
            self._http = media_http

    async def stats(self) -> dict[str, Any]:
        return await self._http.stats()

    async def list_sessions(self) -> dict[str, Any]:
        return await self._http.list_sessions()

    async def list_files(
        self,
        limit: int = 50,
        cursor: str | None = None,
        file_type: str | None = None,
        session_id: str | None = None,
        *,
        include_aux: bool = False,
    ) -> dict[str, Any]:
        """Album page, newest first. Videos carry `thumbnail_file_id` / `proxy_file_id` (sidecars folded in)."""
        return await self._http.list_files(
            limit=limit,
            cursor=cursor,
            file_type=file_type,
            session_id=session_id,
            include_aux=include_aux,
        )

    async def probe(self) -> dict[str, Any]:
        """Never raises: `{"state": "ok" | "auth_error" | "unreachable" | "error", "error"?: {...}, "stats"?: {...}}`."""
        try:
            stats = await self._http.stats()
        except NebiError as exc:
            state = {"MEDIA_AUTH_REQUIRED": "auth_error", "MEDIA_UNREACHABLE": "unreachable"}.get(exc.code, "error")
            return {"ok": False, "state": state, "error": exc.to_dict()}
        return {"ok": True, "state": "ok" if stats.get("photos") or stats.get("videos") else "empty", "stats": stats}

    async def get_file(self, file_id: str) -> dict[str, Any]:
        return await self._http.get_file(file_id)

    async def download_bytes(self, file_id: str, byte_range: str | None = None) -> tuple[bytes, str]:
        return await self._http.download_content(file_id, byte_range=byte_range)

    async def save_file(self, file_id: str, destination: str | Path) -> Path:
        data, _ = await self._http.download_content(file_id)
        out = Path(destination)

        def _write() -> None:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(data)

        await asyncio.to_thread(_write)  # media files can be large: keep file I/O off the event loop
        return out

    async def delete_file(self, file_id: str) -> dict[str, Any]:
        return await self._http.delete_file(file_id)

    async def format_storage(self, storage_type: int = 1, timeout: float = 5.0) -> dict[str, Any]:
        """Format SD/internal storage via `/cmd/storage/op_req` (`operation_type=1` SD format)."""
        return await self._request_ints(
            "/cmd/storage/op_req", "/cmd/storage/op_rsp", ("ret_code",), timeout=timeout, operation_type=int(storage_type)
        )
