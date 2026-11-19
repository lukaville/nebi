"""The extension API: discovery, spec registration, agent hooks, server/UI hooks, failure isolation."""
from __future__ import annotations

import asyncio
import sys
import types

import pytest
from fastapi.testclient import TestClient

from fake_extension import BrokenExtension, FakeExtension
from nebi.extension_loader import extension_report, load_extensions, reset_extensions
from nebi_cli import cli as nebi_cli
from nebi_cli import spec as S
from nebi_cli.daemon import create_agent_app
from nebi_cockpit.app import create_app


def test_disabled_by_env_in_core_tests() -> None:
    reset_extensions()
    assert load_extensions() == []
    assert "hello" not in S.COMMANDS_BY_NAME


def test_env_spec_loading_and_load_errors(monkeypatch) -> None:
    mod = types.ModuleType("pynebi_test_ext_mod")
    mod.ext = FakeExtension()  # type: ignore[attr-defined]
    mod.Factory = FakeExtension  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pynebi_test_ext_mod", mod)
    monkeypatch.delenv("PYNEBI_DISABLE_EXTENSIONS")
    monkeypatch.setenv("PYNEBI_EXTENSIONS", "pynebi_test_ext_mod:ext, pynebi_test_ext_mod:Factory, no_such_module_xyz:ext")
    monkeypatch.setattr("nebi.extension_loader.metadata.entry_points", lambda group: [])  # hermetic: no installed ones
    try:
        reset_extensions()
        found = load_extensions()
        names = [(e.name, e.ok) for e in found]
        assert names == [("fake", True), ("no_such_module_xyz:ext", False)]  # the class duplicate is dropped
        report = extension_report()
        assert report[1]["errors"] and "ModuleNotFoundError" in report[1]["errors"][0]
    finally:
        reset_extensions()


def test_spec_contributions(extensions) -> None:
    extensions(FakeExtension())
    from nebi_cli.extensions import ensure_agent_spec

    ensure_agent_spec()
    assert S.COMMANDS_BY_NAME["hello"].group == "fake" and S.GROUP_TITLES["fake"] == "Fake (test)"
    view = next(a for a in S.COMMANDS_BY_NAME["look"].args if a.name == "view")
    assert view.choices == ("camera", "fakeview")
    assert "poked" in S.wait_event_names() and "poked" in S.COMMANDS_BY_NAME["wait"].notes
    events_args = {a.name: a for a in S.COMMANDS_BY_NAME["events"].args}
    assert "fake_since" in events_args and events_args["source"].choices == ("all", "robot", "fake")
    schema = S.schema_dict()
    assert schema["extension_commands"] == {"hello": "fake"}
    # the CLI parser, `nebi schema` and `nebi guide` see the extension
    ns = nebi_cli.build_parser().parse_args(["hello", "Bob"])
    assert ns.command == "hello" and ns.who == "Bob"
    assert "## Extension: fake" in nebi_cli.guide_text()
    # extensions cannot shadow core commands
    with pytest.raises(ValueError):
        S.register_extension_commands("evil", (S.Cmd("drive", "x", "no"),))
    reset_extensions()
    assert "hello" not in S.COMMANDS_BY_NAME and S.COMMANDS_BY_NAME["look"].args[-1].choices == ("camera",)


