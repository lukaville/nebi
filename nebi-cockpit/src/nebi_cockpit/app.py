"""FastAPI Web & Control Server bridging browser clients to `AsyncNebiClient` across all local Nebi flows.

`create_app()` wires the pieces together:
  * `connection_manager.NebiServerManager` - the single robot link, simulator and video hub
    (on `app.state.manager`; routes get it and the connected client through `deps`)
  * `routes_connection` / `routes_motion` / `routes_camera` / `routes_media` / `routes_system` / `routes_voice` - REST
  * `ws_routes` - `/ws/control` (dead-man joystick), `/ws/video`, `/ws/intercom`
  * `errors` - uniform JSON error bodies (`NebiError` -> status + connection summary)
  * `security.SecurityMiddleware` - Host / Origin (CSWSH, CSRF) / token checks
  * `security.SecurityHeadersMiddleware` - CSP / X-Frame-Options (clickjacking), nosniff, no-referrer, CORP
  * `extensions` - loaded nebi extensions add routes, static files, cockpit UI modules and allowed origins
    (`GET /api/extensions` lists them for the page)

Run with `nebi-cockpit serve` (or `uvicorn nebi_cockpit.app:create_app --factory`). Installed extensions plug in
through `nebi_cockpit.extensions` (e.g. nebi-cli mounts the `nebi` agent API `/agent/*` this way).
"""

from __future__ import annotations

import importlib.metadata
import os
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response
from starlette.types import Scope

from nebi_cockpit import (
    routes_camera,
    routes_connection,
    routes_eyes,
    routes_media,
    routes_motion,
    routes_system,
    ws_routes,
)
from nebi_cockpit.connection_manager import NebiServerManager
from nebi_cockpit.errors import http_status_for_nebi_error, install_exception_handlers
from nebi_cockpit.extensions import enter_extension_lifespans, extensions_payload, server_state, setup_server_extensions
from nebi_cockpit.intercom_ws import IntercomHub
from nebi_cockpit.paths import STATIC_DIR
from nebi_cockpit.routes_voice import router as voice_router
from nebi_cockpit.schemas import ConnectionSwitchBody
from nebi_cockpit.security import SecurityConfig, SecurityHeadersMiddleware, SecurityMiddleware


def _distribution_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:  # source tree that was never installed
        return "0+unknown"


__all__ = ["STATIC_DIR", "ConnectionSwitchBody", "NebiServerManager", "configure_security", "create_app"]

APP_TITLE = "nebi-cockpit — robot control & live FPV server (compatible with the Beni robot)"
APP_VERSION = _distribution_version("nebi-cockpit")  # single source: pyproject.toml

# backwards-compatible private alias
_http_status_for_nebi_error = http_status_for_nebi_error


class CockpitStaticFiles(StaticFiles):
    """`/static` with `Cache-Control: no-cache`: the cockpit's ES modules import each other without
    version queries, so browsers must revalidate (cheap 304 via ETag) instead of mixing stale modules."""

    def file_response(
        self, full_path: str | os.PathLike[str], stat_result: os.stat_result, scope: Scope, status_code: int = 200
    ) -> Response:
        response = super().file_response(full_path, stat_result, scope, status_code)
        response.headers["Cache-Control"] = "no-cache"
        return response


def configure_security(app: FastAPI, *, bind_host: str = "127.0.0.1", token: str | None = None) -> SecurityConfig:
    """(Re)configure the Host/Origin/token checks for how the server is actually exposed."""
    cfg = SecurityConfig.from_env(bind_host=bind_host, token=token)
    cfg.allowed_origins.update(server_state(app).origins)  # registered by extensions
    app.state.security = cfg
    return cfg


def create_app(
    start_simulator: bool = False, *, bind_host: str = "127.0.0.1", token: str | None = None, port: int | None = None
) -> FastAPI:
    """`port`: where this app will be served (extensions read it as ``ctx.port``, e.g. for URLs they report)."""
    manager = NebiServerManager(start_simulator=start_simulator)

    @asynccontextmanager
    async def lifespan(app_: FastAPI) -> AsyncIterator[None]:
        await manager.startup()
        try:
            async with AsyncExitStack() as stack:  # extension lifespans: entered after the robot link, left before it
                await enter_extension_lifespans(app_, stack)
                yield
        finally:
            await manager.shutdown()

    api = FastAPI(title=APP_TITLE, version=APP_VERSION, lifespan=lifespan)
    api.state.manager = manager
    api.state.port = port
    api.state.intercom_hub = IntercomHub(lambda: manager.client)
    configure_security(api, bind_host=bind_host, token=token)
    api.add_middleware(SecurityMiddleware, get_config=lambda: api.state.security)
    api.add_middleware(SecurityHeadersMiddleware, get_config=lambda: api.state.security)  # outermost: also covers SecurityMiddleware's 403s
    install_exception_handlers(api)

    for module in (routes_connection, ws_routes, routes_motion, routes_camera, routes_system, routes_eyes, routes_media):
        api.include_router(module.router)
    api.include_router(voice_router)  # text-to-speech on the robot speaker (`/api/voice`)

    if STATIC_DIR.exists():
        api.mount("/static", CockpitStaticFiles(directory=str(STATIC_DIR)), name="static")

    @api.get("/api/extensions", tags=["status"])
    async def api_extensions() -> dict[str, Any]:
        """Loaded nebi extensions (name, version, ok, errors, commands) and the cockpit UI modules they add."""
        return extensions_payload(api)

    setup_server_extensions(api)

    @api.get("/", include_in_schema=False)
    async def root_ui() -> FileResponse:
        return FileResponse(str(STATIC_DIR / "index.html"), headers={"Cache-Control": "no-cache"})

    return api
