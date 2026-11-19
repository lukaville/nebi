"""A test extension that uses every hook of the public extension API (nebi_cli.extensions + nebi_cockpit.extensions)."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter

from nebi_cli.extensions import (
    EXIT_TIMEOUT,
    AgentError,
    AgentExtensionContext,
    Arg,
    Args,
    CareContribution,
    Cmd,
    CommandGroup,
    command,
)
from nebi_cockpit.extensions import ServerExtensionContext

STATIC = Path(__file__).parent / "fake_ext_static"


class FakeSensing:
    """CarriedStateProvider + PeopleProvider."""

    def __init__(self) -> None:
        self.carried: bool | None = False
        self.people_list: list[dict[str, Any]] = [{"type": "person", "distance_m": 0.8, "bearing_deg": 20, "direction": "ahead"}]

    async def carried_state(self) -> tuple[bool | None, str]:
        return (self.carried, "fake") if self.carried is not None else (None, "fake sensor unknown")

    async def people(self) -> list[dict[str, Any]]:
        return list(self.people_list)


class FakeEvents:
    """EventSource."""

    name = "fake"
    description = "Fake test events."

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.seq = 0
        self.fail = False

    def push(self, kind: str, summary: str, **kw: Any) -> None:
        self.seq += 1
        self.events.append({"seq": self.seq, "t": time.time(), "kind": kind, "summary": summary, **kw})

    async def cursor(self) -> int:
        return self.seq

    async def poll(self, since: int, *, wait: float = 0.0, limit: int = 50) -> tuple[list[dict[str, Any]], int]:
        if self.fail:
            raise AgentError("FAKE_DOWN", "fake source down")
        t0 = time.monotonic()
        while True:
            evs = [e for e in self.events if e["seq"] > since][:limit]
            if evs or time.monotonic() - t0 >= wait:
                return evs, self.seq
            await asyncio.sleep(0.05)


class HelloCommands(CommandGroup):
    @command("hello")
    async def hello(self, a: Args) -> dict[str, Any]:
        sensing: FakeSensing = self.services["fake.sensing"]
        return {"summary": f"hello {a.get('who') or 'world'}", "carried": sensing.carried}


class FakeExtension:
    name = "fake"
    version = "1.2.3"

    def __init__(self) -> None:
        self.sensing = FakeSensing()
        self.events = FakeEvents()
        self.shutdowns = 0
        self.fail_server = False

    async def _look(self, a: Args) -> dict[str, Any]:
        path = Path(a.get("out") or "/tmp/fake-view.png")
        await asyncio.to_thread(path.write_bytes, b"\x89PNG\r\n\x1a\nfake")
        return {"path": str(path), "summary": "a fake view"}

    async def _wait(self, name: str, value: str, timeout: float) -> dict[str, Any]:
        since = self.events.seq
        evs, _ = await self.events.poll(since, wait=timeout)
        hit = next((e for e in evs if e["kind"] == "poke" and (not value or e.get("detail") == value)), None)
        if hit is None:
            raise AgentError("WAIT_TIMEOUT", f"no poke within {timeout:g}s", exit_code=EXIT_TIMEOUT)
        return {"met": True, "event": hit, "summary": hit["summary"]}

    async def _care(self) -> CareContribution:
        return {"fields": {"fake_temp_c": 50}, "needs": ["cool_down"], "advice": ["fake: too warm"]}

    async def _locate(self, target: str) -> tuple[float, str] | None:
        return (-30.0, "the fake sofa") if target == "sofa" else None

    async def _shutdown(self) -> None:
        self.shutdowns += 1

    def setup_agent(self, ctx: AgentExtensionContext) -> None:
        ctx.add_error_codes({"FAKE_DOWN": (3, "fake source down")})
        ctx.add_commands(
            [Cmd("hello", "fake", "Say hello (test).", (Arg("who", "str", "Name.", positional=True),), ("nebi hello Bob",))],
            HelloCommands,
            group_titles={"fake": "Fake (test)"},
        )
        ctx.add_service("fake.sensing", self.sensing)
        ctx.set_carried_state_provider(self.sensing)
        ctx.set_people_provider(self.sensing)
        ctx.add_event_source(self.events)
        ctx.add_look_view("fakeview", self._look, "a fake test view")
        ctx.add_wait_events({"poked": "a fake poke"}, self._wait)
        ctx.add_care_provider(self._care)
        ctx.add_target_locator(self._locate)
        ctx.on_shutdown(self._shutdown)

    def setup_server(self, ctx: ServerExtensionContext) -> None:
        router = APIRouter()

        @router.get("/api/fake/hello")
        async def hello() -> dict[str, Any]:
            return {"hello": True}

        ctx.include_router(router)
        ctx.mount_static("/ext/fake", STATIC)
        ctx.add_ui_module("/ext/fake/fake.js", styles=["/ext/fake/fake.css"])
        ctx.add_allowed_origins("http://127.0.0.1:9999")
        if self.fail_server:
            raise RuntimeError("server boom")

    def guide_markdown(self) -> str:
        return "Fake extension guide: `nebi hello`."


class BrokenExtension:
    """setup_agent fails halfway: its command must be rolled back."""

    name = "broken"

    def setup_agent(self, ctx: AgentExtensionContext) -> None:
        ctx.add_commands([Cmd("broken-cmd", "fake", "never")], None)
        raise RuntimeError("agent boom")