def test_agent_hooks_against_simulator(tmp_path, extensions) -> None:
    fake = FakeExtension()
    extensions(fake, BrokenExtension())
    app, _runtime = create_agent_app(port=0)
    with TestClient(app) as http:

        def call(cmd: str, **args):
            return http.post("/agent/call", json={"command": cmd, "args": args}).json()

        assert call("connect", sim=True)["ok"] and call("stand")["ok"]
        # commands + services
        r = call("hello", who="Bob")
        assert r["ok"] and r["result"]["summary"] == "hello Bob"
        # the broken extension was rolled back and is reported
        assert "broken-cmd" not in S.COMMANDS_BY_NAME and call("broken-cmd")["error"]["code"] == "UNKNOWN_COMMAND"
        status = call("status")["result"]["extensions"]
        by_name = {e["name"]: e for e in status}
        assert by_name["fake"]["ok"] and by_name["fake"]["version"] == "1.2.3" and by_name["fake"]["commands"] == ["hello"]
        assert not by_name["broken"]["ok"] and "agent boom" in by_name["broken"]["errors"][0]
        # look view
        r = call("look", view="fakeview", out=str(tmp_path / "v.png"))
        assert r["ok"] and r["result"]["view"] == "fakeview" and r["result"]["summary"] == "a fake view"
        # care fields
        r = call("care")
        assert r["result"]["fake_temp_c"] == 50 and "cool_down" in r["result"]["needs"]
        # look-at: people provider bearing, then a target locator
        r = call("body", gesture="look-at", silent=True)
        assert r["ok"] and "person" in r["result"]["target"], r
        r = call("body", gesture="look-at", target="sofa", silent=True)
        assert r["ok"] and r["result"]["target"] == "the fake sofa", r
        # carried provider
        fake.sensing.carried = True
        assert call("drive", direction="forward", distance=0.1)["error"]["code"] == "BEING_CARRIED"
        assert "being carried: stay still" in call("care")["result"]["advice"]
        fake.sensing.carried = False
        # event source in `events`
        fake.events.push("poke", "poked once", detail="soft")
        r = call("events", source="fake", fake_since=0)
        assert r["ok"] and r["result"]["fake_events"][0]["kind"] == "poke" and r["result"]["next_fake_since"] == 1
        assert r["result"]["summary"] == "poked once"
        r = call("events", since=0)  # "all": robot + fake
        assert "events" in r["result"] and "fake_events" in r["result"]
        fake.events.fail = True
        r = call("events", since=0)
        assert r["ok"] and any("fake events unavailable (FAKE_DOWN)" in w for w in r["warnings"])
        r = call("events", source="fake")
        assert r["error"]["code"] == "FAKE_DOWN" and r["exit_code"] == 3  # extension error code catalogued
        fake.events.fail = False
        # wait --for extension event
        assert call("wait", **{"for": "poked", "timeout": 0.3})["exit_code"] == S.EXIT_TIMEOUT
        http.portal.call(_later, lambda: fake.events.push("poke", "poked again", detail="hard"))
        r = call("wait", **{"for": "poked=hard", "timeout": 3})
        assert r["ok"] and r["result"]["summary"] == "poked again"
        # idle reacts to extension events
        r = call("idle", action="start", silent=True)
        assert "fake events" in r["result"]["summary"]
        call("idle", action="stop")
        assert call("sit")["ok"]
    assert fake.shutdowns == 1


async def _later(fn) -> None:
    asyncio.get_running_loop().call_later(0.3, fn)


def test_server_hooks(extensions) -> None:
    extensions(FakeExtension())
    app = create_app(start_simulator=False)
    with TestClient(app) as client:
        exts = client.get("/api/extensions").json()["extensions"]
        assert exts[0]["name"] == "fake" and exts[0]["ui_modules"] == ["/ext/fake/fake.js"]
        assert exts[0]["styles"] == ["/ext/fake/fake.css"]
        assert client.get("/api/fake/hello").json() == {"hello": True}
        js = client.get("/ext/fake/fake.js")
        assert js.status_code == 200 and "registerTab" in js.text and js.headers["cache-control"] == "no-cache"
        # registered origin is allowed, others are not
        assert client.post("/api/connection/disconnect", headers={"origin": "http://127.0.0.1:9999"}).status_code == 200
        assert client.post("/api/connection/disconnect", headers={"origin": "http://127.0.0.1:9998"}).status_code == 403
        assert "/api/nav/config" not in {getattr(r, "path", "") for r in app.routes}


def test_server_setup_failure_is_isolated(extensions) -> None:
    fake = FakeExtension()
    fake.fail_server = True
    extensions(fake)
    app = create_app(start_simulator=False)
    with TestClient(app) as client:
        ext = client.get("/api/extensions").json()["extensions"][0]
        assert not ext["ok"] and "server boom" in ext["errors"][0] and ext["ui_modules"] == []
        assert client.get("/api/health").json()["ok"]


def test_core_server_has_no_extensions() -> None:
    reset_extensions()
    app = create_app(start_simulator=False)
    with TestClient(app) as client:
        assert client.get("/api/extensions").json() == {"extensions": []}
        page = client.get("/").text
        assert "nav.js" not in page and "tab-explore" not in page and "map-frame" not in page
