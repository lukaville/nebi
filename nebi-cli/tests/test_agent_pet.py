"""Pet-agent tools (body language, memory, care, idle, core `wait --for` events) against the simulator, core only."""
from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from nebi_cli import pet_ops
from nebi_cli import spec as S
from nebi_cli.daemon import create_agent_app
from nebi_cli.idle_ops import hours_window, in_window, robot_event_for_idle


async def _later(fn) -> None:
    asyncio.get_running_loop().call_later(0.3, fn)


def test_pet_tools_against_simulator(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(pet_ops, "MEMORY_FILE", tmp_path / "memory.json")
    monkeypatch.setattr(pet_ops, "FACES_FILE", tmp_path / "faces.json")
    app, runtime = create_agent_app(port=0)
    with TestClient(app) as http:

        def call(cmd: str, **args):
            return http.post("/agent/call", json={"command": cmd, "args": args}).json()

        assert call("connect", sim=True)["ok"]

        # extension-only commands and views are absent from the core
        for name in ("sense", "feel", "goto", "where", "explore"):
            assert call(name)["error"]["code"] == "UNKNOWN_COMMAND"
            assert name not in S.COMMANDS_BY_NAME
        assert call("look", view="depth")["exit_code"] == S.EXIT_USAGE  # not a core view

        r = call("care")
        assert r["ok"] and r["result"]["mood"] in ("fine", "tired", "uneasy") and "summary" in r["result"]
        assert "battery_temp_c" not in r["result"] and not r.get("warnings")

        # body language needs standing for most gestures
        assert call("sit")["ok"]
        assert call("body", gesture="wiggle")["error"]["code"] == "NOT_STANDING"
        assert call("stand")["ok"]
        r = call("body", gesture="nod", silent=True)
        assert r["ok"] and r["result"]["completed"], r
        r = call("body", gesture="cower", silent=True, intensity=0.5)
        assert r["ok"] and r["result"]["summary"].startswith("cowered")
        assert abs(runtime.manager.client.motion.joystick.height - (16 / 31)) < 0.02  # restored
        r = call("body", gesture="look-at", target="sofa", silent=True)
        assert r["error"]["code"] == "TARGET_NOT_FOUND"  # only people/pets without an extension locator

        # core wait --for events come from the robot event log
        runtime.events.emit("charging_changed", old=False, new=False)
        r = call("wait", **{"for": "charging", "timeout": 0.3})
        assert r["exit_code"] == S.EXIT_TIMEOUT  # only events after the call (and new=True) count
        assert call("wait", **{"for": "nonsense"})["exit_code"] == S.EXIT_USAGE
        assert call("wait", **{"for": "petted"})["exit_code"] == S.EXIT_USAGE  # extension event, not loaded
        assert call("wait")["exit_code"] == S.EXIT_USAGE

        def emit_posture() -> None:
            runtime.events.emit("posture_changed", old="BALANCING", new="FOLDED")

        http.portal.call(_later, emit_posture)
        r = call("wait", **{"for": "posture", "timeout": 3})
        assert r["ok"] and r["result"]["event"]["type"] == "posture_changed" and "posture" in r["result"]["summary"]

        r = call("events", since=0)
        assert r["ok"] and "senses_events" not in r["result"] and r["result"]["events"]
        assert call("events", source="senses")["exit_code"] == S.EXIT_USAGE

        # memory
        assert call("note", text="Alice likes the red ball", about="Alice")["ok"]
        assert call("note", text="Bob is afraid of the vacuum", tags="fear")["ok"]
        r = call("recall", query="red ball")
        assert r["result"]["memories"][0]["about"] == "Alice"
        assert call("recall")["result"]["total"] == 2
        assert call("note", forget="1")["result"]["removed"] == 1
        assert "nothing" in call("recall", query="ball")["result"]["summary"]

        assert call("meet", list=True)["result"]["people"] == {}

        # idle life
        r = call("idle", action="start", silent=True)
        assert r["ok"] and r["result"]["idle"] == "running"
        assert http.get("/agent/activity").json()["idle_running"] is True
        assert call("idle", action="stop")["result"]["idle"] == "stopped"

        assert call("sit")["ok"]


def test_quiet_hours_window() -> None:
    w = hours_window("22-8")
    assert in_window(w, 23) and in_window(w, 3) and not in_window(w, 12)
    assert in_window(hours_window("9-17"), 10)
    assert not in_window(None)


def test_idle_understands_core_robot_events() -> None:
    e = robot_event_for_idle({"type": "target_appeared", "id": 3, "label": "person", "where": "left"})
    assert e is not None and e["kind"] == "person_appeared" and "person" in e["summary"]
    battery = robot_event_for_idle({"type": "battery_low", "battery": 19})
    assert battery is not None and battery["kind"] == "battery_low"
    fault = robot_event_for_idle({"type": "fault_raised", "code": "0x1"})
    assert fault is not None and fault["kind"] == "fault"
    assert robot_event_for_idle({"type": "motion_changed"}) is None


def test_every_spec_command_has_a_handler() -> None:
    _app, runtime = create_agent_app(port=0)
    for c in S.COMMANDS:
        if not c.local:
            assert c.name.replace("-", "_") in runtime.handlers, c.name
