"""Robot bridge connecting `nebi-ai` to Nebi in-process or via a running `nebi-cockpit` / `nebi` daemon."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RobotTarget:
    """Connection target for `RobotBridge`."""

    sim: bool = False
    host: str = ""
    flow: str = ""
    interface: str = ""
    daemon_url: str = ""

    @property
    def label(self) -> str:
        if self.daemon_url:
            return f"daemon:{self.daemon_url}"
        if self.sim:
            return "simulator"
        if self.host:
            return f"host:{self.host}"
        if self.flow:
            return f"flow:{self.flow}"
        return "auto"


class RobotBridge:
    """Unified async interface to `nebi-cli`'s safe `AgentRuntime` commands.

    Supports two modes:
      * **In-process** (`--sim`, `--host`, or `--flow`): owns a `NebiServerManager` + `AgentRuntime`
        directly so `nebi-ai run` works with a single command.
      * **Attached** (`--daemon-url`): forwards commands to an existing `nebi-cockpit serve` or
        `nebi` daemon's `POST /agent/call` endpoint so you can share the single robot link with the cockpit.
    """

    def __init__(self, target: RobotTarget | None = None) -> None:
        self.target = target or RobotTarget(flow="auto")
        self._manager: Any = None
        self._runtime: Any = None
        self._http: httpx.AsyncClient | None = None
        self._started = False
        self._last_snapshot_path: Path | None = None
        self._last_event_seq: int = 0

    @property
    def is_remote(self) -> bool:
        return bool(self.target.daemon_url)

    @property
    def last_snapshot_path(self) -> Path | None:
        return self._last_snapshot_path

    async def start(self) -> dict[str, Any]:
        """Connect to the robot (or verify connectivity to the remote daemon)."""
        if self._started:
            return await self.state()

        if self.is_remote:
            self._http = httpx.AsyncClient(base_url=self.target.daemon_url.rstrip("/"), timeout=60.0)
            resp = await self._http.get("/agent/ping")
            resp.raise_for_status()
            self._started = True
            return await self.state()

        from nebi_cli.runtime import AgentRuntime
        from nebi_cockpit.app import NebiServerManager

        self._manager = NebiServerManager(start_simulator=False)
        self._runtime = AgentRuntime(self._manager, embedded=False)
        await self._manager.startup()
        await self._runtime.start()
        self._started = True

        connect_args: dict[str, Any] = {}
        if self.target.sim:
            connect_args["sim"] = True
        elif self.target.host:
            connect_args["host"] = self.target.host
            if self.target.interface:
                connect_args["interface"] = self.target.interface
        else:
            connect_args["flow"] = self.target.flow or "auto"
            if self.target.interface:
                connect_args["interface"] = self.target.interface

        res = await self.call("connect", connect_args)
        if not res.get("ok"):
            logger.warning("Initial robot connect returned error: %s", res.get("error"))
        return res

    async def stop(self) -> None:
        """Stop motion safely and release the robot link."""
        if not self._started:
            return
        try:
            await self.call("stop", {})
        except Exception:
            pass
        if self._http is not None:
            await self._http.aclose()
            self._http = None
        if self._runtime is not None:
            try:
                await self._runtime.stop()
            finally:
                self._runtime = None
        if self._manager is not None:
            try:
                await self._manager.shutdown()
            finally:
                self._manager = None
        self._started = False

    async def call(self, command: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        """Execute one `nebi-cli` agent command and return its JSON response envelope."""
        payload = dict(args or {})
        if self._http is not None:
            resp = await self._http.post("/agent/call", json={"command": command, "args": payload})
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
        elif self._runtime is not None:
            data = await self._runtime.call(command, payload)
        else:
            raise RuntimeError("RobotBridge is not started; call `await bridge.start()` first.")

        if command == "look" and data.get("ok"):
            path_str = (data.get("result") or {}).get("path")
            if isinstance(path_str, str) and path_str:
                self._last_snapshot_path = Path(path_str)
        return data

    async def state(self) -> dict[str, Any]:
        """Fetch compact robot state."""
        return await self.call("state", {})

    async def poll_events(self) -> list[dict[str, Any]]:
        """Return new robot telemetry events since the previous poll."""
        res = await self.call("events", {"since": self._last_event_seq, "wait": 0.0})
        if not res.get("ok"):
            return []
        result = res.get("result") or {}
        next_since = result.get("next_since")
        if isinstance(next_since, int):
            self._last_event_seq = next_since
        events = result.get("events")
        return list(events) if isinstance(events, list) else []

    def read_last_snapshot_bytes(self) -> bytes | None:
        """Read the most recent JPEG captured via `look`, if available."""
        if self._last_snapshot_path is not None and self._last_snapshot_path.is_file():
            try:
                return self._last_snapshot_path.read_bytes()
            except OSError:
                return None
        return None
