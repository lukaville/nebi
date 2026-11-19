"""The agent API attaches to the cockpit through nebi-cli's `nebi.extensions` entry point (no cockpit -> cli import)."""

from __future__ import annotations

from importlib import metadata

from fastapi.testclient import TestClient

from nebi_cli import cli as nebi_cli
from nebi_cli import cockpit_ext, tools
from nebi_cli.daemon import create_agent_app
from nebi_cockpit.app import create_app


def test_entry_point_is_declared() -> None:
    eps = {ep.name: ep.value for ep in metadata.entry_points(group="nebi.extensions")}
    assert eps.get("nebi-cli") == "nebi_cli.cockpit_ext:extension"


def test_cockpit_hosts_embedded_agent_via_extension(extensions) -> None:
    extensions(cockpit_ext.extension)
    app = create_app(start_simulator=True, port=8123)
    with TestClient(app) as tc:
        ping = tc.get("/agent/ping").json()
        assert ping["agent"] is True and ping["embedded"] is True
        assert tc.get("/agent/activity").status_code == 200
        exts = {e["name"]: e for e in tc.get("/api/extensions").json()["extensions"]}
        assert exts["nebi-cli"]["ok"] is True
        status = tc.post("/agent/call", json={"command": "status", "args": {}}).json()
        assert status["ok"] is True
        assert status["result"]["daemon"]["port"] == 8123
        # embedded: `nebi down` stops motion but must not shut the user's cockpit down
        down = tc.post("/agent/call", json={"command": "shutdown", "args": {}}).json()
        assert down["ok"] is True and down["result"]["embedded"] is True


def test_cockpit_without_the_extension_has_no_agent_api(extensions) -> None:
    extensions()
    app = create_app(start_simulator=False)
    with TestClient(app) as tc:
        assert tc.get("/agent/ping").status_code == 404


def test_standalone_daemon_attaches_once_even_with_extension_loaded(extensions) -> None:
    extensions(cockpit_ext.extension)
    app, runtime = create_agent_app(8124, start_simulator=True)
    assert runtime.embedded is False
    assert app.state.agent is runtime  # the entry-point extension left the agent to create_agent_app
    with TestClient(app) as tc:
        assert tc.get("/agent/ping").json()["embedded"] is False


def test_tool_routing() -> None:
    assert nebi_cli._tool_argv(["tools", "status", "--flow", "auto"]) == (["status", "--flow", "auto"], "nebi tools")
    assert nebi_cli._tool_argv(["setup-wifi", "--ssid", "x"]) == (["setup-wifi", "--ssid", "x"], "nebi")
    assert nebi_cli._tool_argv(["status"]) is None  # the agent's `status`
    assert nebi_cli._tool_argv(["say", "hi"]) is None  # the agent's `say`
    p = tools.build_parser()
    for name in tools.TOOL_NAMES:
        assert p.parse_args([name] + (["x"] if name == "say" else [])).cmd == name
    assert "serve" not in tools.TOOL_NAMES


def test_nebi_say_and_cockpit_speak_share_one_speaker(extensions) -> None:
    """`nebi say` (embedded agent) and the cockpit's /api/voice share the robot speaker: one voice at a time,
    each interrupts the other, `/api/voice/stop` silences `nebi say`, and both are refused while the intercom mic is held."""
    import time

    extensions(cockpit_ext.extension)
    app = create_app(start_simulator=True, port=8125)
    with TestClient(app) as tc:

        def say(**args):
            return tc.post("/agent/call", json={"command": "say", "args": args}).json()

        def status():
            return tc.get("/api/voice/status").json()

        assert tc.post("/api/voice/say", json={"text": "one two three four five"}).json()["ok"]
        r = say(text="hello friend", to="robot", no_wait=True, no_animate=True)
        assert r["ok"], r
        st = status()
        assert st["speaking"] and st["turn"]["owner"] == "nebi say"
        stopped = tc.post("/api/voice/stop").json()
        assert stopped["stopped"] and stopped["turn"]["owner"] == "nebi say"
        r = say(text="hello again", to="robot", no_animate=True)  # waits for playback; nothing interrupts it
        assert r["ok"] and not r["result"].get("interrupted"), r

        with tc.websocket_connect("/ws/intercom") as ws:
            ws.receive_json()
            ws.send_json({"type": "start"})
            ws.send_json({"type": "talk", "on": True})
            end = time.monotonic() + 5
            while not status()["mic_held"] and time.monotonic() < end:
                time.sleep(0.05)
            r = say(text="hi", to="robot")
            assert not r["ok"] and r["error"]["code"] == "SPEAKER_BUSY" and r["error"]["hint"], r
            ws.send_json({"type": "stop"})
