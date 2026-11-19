"""Camera and media commands: photo, record, zoom, media (robot storage)."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nebi_cli import spec as S
from nebi_cli.errors import CACHE_DIR, AgentError
from nebi_cli.registry import Args, CommandGroup, command

if TYPE_CHECKING:
    from nebi.client import AsyncNebiClient

logger = logging.getLogger(__name__)

PHOTO_LIST_WAIT_S = 8.0  # how long `photo` waits for the new file to show up in the media list
MEDIA_KEEP = ("file_id", "filename", "file_type", "size_bytes", "duration_s", "created_at", "width", "height")


def _files(listing: dict[str, Any]) -> list[dict[str, Any]]:
    """Media list replies carry `files` (newer firmware) or `items`."""
    return [f for f in (listing.get("files", []) or listing.get("items", [])) if isinstance(f, dict)]


async def _photo_ids(c: AsyncNebiClient) -> set[Any]:
    try:
        return {f.get("file_id") for f in _files(await c.media.list_files(limit=20, file_type="photo"))}
    except Exception:  # listing may need auth on some firmwares: then every listed file counts as new
        logger.debug("media list failed", exc_info=True)
        return set()


class CameraCommands(CommandGroup):
    @command("photo")
    async def photo(self, a: Args) -> dict[str, Any]:
        c = self.client()
        warnings: list[str] = a["_warnings"]
        before = await _photo_ids(c)
        await c.camera.take_photo()
        new: dict[str, Any] | None = None
        deadline = time.monotonic() + PHOTO_LIST_WAIT_S
        while time.monotonic() < deadline:
            await asyncio.sleep(0.5)
            try:
                lst = await c.media.list_files(limit=20, file_type="photo")
            except Exception:
                logger.debug("media list failed", exc_info=True)
                break
            fresh = [f for f in _files(lst) if f.get("file_id") not in before]
            if fresh:
                new = fresh[0]
                break
        res: dict[str, Any] = {"captured": True, "file": new}
        if new is None:
            warnings.append("Photo command sent but the new file did not show up in the media list (yet).")
            res["summary"] = "photo taken (file not listed yet)"
        else:
            res["summary"] = f"photo taken: {new.get('filename') or new.get('file_id')}"
            if a.get("download") and new.get("file_id"):
                p = await c.media.save_file(new["file_id"], Path(a["download"]))
                res["downloaded"] = str(p.resolve())
                res["summary"] += f", saved to {res['downloaded']}"
        return res

    @command("record")
    async def record(self, a: Args) -> dict[str, Any]:
        c = self.client()
        act = a["action"]
        if act == "start":
            return await c.camera.start_recording()
        if act == "stop":
            return await c.camera.stop_recording()
        await c.camera.mark_highlight()
        return {"highlight": True}

    @command("zoom")
    async def zoom(self, a: Args) -> dict[str, Any]:
        c = self.client()
        z = max(1.0, min(4.0, float(a["factor"])))
        await c.camera.set_zoom(z)
        return {"zoom": z}

    @command("media")
    async def media(self, a: Args) -> dict[str, Any]:
        c = self.client()
        act = a.get("action") or "ls"
        fid = a.get("file_id")
        if act == "stats":
            return await c.media.stats()
        if act == "ls":
            lst = await c.media.list_files(limit=int(a.get("limit") or 20), file_type=a.get("type"))
            files = lst.get("files", []) or lst.get("items", [])
            out = [{k: f.get(k) for k in MEDIA_KEEP if k in f} for f in files]
            res: dict[str, Any] = {"count": len(out), "files": out}
            if lst.get("auth_required"):
                res["note"] = "Robot media server requires auth on this firmware; listing unavailable."
            return res
        if not fid:
            raise AgentError("BAD_ARGUMENT", f"`nebi media {act}` needs a file_id (see `nebi media ls`).", exit_code=S.EXIT_USAGE)
        if act == "get":
            meta: dict[str, Any] = {}
            try:
                meta = await c.media.get_file(fid)
            except Exception:  # metadata only names the file; fall back to the id
                logger.debug("media get_file failed", exc_info=True)
            path = Path(a["out"]) if a.get("out") else CACHE_DIR / "media" / (meta.get("filename") or fid)
            p = await c.media.save_file(fid, path)
            return {"file_id": fid, "path": str(p.resolve()), "size_bytes": p.stat().st_size}
        if act == "rm":
            return await c.media.delete_file(fid)
        raise AgentError("BAD_ARGUMENT", f"Unknown media action {act!r}.", exit_code=S.EXIT_USAGE)
