"""Session commands: connect (`nebi up`), disconnect, shutdown (`nebi down`), status."""

from __future__ import annotations

import asyncio
import os
import time
from typing import TYPE_CHECKING, Any

from nebi_cli import spec as S
from nebi_cli.errors import AgentError
from nebi_cli.extensions import extension_report
from nebi_cli.motion import neutral
from nebi_cli.registry import Args, CommandGroup, command
from nebi_cockpit.app import ConnectionSwitchBody

if TYPE_CHECKING:
    from nebi_cli.runtime import AgentRuntime

CONNECT_SETTLE_S = 0.6  # let telemetry arrive so the first `state` after `up` is real, not defaults


class SessionCommands(CommandGroup):
    def __init__(self, rt: AgentRuntime) -> None:
        super().__init__(rt)
        self.target: str | None = None  # "sim" | "host:<ip>" | "flow:<name>" of the current connection

    def _status_core(self) -> dict[str, Any]:
        conn = self.manager.connection_summary()
        return {
            "connected": bool(conn.get("connected")),
            "flow": conn.get("flow"),
            "stage": conn.get("stage"),
            "host": conn.get("host"),
            "md_ts_active": conn.get("md_ts_active"),
            "last_error": conn.get("last_error"),
            "cockpit_url": f"http://127.0.0.1:{self.rt.port}/",
        }

    @command("connect")
    async def connect(self, a: Args) -> dict[str, Any]:
        sim, host, flow = bool(a.get("sim")), a.get("host") or "", a.get("flow") or ""
        iface = a.get("interface") or ""
        if sim:
            body = ConnectionSwitchBody(mode="simulator")
            target = "sim"
        elif host:
            body = ConnectionSwitchBody(mode="robot", host=host, bind_interface=iface)
            target = f"host:{host}"
        elif flow:
            body = ConnectionSwitchBody(mode=flow, flow=flow, bind_interface=iface)
            target = f"flow:{flow}"
        else:
            raise AgentError("NO_TARGET", "Say what to connect to.", hint="nebi up --sim | --host <ip> | --flow auto", exit_code=S.EXIT_USAGE)

        c = self.manager.client
        if c is not None and c.connected and self.target == target and not a.get("reconnect"):
            return {"target": target, "already_connected": True, **self._status_core()}
        self.motion.new_epoch()
        self.target = target
        await self.manager.switch_connection(body)
        self.motion.forget_kinematics()
        await asyncio.sleep(CONNECT_SETTLE_S)
        self.events.emit("connected", target=target)
        return {"target": target, "already_connected": False, **self._status_core()}

    @command("disconnect")
    async def disconnect(self, a: Args) -> dict[str, Any]:
        c = self.manager.client
        if c is not None and c.connected:
            self.motion.new_epoch()
            neutral(c)
        await self.manager.disconnect()
        self.target = None
        return {"disconnected": True}

    @command("shutdown")
    async def shutdown(self, a: Args) -> dict[str, Any]:
        if self.rt.embedded:
            self.motion.new_epoch()
            if self.manager.client is not None:
                neutral(self.manager.client)
            return {"shutting_down": False, "embedded": True,
                    "note": "The agent runs inside the `nebi-cockpit serve` cockpit; motion stopped, robot stays connected."}
        await self.disconnect(a)
        self.rt.request_shutdown()
        return {"shutting_down": True}

    @command("status")
    async def status(self, a: Args) -> dict[str, Any]:
        return {
            "daemon": {"pid": os.getpid(), "uptime_s": round(time.time() - self.rt.started_at, 1), "port": self.rt.port},
            "target": self.target,
            **self._status_core(),
            "extensions": extension_report(),
        }
