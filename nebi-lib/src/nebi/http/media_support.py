"""Robot media server (:8084) bearer token, response normalization and structured errors.

How the robot authenticates media requests (firmware V26.10.28.x):

- The robot's media server serves HTTPS with a self-signed certificate. Every `/media/*` route takes
  `Authorization: Bearer <token>`, hashes the token with SHA-256 and compares it to a fixed digest
  (`MEDIA_TOKEN_SHA256`). Missing or wrong token: `401`, body `AUTH_REQUIRED`, `www-authenticate: Bearer`.
- The token is one static string shared by all retail robots. There is no per-device
  or per-session exchange (no Zenoh request, no config-agent call), and it does not expire: "refresh" therefore
  means re-resolving the configured candidates after a 401.

nebi does NOT ship that token. The user supplies it: explicit argument > `$PYNEBI_MEDIA_TOKEN` >
`~/.config/pynebi/media_token`. `SIM_MEDIA_TOKEN` (public, accepted only by the bundled simulator) is always the
last candidate. A rejected candidate is followed by the next one once before reporting `MEDIA_AUTH_REQUIRED`
(or `MEDIA_TOKEN_MISSING` when the user configured none). `token_matches_firmware()` checks a user-supplied token
against the firmware digest offline, without revealing the token.
"""

from __future__ import annotations

import hashlib
import mimetypes
import os
from pathlib import Path, PurePosixPath
from typing import Any

from nebi.exceptions import NebiError

SIM_MEDIA_TOKEN = "nebi-simulator-media-token"
"""Public token accepted only by nebi's simulator (the real robot rejects it)."""

MEDIA_TOKEN_SHA256 = "5500fdb05dcfc31e9ddd00b4892d4ded351227b09b8796ca8bb8cdd4b727b0e5"
"""SHA-256 digest that the robot's media server compares every bearer token against."""

MEDIA_TOKEN_ENV = "PYNEBI_MEDIA_TOKEN"

# Sidecar files the firmware indexes next to each recording; galleries show them through their parent video.
AUX_FILE_TYPES = frozenset({"thumbnail", "lrv"})


def media_token_file() -> Path:
    return Path.home() / ".config" / "pynebi" / "media_token"


def token_matches_firmware(token: str) -> bool:
    """True when `token` passes the retail firmware check (`sha256(token) == MEDIA_TOKEN_SHA256`)."""
    return hashlib.sha256(token.encode()).hexdigest() == MEDIA_TOKEN_SHA256


def _fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:12]


class MediaTokenProvider:
    """Resolves and caches the media bearer; `rotate()` moves to the next candidate after a 401."""

    def __init__(self, token: str | None = None) -> None:
        self._explicit = token.strip() if token else None
        self._candidates: list[tuple[str, str]] | None = None
        self._index = 0

    def _resolve(self) -> list[tuple[str, str]]:
        found: list[tuple[str, str]] = []
        if self._explicit:
            found.append(("argument", self._explicit))
        env = os.environ.get(MEDIA_TOKEN_ENV, "").strip()
        if env:
            found.append((f"${MEDIA_TOKEN_ENV}", env))
        try:
            file_tok = media_token_file().read_text(encoding="utf-8").strip()
        except OSError:
            file_tok = ""
        if file_tok:
            found.append((str(media_token_file()), file_tok))
        found.append(("simulator token", SIM_MEDIA_TOKEN))
        unique: list[tuple[str, str]] = []
        for src, tok in found:
            if tok not in (t for _, t in unique):
                unique.append((src, tok))
        return unique

    def _ensure(self) -> list[tuple[str, str]]:
        if self._candidates is None:
            self._candidates, self._index = self._resolve(), 0
        return self._candidates

    @property
    def token(self) -> str:
        return self._ensure()[self._index][1]

    @property
    def source(self) -> str:
        return self._ensure()[self._index][0]

    def rotate(self) -> bool:
        """After a 401: advance to the next candidate. False when every candidate has been tried."""
        cands = self._ensure()
        if self._index + 1 < len(cands):
            self._index += 1
            return True
        return False

    def reset(self) -> None:
        """Forget the cache (re-read env/file on next use), e.g. after the user fixed an override."""
        self._candidates = None

    @property
    def user_configured(self) -> bool:
        """True when the user supplied at least one token (argument, env or file)."""
        return any(src != "simulator token" for src, _ in self._ensure())

    def describe(self) -> dict[str, Any]:
        tok = self.token
        return {"source": self.source, "sha256_prefix": _fingerprint(tok), "matches_retail_firmware": token_matches_firmware(tok)}


# --------------------------------------------------------------------------------------------- errors


def media_error(
    code: str, message: str, hint: str, *, status: int | None = None, path: str = "", body: str = "", **extra: Any
) -> NebiError:
    details: dict[str, Any] = {"service": "media", **extra}
    if status is not None:
        details["http_status"] = status
    if path:
        details["path"] = path
    if body:
        details["body"] = body[:200]
    return NebiError(code, message, stage="connected", recoverable=True, hint=hint, details=details)


def auth_error(endpoint: str, status: int, path: str, body: str, tokens: MediaTokenProvider) -> NebiError:
    if not tokens.user_configured:
        return media_error(
            "MEDIA_TOKEN_MISSING",
            f"Robot media server {endpoint} needs a bearer token and none is configured (HTTP {status}).",
            f"nebi does not ship the robot's media token. Put it in {media_token_file()} (chmod 600) or set "
            f"${MEDIA_TOKEN_ENV}; see the nebi README, section 'Media gallery access'.",
            status=status,
            path=path,
            body=body,
        )
    return media_error(
        "MEDIA_AUTH_REQUIRED",
        f"Robot media server {endpoint} rejected the bearer token (HTTP {status} {body.strip()[:40] or 'AUTH_REQUIRED'}).",
        f"The configured token ({tokens.describe()['source']}) is wrong or the firmware changed it. "
        f"Fix {media_token_file()} or ${MEDIA_TOKEN_ENV}; details.token.matches_retail_firmware tells whether it "
        "matches the digest nebi knows.",
        status=status,
        path=path,
        body=body,
        token=tokens.describe(),
    )


