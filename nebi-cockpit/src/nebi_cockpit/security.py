"""Request hardening for the cockpit: Host (DNS rebinding), Origin/Referer (CSWSH + CSRF) and token checks.

The cockpit drives a physical robot, so any web page the user happens to visit must not be able to
reach it through the user's browser:

* **Host check** (HTTP + WebSocket): only loopback names, IP literals, the bound host, this machine's
  hostname (`name`, `name.local`) and `PYNEBI_ALLOWED_HOSTS` are accepted. A DNS-rebinding page
  (`evil.example` resolving to 127.0.0.1) arrives with `Host: evil.example` and is refused.
* **Origin check**: every WebSocket handshake, and every state-changing HTTP request
  (POST/PUT/PATCH/DELETE) that carries an `Origin` (or, failing that, a `Referer`), must come from an
  allowed origin: the cockpit itself (same host:port, or its loopback aliases), origins registered by
  extensions (`ServerExtensionContext.add_allowed_origins`) and `PYNEBI_ALLOWED_ORIGINS` (comma separated).
  Requests without Origin/Referer are non-browser clients (curl, the `nebi` CLI, nebi's drive loop on
  `ws://127.0.0.1:8000/ws/control`) and stay allowed: a browser always sends Origin on WebSockets and on
  cross-origin POSTs.
* **Token** (only when `nebi-cockpit serve --host` binds a non-loopback address): requests from non-loopback
  peers must present the token via `X-Pynebi-Token`, `?token=` or the `pynebi_token` cookie (set by
  `static/auth.js`). `/`, `/static/*` and `/favicon.ico` are exempt so the page can load and pick the
  token up from its URL. Loopback peers (local CLI, local helper services) never need it; the Host/Origin checks
  already cover local browsers.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import os
import secrets
import socket
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

LOOPBACK_NAMES = frozenset({"localhost", "127.0.0.1", "::1"})
STATE_CHANGING = frozenset({"POST", "PUT", "PATCH", "DELETE"})
TOKEN_HEADER = "x-pynebi-token"
TOKEN_COOKIE = "pynebi_token"
_TOKEN_EXEMPT_PATHS = frozenset({"/", "/favicon.ico"})
WS_POLICY_VIOLATION = 1008
_TESTCLIENT_PEER = "testclient"  # starlette.testclient's synthetic peer; never seen on a real socket


def _split_env(name: str) -> list[str]:
    return [p.strip() for p in os.environ.get(name, "").split(",") if p.strip()]


def is_loopback_host(host: str) -> bool:
    h = (host or "").strip("[]").lower()
    if h in LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


def split_host_port(hostport: str) -> tuple[str, int | None]:
    hp = (hostport or "").strip().lower()
    if hp.startswith("["):  # [v6]:port
        end = hp.find("]")
        host = hp[1:end] if end > 0 else hp.strip("[]")
        rest = hp[end + 1 :] if end > 0 else ""
        port = int(rest[1:]) if rest.startswith(":") and rest[1:].isdigit() else None
        return host, port
    if hp.count(":") == 1:
        host, _, p = hp.partition(":")
        return host, int(p) if p.isdigit() else None
    return hp, None  # bare host or bare IPv6 without brackets


def _origin_tuple(origin: str) -> tuple[str, str, int | None] | None:
    try:
        u = urlsplit(origin.strip())
    except ValueError:
        return None
    if u.scheme not in ("http", "https", "ws", "wss") or not u.hostname:
        return None
    try:
        port = u.port
    except ValueError:
        return None
    scheme = {"ws": "http", "wss": "https"}.get(u.scheme, u.scheme)
    if port is None:
        port = 443 if scheme == "https" else 80
    return scheme, u.hostname.lower(), port


@dataclass
class SecurityConfig:
    bind_host: str = "127.0.0.1"
    token: str | None = None
    allowed_hosts: set[str] = field(default_factory=set)
    allowed_origins: set[str] = field(default_factory=set)

    @classmethod
    def from_env(cls, bind_host: str = "127.0.0.1", token: str | None = None) -> SecurityConfig:
        hosts = {h.lower() for h in _split_env("PYNEBI_ALLOWED_HOSTS")}
        if bind_host:
            hosts.add(bind_host.strip("[]").lower())
        try:
            name = socket.gethostname().lower()
            if name:
                hosts.update({name, name.split(".")[0], f"{name.split('.')[0]}.local"})
        except OSError:
            pass
        origins = set(_split_env("PYNEBI_ALLOWED_ORIGINS"))
        return cls(bind_host=bind_host, token=token or None, allowed_hosts=hosts, allowed_origins=origins)

    @property
    def requires_token(self) -> bool:
        return bool(self.token)

    # -- checks -------------------------------------------------------------------------------------

    def host_allowed(self, host_header: str, peer: str | None = None) -> bool:
        host, _ = split_host_port(host_header)
        if not host:
            return True  # HTTP/1.0 without Host: not a browser
        if is_loopback_host(host) or _is_ip_literal(host):
            return True  # rebinding needs a DNS name; an IP literal origin is the real origin
        if host in self.allowed_hosts:
            return True
        return host == "testserver" and peer == _TESTCLIENT_PEER

    def origin_allowed(self, origin: str, host_header: str, server_port: int | None, peer: str | None = None) -> bool:
        o = _origin_tuple(origin)
        if o is None:
            return False  # includes the opaque "null" origin (sandboxed iframes, file://)
        scheme, ohost, oport = o
        for extra in self.allowed_origins:
            if _origin_tuple(extra) == o:
                return True
        h, hport = split_host_port(host_header)
        if hport is None:
            hport = server_port or (443 if scheme == "https" else 80)
        if ohost == h and oport == hport and self.host_allowed(host_header, peer):
            return True  # same origin as the page we served
        # loopback aliases of this very server (localhost:8000 <-> 127.0.0.1:8000 <-> [::1]:8000)
        ports = {p for p in (hport, server_port) if p}
        return is_loopback_host(ohost) and oport in ports and is_loopback_host(h)

    def token_ok(self, presented: Iterable[str | None]) -> bool:
        if not self.token:
            return True
        expected = self.token.encode()
        return any(value and hmac.compare_digest(value.encode(), expected) for value in presented)


def generate_token() -> str:
    return secrets.token_urlsafe(18)


def _token_exempt(path: str) -> bool:
    return path in _TOKEN_EXEMPT_PATHS or path.startswith("/static/")


class SecurityMiddleware:
    """Pure ASGI middleware (covers WebSocket handshakes, which `BaseHTTPMiddleware` does not)."""

    def __init__(self, app: ASGIApp, get_config: Callable[[], SecurityConfig]) -> None:
        self.app = app
        self.get_config = get_config

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        kind = scope.get("type")
        if kind not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        reason = self._check(scope, self.get_config())
        if reason is None:
            await self.app(scope, receive, send)
            return
        if kind == "websocket":
            msg = await receive()  # websocket.connect
            if msg.get("type") == "websocket.connect":
                await send({"type": "websocket.close", "code": WS_POLICY_VIOLATION, "reason": reason})
            return
        body = json.dumps({"ok": False, "detail": reason}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 403,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
            }
        )
        await send({"type": "http.response.body", "body": body})

    @staticmethod
    def _check(scope: Scope, cfg: SecurityConfig) -> str | None:
        headers: dict[str, str] = {}
        cookies = ""
        for k, v in scope.get("headers") or []:
            name = k.decode("latin-1").lower()
            val = v.decode("latin-1")
            if name == "cookie":
                cookies = f"{cookies}; {val}" if cookies else val
            else:
                headers[name] = val
        client = scope.get("client")
        peer = client[0] if client else None
        host_header = headers.get("host", "")
        if not cfg.host_allowed(host_header, peer):
            return "Host header not allowed (DNS rebinding protection)"

        server = scope.get("server")
        server_port = server[1] if server and len(server) > 1 else None
        origin = headers.get("origin")
        is_ws = scope.get("type") == "websocket"
        method = scope.get("method", "GET").upper()
        if is_ws or method in STATE_CHANGING:
            src = origin
            if src is None and not is_ws:
                ref = headers.get("referer")
                if ref:
                    u = urlsplit(ref)
                    src = f"{u.scheme}://{u.netloc}" if u.scheme and u.netloc else "null"
            if src is not None and not cfg.origin_allowed(src, host_header, server_port, peer):
                return "Cross-origin request refused"

        needs_token = cfg.requires_token and not _token_exempt(scope.get("path", ""))
        if needs_token and not (peer and is_loopback_host(peer)):
            qs = parse_qs((scope.get("query_string") or b"").decode("latin-1"))
            cookie_tok = None
            for part in cookies.split(";"):
                k, _, v = part.strip().partition("=")
                if k == TOKEN_COOKIE:
                    cookie_tok = unquote(v)
            if not cfg.token_ok([headers.get(TOKEN_HEADER), *(qs.get("token") or []), cookie_tok]):
                return "Missing or invalid access token"
        return None


# --- response security headers (clickjacking, MIME sniffing, referrer leaks) ---

_SAFE_HOST_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-:[]")
# Robot media/video must not be embeddable by other sites (e.g. as <img>/<video> pulling the camera feed).
CORP_PATH_PREFIXES = ("/api/video/", "/api/media/")


def _csp_origin(origin: str) -> str | None:
    """`scheme://host[:port]` safe to splice into a CSP source list, else None."""
    o = origin.strip().rstrip("/")
    scheme, sep, rest = o.partition("://")
    if not sep or scheme.lower() not in ("http", "https", "ws", "wss") or not rest:
        return None
    return o if all(c in _SAFE_HOST_CHARS for c in rest) else None


def content_security_policy(host: str = "", extra_origins: Iterable[str] = ()) -> str:
    """CSP for the cockpit UI. Scripts only from this origin (no inline scripts / eval); inline *style
    attributes* are allowed because the UI builds markup with `style="..."` (bar heights, colour swatches).
    WebSockets to the same host are listed explicitly for browsers whose `'self'` does not cover ws(s)://.

    `extra_origins` are the trusted local services the page may fetch from, show images/video of and embed:
    the allowed origins registered by extensions (e.g. a 3D map served by an extension) and `PYNEBI_ALLOWED_ORIGINS`.
    """
    peers = " ".join(sorted({o for o in map(_csp_origin, extra_origins) if o}))
    connect = "'self'"
    if host and all(c in _SAFE_HOST_CHARS for c in host):
        connect += f" ws://{host} wss://{host}"
    if peers:
        connect += f" {peers}"
    tail = f" {peers}" if peers else ""
    return "; ".join(
        (
            "default-src 'self'",
            "script-src 'self'",
            "style-src 'self' 'unsafe-inline'",
            f"img-src 'self' data: blob:{tail}",
            f"media-src 'self' blob:{tail}",
            "font-src 'self' data:",
            f"connect-src {connect}",
            f"frame-src 'self'{tail}",
            "worker-src 'self' blob:",
            "object-src 'none'",
            "base-uri 'self'",
            "form-action 'self'",
            "frame-ancestors 'none'",
        )
    )


class SecurityHeadersMiddleware:
    """Pure ASGI middleware adding security headers to every HTTP response (including 403s from
    `SecurityMiddleware`, when added after it): the UI drives a physical robot, so it must never be framed
    (clickjacking), sniffed into another content type, or leak its URL (which may carry `?token=`)."""

    def __init__(self, app: ASGIApp, get_config: Callable[[], SecurityConfig | None] | None = None) -> None:
        self.app = app
        self.get_config = get_config

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        host = ""
        for k, v in scope.get("headers") or []:
            if k.lower() == b"host":
                host = v.decode("latin-1").strip()
                break
        path = str(scope.get("path") or "")
        extra: list[tuple[bytes, bytes]] = [
            (b"x-frame-options", b"DENY"),
            (b"content-security-policy", content_security_policy(host, self._peer_origins()).encode("latin-1")),
            (b"x-content-type-options", b"nosniff"),
            (b"referrer-policy", b"no-referrer"),
        ]
        if path.startswith(CORP_PATH_PREFIXES):
            extra.append((b"cross-origin-resource-policy", b"same-origin"))
        names = {k for k, _ in extra}

        async def send_with_headers(message: dict[str, Any]) -> None:
            if message.get("type") == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers") or [] if k.lower() not in names]
                message = {**message, "headers": [*headers, *extra]}
            await send(message)

        await self.app(scope, receive, send_with_headers)

    def _peer_origins(self) -> Iterable[str]:
        cfg = self.get_config() if self.get_config is not None else None
        return cfg.allowed_origins if cfg is not None else ()
