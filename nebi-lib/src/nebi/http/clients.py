"""Async `httpx` clients for Nebi's local HTTP REST daemons.

Endpoints:
  - `ConfigHttpClient` (`http://<host>:8010/config/v1/local/*`)
  - `MediaHttpClient` (`https://<host>:8084/media/*`, bearer token: `nebi.http.media_support`)
  - `OtaHttpClient` (`http://<host>:8088/*` or shared simulator HTTP port)
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from nebi.http.media_support import (
    AUX_FILE_TYPES,
    MediaTokenProvider,
    auth_error,
    link_aux_files,
    normalize_file,
    normalize_stats,
    status_error,
    unreachable_error,
)

logger = logging.getLogger(__name__)


def _make_async_http_client(
    base_url: str,
    timeout: float,
    local_address: str | None = None,
    *,
    verify: bool = True,
) -> httpx.AsyncClient:
    transport = httpx.AsyncHTTPTransport(local_address=local_address, verify=verify) if local_address else None
    return httpx.AsyncClient(base_url=base_url, timeout=timeout, transport=transport, verify=verify)


class _HttpServiceClient:
    """One robot HTTP daemon at `http://<host>:<port>`, optionally bound to a local address (split-socket flows)."""

    def __init__(self, host: str, port: int, timeout: float, local_address: str | None = None) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.base_url = f"http://{host}:{port}"
        self.local_address = local_address
        self._client = _make_async_http_client(self.base_url, timeout=timeout, local_address=local_address)

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        return await self._client.request(method, path, **kwargs)

    async def _json(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        resp = await self._request(method, path, **kwargs)
        resp.raise_for_status()
        result: dict[str, Any] = resp.json()
        return result

    async def close(self) -> None:
        await self._client.aclose()


class _TlsUpgradingClient(_HttpServiceClient):
    """Shared http:// -> https:// upgrade for the robot's local daemons.

    Real firmware serves some ports over TLS with a self-signed device certificate (hence verify=False, the LAN
    peer is authenticated by the MD-TS layer, not by this HTTP channel). We only switch to https when a plain
    request fails at the protocol level AND the https retry succeeds; a transient network error never flips the
    scheme permanently.
    """

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            return await self._client.request(method, path, **kwargs)
        except (httpx.RemoteProtocolError, httpx.ReadError) as plain_exc:
            if not self.base_url.startswith("http://"):
                raise
            https_url = f"https://{self.host}:{self.port}"
            tls = _make_async_http_client(https_url, timeout=self.timeout, local_address=self.local_address, verify=False)
            try:
                resp = await tls.request(method, path, **kwargs)
            except httpx.HTTPError:
                await tls.aclose()
                raise plain_exc from None  # not a TLS port: keep http and report the original error
            await self._client.aclose()
            self._client, self.base_url = tls, https_url
            logger.info("%s:%d speaks TLS; switched to https", self.host, self.port)
            return resp


class ConfigHttpClient(_TlsUpgradingClient):
    """Client for the robot's local config REST service (`:8010/config/v1/local/*`)."""

    def __init__(self, host: str, port: int = 8010, timeout: float = 4.0, *, local_address: str | None = None) -> None:
        super().__init__(host, port, timeout, local_address)

    async def health(self) -> dict[str, Any]:
        return await self._json("GET", "/config/v1/local/health")

    async def version(self) -> dict[str, Any]:
        return await self._json("GET", "/config/v1/local/version")

    async def status(self) -> dict[str, Any]:
        return await self._json("GET", "/config/v1/local/status")

    async def query(self, keys: list[str] | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if keys is not None:
            payload["keys"] = keys
        return await self._json("POST", "/config/v1/local/query", json=payload)

    async def commit(self, values: dict[str, Any]) -> dict[str, Any]:
        return await self._json("POST", "/config/v1/local/commit", json={"values": values})


class MediaHttpClient(_TlsUpgradingClient):
    """Client for the robot media server's album & streaming REST API (`:8084/media/*`).

    Sends the media bearer token (see `nebi.http.media_support`), retries once with the next token candidate
    after a 401, and normalizes firmware payloads to stable keys (`file_id`, `filename`, `size_bytes`, ...).
    Failures raise `NebiError` with an LLM-friendly code/message/hint: `MEDIA_AUTH_REQUIRED`, `MEDIA_UNREACHABLE`,
    `MEDIA_NOT_FOUND`, `MEDIA_STORAGE_BUSY` or `MEDIA_HTTP_ERROR`.
    """

    def __init__(
        self,
        host: str,
        port: int = 8084,
        timeout: float = 10.0,
        *,
        local_address: str | None = None,
        token: str | None = None,
        tokens: MediaTokenProvider | None = None,
    ) -> None:
        super().__init__(host, port, timeout, local_address)
        self.tokens = tokens or MediaTokenProvider(token)

    @property
    def _endpoint(self) -> str:
        return f"{self.host}:{self.port}"

    def _auth_headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        return {**(extra or {}), "Authorization": f"Bearer {self.tokens.token}"}

    async def _send(self, method: str, path: str, *, headers: dict[str, str] | None = None, **kwargs: Any) -> httpx.Response:
        """Authorized request with one token rotation on 401/403; non-2xx -> structured NebiError."""
        try:
            resp = await self._request(method, path, headers=self._auth_headers(headers), **kwargs)
            while resp.status_code in (401, 403) and self.tokens.rotate():
                logger.info("media token rejected; retrying with %s", self.tokens.source)
                resp = await self._request(method, path, headers=self._auth_headers(headers), **kwargs)
        except httpx.HTTPError as exc:
            raise unreachable_error(self._endpoint, path, exc) from exc
        self._check(resp, path)
        return resp

    def _check(self, resp: httpx.Response, path: str) -> None:
        if resp.status_code < 400:
            return
        try:
            body = resp.text
        except httpx.ResponseNotRead:
            body = ""
        if resp.status_code in (401, 403):
            raise auth_error(self._endpoint, resp.status_code, path, body, self.tokens)
        raise status_error(self._endpoint, resp.status_code, path, body)

    async def _media_json(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        resp = await self._send(method, path, **kwargs)
        try:
            result = resp.json()
        except ValueError:
            return {"ok": True, "text": resp.text}
        return result if isinstance(result, dict) else {"ok": True, "items": result}

    async def health(self) -> dict[str, Any]:
        return await self._media_json("GET", "/media/health")

    async def stats(self) -> dict[str, Any]:
        return normalize_stats(await self._media_json("GET", "/media/stats"))

    async def index_status(self) -> dict[str, Any]:
        return await self._media_json("GET", "/media/index_status")

    async def list_sessions(self) -> dict[str, Any]:
        raw = await self._media_json("GET", "/media/sessions")
        sessions = raw.get("sessions", raw.get("items", []))
        return {**raw, "sessions": sessions}

    async def get_session(self, session_id: str) -> dict[str, Any]:
        return await self._media_json("GET", f"/media/sessions/{quote(session_id, safe='')}")

    async def _raw_files(self, params: dict[str, Any]) -> dict[str, Any]:
        raw = await self._media_json("GET", "/media/files", params=params)
        items = raw.get("items", raw.get("files", []))
        return {**raw, "files": [normalize_file(f) for f in items if isinstance(f, dict)]}

    async def list_files(
        self,
        limit: int = 50,
        cursor: str | None = None,
        file_type: str | None = None,
        status: str | None = None,
        session_id: str | None = None,
        *,
        include_aux: bool = False,
    ) -> dict[str, Any]:
        """One page of the album, newest first: `{"ok", "files", "next_cursor", "has_more"}`.

        By default the firmware's sidecar entries (`thumbnail` JPEGs, `lrv` low-res MP4 proxies) are folded into
        their video as `thumbnail_file_id` / `proxy_file_id` instead of being listed (`include_aux=True` lists
        them). The page is then read with a larger raw limit so roughly `limit` visible items come back.
        """
        fold = not include_aux and file_type not in AUX_FILE_TYPES
        raw_limit = min(500, max(1, limit) * (3 if fold and not file_type else 1))
        params: dict[str, Any] = {"limit": raw_limit}
        for key, val in (("cursor", cursor), ("file_type", file_type), ("status", status), ("session_id", session_id)):
            if val:
                params[key] = val
        page = await self._raw_files(params)
        files: list[dict[str, Any]] = page["files"]
        if fold:
            aux = [f for f in files if f["file_type"] in AUX_FILE_TYPES]
            files = [f for f in files if f["file_type"] not in AUX_FILE_TYPES]
            stems_missing = any(f["file_type"] == "video" for f in files) and (file_type or not aux)
            if stems_missing:  # filtered page: sidecars are not in it, fetch them separately
                thm, lrv = await asyncio.gather(
                    self._raw_files({"limit": 500, "file_type": "thumbnail"}),
                    self._raw_files({"limit": 500, "file_type": "lrv"}),
                )
                aux = thm["files"] + lrv["files"]
            link_aux_files(files, aux)
        return {
            "ok": True,
            "files": files,
            "count": len(files),
            "next_cursor": page.get("next_cursor"),
            "has_more": bool(page.get("has_more", page.get("next_cursor"))),
        }

    async def get_file(self, file_id: str) -> dict[str, Any]:
        return normalize_file(await self._media_json("GET", f"/media/files/{quote(str(file_id), safe='')}"))

    async def download_content(self, file_id: str, byte_range: str | None = None) -> tuple[bytes, str]:
        """Download full or partial bytes and return `(bytes, content_type)`."""
        headers = {"Range": byte_range} if byte_range else {}
        resp = await self._send("GET", f"/media/files/{quote(str(file_id), safe='')}/content", headers=headers)
        return resp.content, resp.headers.get("content-type", "application/octet-stream")

    async def _open_stream(self, path: str, headers: dict[str, str]) -> httpx.Response:
        try:
            req = self._client.build_request("GET", path, headers=self._auth_headers(headers))
            try:
                return await self._client.send(req, stream=True)
            except (httpx.RemoteProtocolError, httpx.ReadError):
                await self._request("HEAD", path, headers=self._auth_headers())  # shared logic decides about TLS
                req = self._client.build_request("GET", path, headers=self._auth_headers(headers))
                return await self._client.send(req, stream=True)
        except httpx.HTTPError as exc:
            raise unreachable_error(self._endpoint, path, exc) from exc

    @asynccontextmanager
    async def open_content_stream(self, file_id: str, byte_range: str | None = None) -> AsyncIterator[httpx.Response]:
        """Streaming GET of a media file (honours Range). Yields the open response: forward `status_code`,
        `headers` (Content-Range, Content-Length, Accept-Ranges, Content-Type) and iterate `aiter_bytes()`."""
        headers = {"Range": byte_range} if byte_range else {}
        path = f"/media/files/{quote(str(file_id), safe='')}/content"
        resp = await self._open_stream(path, headers)
        while resp.status_code in (401, 403) and self.tokens.rotate():
            await resp.aclose()
            resp = await self._open_stream(path, headers)
        try:
            if resp.status_code >= 400:
                try:
                    await resp.aread()
                except httpx.HTTPError:
                    pass
                self._check(resp, path)
            yield resp
        finally:
            await resp.aclose()

    async def stream_content(self, file_id: str, chunk_size: int = 65536, byte_range: str | None = None) -> AsyncIterator[bytes]:
        async with self.open_content_stream(file_id, byte_range) as resp:
            async for chunk in resp.aiter_bytes(chunk_size=chunk_size):
                yield chunk

    async def delete_file(self, file_id: str) -> dict[str, Any]:
        res = await self._media_json("DELETE", f"/media/files/{quote(str(file_id), safe='')}")
        return {"ok": True, "file_id": str(file_id), **res}


class OtaHttpClient(_HttpServiceClient):
    """Client for the robot's local firmware upload HTTP API (plain HTTP, no TLS upgrade)."""

    def __init__(self, host: str, port: int = 8088, timeout: float = 60.0, *, local_address: str | None = None) -> None:
        super().__init__(host, port, timeout, local_address)

    async def list_packages(self) -> dict[str, Any]:
        return await self._json("GET", "/list")

    async def upload_package(self, filename: str, content: bytes) -> dict[str, Any]:
        md5_hex = await asyncio.to_thread(lambda: hashlib.md5(content).hexdigest())  # firmware images are large
        files = {"file": (filename, content, "application/octet-stream")}
        data = {"md5": md5_hex, "size": str(len(content))}
        return await self._json("POST", "/upload", data=data, files=files)

    async def upload_file_path(self, path: str | Path) -> dict[str, Any]:
        p = Path(path)
        return await self.upload_package(p.name, await asyncio.to_thread(p.read_bytes))

    async def create_v2_session(self, filename: str, size: int, sha256: str = "") -> dict[str, Any]:
        return await self._json("POST", "/v2/upgrades", json={"filename": filename, "size": size, "sha256": sha256})

    async def upload_v2_payload(self, upgrade_id: str, content: bytes) -> dict[str, Any]:
        return await self._json(
            "PUT",
            f"/v2/upgrades/{upgrade_id}/upload",
            content=content,
            headers={"Content-Type": "application/octet-stream"},
        )

    async def cancel_v2(self, upgrade_id: str) -> dict[str, Any]:
        return await self._json("POST", f"/v2/upgrades/{upgrade_id}/cancel")