def unreachable_error(endpoint: str, path: str, exc: Exception) -> NebiError:
    return media_error(
        "MEDIA_UNREACHABLE",
        f"Robot media server {endpoint} is unreachable: {type(exc).__name__}: {exc}".rstrip(": "),
        "The album is served over the robot's Wi-Fi/LAN IP (HTTPS port 8084). Check that the robot is powered, on "
        "the same network and connected in the cockpit; BLE-only links have no media access.",
        path=path,
    )


def status_error(endpoint: str, status: int, path: str, body: str) -> NebiError:
    text = body.strip()
    if status == 404:
        return media_error(
            "MEDIA_NOT_FOUND",
            f"Media file or session not found on the robot ({text[:80] or 'HTTP 404'}).",
            "List files again (`nebi media ls` or GET /api/media/files); ids are the robot's integer index ids "
            "and change after a format.",
            status=status,
            path=path,
            body=body,
        )
    if status == 503:
        return media_error(
            "MEDIA_STORAGE_BUSY",
            f"Robot media storage is temporarily unavailable ({text[:80] or 'HTTP 503'}).",
            "Storage is being mounted, scanned or formatted; retry in a few seconds.",
            status=status,
            path=path,
            body=body,
        )
    return media_error(
        "MEDIA_HTTP_ERROR",
        f"Robot media server {endpoint} answered HTTP {status} for {path}: {text[:120]}",
        "Retry; if it persists, restart the robot.",
        status=status,
        path=path,
        body=body,
    )


# --------------------------------------------------------------------------------------------- schema


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _created_seconds(v: Any) -> float | None:
    n = _num(v)
    if n is None:
        return None
    return n / 1000.0 if n > 1e11 else n  # firmware: epoch ms; older/simulated payloads: epoch s


def guess_mime(filename: str, file_type: str) -> str:
    ext = PurePosixPath(filename).suffix.lower()
    if ext in (".thm", ".jpg", ".jpeg") or file_type in ("thumbnail", "photo"):
        return "image/jpeg"
    if ext in (".lrv", ".mp4", ".mov") or file_type in ("video", "lrv", "highlight"):
        return "video/mp4"
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


def normalize_file(raw: dict[str, Any]) -> dict[str, Any]:
    """Firmware item (`id`, `file_name`, `file_size`, `duration_ms`, `created_at` ms) -> stable nebi shape.

    Canonical keys: `file_id` (str), `filename`, `file_type`, `size_bytes`, `duration_s`, `created_at` (epoch s),
    `width`, `height`, `fps`, `session_id`, `status`, `mime_type`. Unknown firmware keys are kept as-is.
    """
    out = dict(raw)
    fid = raw.get("file_id", raw.get("id"))
    out["file_id"] = str(fid) if fid is not None else ""
    out["filename"] = raw.get("filename") or raw.get("file_name") or out["file_id"]
    out["file_type"] = str(raw.get("file_type") or "")
    size = raw.get("size_bytes", raw.get("file_size"))
    out["size_bytes"] = int(size) if isinstance(size, (int, float)) else None
    dur_ms = _num(raw.get("duration_ms"))
    out["duration_s"] = round(dur_ms / 1000.0, 3) if dur_ms is not None else _num(raw.get("duration_s"))
    out["created_at"] = _created_seconds(raw.get("created_at"))
    out["mime_type"] = raw.get("mime_type") or guess_mime(out["filename"], out["file_type"])
    for k in ("id", "file_name", "file_size", "duration_ms"):
        out.pop(k, None)
    return out


def link_aux_files(files: list[dict[str, Any]], aux: list[dict[str, Any]]) -> None:
    """Attach `thumbnail_file_id` / `proxy_file_id` (LRV low-res MP4) to videos sharing the same file stem."""
    by_stem: dict[tuple[str, str], str] = {}
    for a in aux:
        by_stem[(PurePosixPath(a["filename"]).stem, a["file_type"])] = a["file_id"]
    for f in files:
        stem = PurePosixPath(f["filename"]).stem
        if f["file_type"] == "photo":
            f.setdefault("thumbnail_file_id", f["file_id"])
            continue
        if (thm := by_stem.get((stem, "thumbnail"))) is not None:
            f["thumbnail_file_id"] = thm
        if (lrv := by_stem.get((stem, "lrv"))) is not None:
            f["proxy_file_id"] = lrv


def normalize_stats(raw: dict[str, Any]) -> dict[str, Any]:
    """Firmware `total_videos` / `total_photos` / `total_size_bytes` -> `videos` / `photos` / `total_bytes`."""
    out = dict(raw)
    out["photos"] = raw.get("photos", raw.get("total_photos", 0))
    out["videos"] = raw.get("videos", raw.get("total_videos", 0))
    out["sessions"] = raw.get("sessions", raw.get("total_sessions"))
    out["total_bytes"] = raw.get("total_bytes", raw.get("total_size_bytes", 0))
    out["sdcard_bytes"] = raw.get("sdcard_size_bytes")
    out["internal_bytes"] = raw.get("internal_size_bytes")
    out.setdefault("ok", True)
    return out
