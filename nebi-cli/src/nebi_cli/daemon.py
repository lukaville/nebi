"""`nebi` agent API on the cockpit: the `/agent/*` endpoints and the standalone daemon process.

The agent never runs a server of its own: it attaches to a nebi-cockpit FastAPI app, so a human can watch the
live FPV cockpit at the same URL while an agent drives through the CLI, over the one (single-client) robot link.

  * embedded: nebi-cli's ``nebi.extensions`` entry point (`nebi_cli.cockpit_ext`) mounts the agent API in every
    `nebi-cockpit serve` (``embedded=True``: `nebi down` does not stop the user's cockpit);
  * standalone daemon (`nebi up` spawns it when no cockpit is running): the cockpit app + the agent API.

Run directly:  python -m nebi_cli.daemon --port 8765
"""

from __future__ import annotations

import argparse
import contextvars
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Body, FastAPI

from nebi_cli.runtime import AgentRuntime
from nebi_cli.settings import CACHE_DIR, DEFAULT_PORT
from nebi_cockpit.app import create_app
from nebi_cockpit.extensions import ServerExtensionContext

__all__ = ["AGENT_EXTENSION_NAME", "DEFAULT_PORT", "attach_agent", "create_agent_app", "main"]

AGENT_EXTENSION_NAME = "nebi-cli"

# Set while `create_agent_app` builds its cockpit app: the entry-point extension then leaves the agent to it.
_standalone_build: contextvars.ContextVar[bool] = contextvars.ContextVar("nebi_agent_standalone_build", default=False)


def attach_agent(ctx: ServerExtensionContext, *, port: int, embedded: bool) -> AgentRuntime:
    """Mount `/agent/call|ping|activity` on the cockpit app and run an AgentRuntime for the app's lifetime."""
    app = ctx.app
    existing = getattr(app.state, "agent", None)
    if existing is not None:
        return existing
    runtime = AgentRuntime(ctx.manager, port=port, embedded=embedded)
    app.state.agent = runtime

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await runtime.start()
        try:
            yield
        finally:
            await runtime.stop()

    ctx.add_lifespan(lifespan)

    router = APIRouter(prefix="/agent", tags=["agent"])

    @router.post("/call")
    async def agent_call(payload: Annotated[dict[str, Any], Body(...)]) -> dict[str, Any]:
        return await runtime.call(str(payload.get("command", "")), dict(payload.get("args") or {}))

    @router.get("/ping")
    async def agent_ping() -> dict[str, Any]:
        return {"ok": True, "pid": os.getpid(), "agent": True, "embedded": embedded}

    @router.get("/activity")
    async def agent_activity() -> dict[str, Any]:
        """For the cockpit's 'agent driving' indicator."""
        return runtime.activity_snapshot()

    ctx.include_router(router)
    return runtime


def setup_embedded(ctx: ServerExtensionContext) -> None:
    """`setup_server` of nebi-cli's cockpit extension: host the agent API in the user's cockpit."""
    if _standalone_build.get():
        return  # create_agent_app attaches its own (non-embedded) agent
    attach_agent(ctx, port=ctx.port or int(os.environ.get("PYNEBI_PORT", "8000")), embedded=True)


def create_agent_app(port: int = DEFAULT_PORT, *, start_simulator: bool = False, embedded: bool = False) -> tuple[FastAPI, AgentRuntime]:
    """A cockpit app with the agent API. embedded=True: behaves as the agent inside the user's cockpit."""
    token = _standalone_build.set(True)
    try:
        app = create_app(start_simulator=start_simulator, port=port)
    finally:
        _standalone_build.reset(token)
    runtime = attach_agent(ServerExtensionContext(AGENT_EXTENSION_NAME, app), port=port, embedded=embedded)
    return app, runtime


def main() -> None:
    import uvicorn  # server-only dependency

    ap = argparse.ArgumentParser(prog="nebi-daemon")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=int(os.environ.get("NEBI_PORT", DEFAULT_PORT)))
    ap.add_argument("--pidfile", default=str(CACHE_DIR / "daemon.pid"))
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    app, runtime = create_agent_app(args.port)
    server = uvicorn.Server(uvicorn.Config(app, host=args.host, port=args.port, log_level="warning"))

    def _exit() -> None:
        server.should_exit = True

    runtime.set_shutdown_callback(_exit)

    pid = Path(args.pidfile)
    pid.parent.mkdir(parents=True, exist_ok=True)
    pid.write_text(str(os.getpid()))
    try:
        server.run()
    finally:
        try:
            if pid.read_text().strip() == str(os.getpid()):
                pid.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    main()
