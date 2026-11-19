"""Tests for the agent-friendly `nebi` CLI: spec/unit parsing, condition grammar, and daemon ops vs the simulator."""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from nebi_cli import spec as S
from nebi_cli.cli import build_parser, ns_to_args
from nebi_cli.daemon import create_agent_app
from nebi_cli.ops import AgentError, _eval_clause, parse_condition


def test_unit_parsers() -> None:
    assert S.parse_distance("50cm") == pytest.approx(0.5)
    assert S.parse_distance("1m") == 1.0
    assert S.parse_distance("0.3") == 0.3
    assert S.parse_duration("500ms") == 0.5
    assert S.parse_duration("2m") == 120.0
    assert S.parse_angle("quarter") == 90.0
    assert S.parse_angle("45deg") == 45.0
    with pytest.raises(ValueError):
        S.parse_distance("far")


def test_parser_builds_from_spec_and_converts_units() -> None:
    p = build_parser()
    ns = p.parse_args(["drive", "forward", "50cm", "--speed", "0.2"])
    args = ns_to_args(S.COMMANDS_BY_NAME["drive"], ns)
    assert args == {"direction": "forward", "distance": pytest.approx(0.5), "speed": 0.2, "clearance": 0.3}
    ns = p.parse_args(["pub", "/cmd/x"])
    assert ns_to_args(S.COMMANDS_BY_NAME["pub"], ns)["fields"] == {}
    # Every spec command must be parseable with its examples' first token.
    for c in S.COMMANDS:
        for ex in c.examples:
            assert ex.startswith(f"nebi {c.name}"), ex


def test_condition_grammar() -> None:
    cl = parse_condition("posture=BALANCING, battery>30")
    assert cl == [("posture", "=", "BALANCING"), ("battery", ">", 30.0)]
    assert _eval_clause("balancing", "=", "BALANCING")
    assert _eval_clause(31, ">", 30.0)
    assert not _eval_clause(False, "=", True)
    assert _eval_clause([], "=", 0.0)  # lists compare by length
    with pytest.raises(AgentError):
        parse_condition("posture")


def test_daemon_ops_against_simulator(tmp_path, monkeypatch) -> None:
    app, _runtime = create_agent_app(port=0)
    with TestClient(app) as http:

        def call(cmd: str, **args):
            return http.post("/agent/call", json={"command": cmd, "args": args}).json()

        r = call("state")
        assert r["ok"] is False and r["error"]["code"] == "NOT_CONNECTED" and r["exit_code"] == S.EXIT_UNAVAILABLE

        r = call("connect", sim=True)
        assert r["ok"], r
        assert r["state"]["connected"] is True

        assert call("flip")["exit_code"] == S.EXIT_SAFETY
        assert call("drive", direction="forward")["exit_code"] == S.EXIT_USAGE  # no extent
        assert call("nope")["error"]["code"] == "UNKNOWN_COMMAND"

        r = call("stand")
        assert r["ok"] and r["state"]["standing"]

        r = call("drive", direction="forward", distance=0.4, speed=0.3)
        assert r["ok"], r
        assert r["result"]["stopped_reason"] == "reached"
        assert 0.25 <= r["result"]["traveled_m"] <= 0.7

        r = call("turn", direction="left", degrees=30)
        assert r["ok"] and r["result"]["stopped_reason"] == "reached"

        out = tmp_path / "look.jpg"
        r = call("look", out=str(out), annotate=True)
        assert r["ok"], r
        assert out.read_bytes()[:2] == b"\xff\xd8"

        assert call("wait", condition="standing=true", timeout=2)["ok"]
        r = call("wait", condition="battery<0", timeout=0.3)
        assert r["exit_code"] == S.EXIT_TIMEOUT and "observed" in r["error"]["details"]

        r = call("events", since=0)
        assert r["ok"] and any(e["type"] == "connected" for e in r["result"]["events"])

        r = call("eyes", what="happy")
        assert r["ok"] and r["result"]["mode"] == "expression" and r["result"]["expression"] == "happy", r
        r = call("eyes", what="list")["result"]
        assert "happy" in r["expressions"] and r["status"]["mode"] == "expression"
        r = call("eyes", what="#0000ff", until_release=True)["result"]
        assert r["mode"] == "color" and r["until_release"] and r["color"] == "#0000ff"
        r = call("eyes", what="red", hold=5, brightness=0.5)["result"]
        assert r["mode"] == "color" and 4 < r["seconds_left"] <= 5
        assert call("eyes", what="status")["result"]["mode"] == "color"
        assert call("eyes", what="release")["result"]["mode"] == "firmware"
        r = call("eyes", what="red", brightness=3)
        assert r["error"]["code"] == "LED_BAD_BRIGHTNESS" and r["exit_code"] == S.EXIT_USAGE and r["error"]["hint"], r
        r = call("emote", emotion="love")
        assert r["ok"] and r["result"]["eyes"] == "heart", r
        assert call("eyes", what="release")["ok"]
        assert call("sound", name="meow")["ok"]

        # Choreographed motions are not offered at all (the firmware never accepted them from apps)
        assert "play" not in S.COMMANDS_BY_NAME and "motions" not in S.COMMANDS_BY_NAME
        r = call("emote", emotion="happy")
        assert r["ok"] and "motion" not in r["result"]

        # HMS: a fault raised on /status/hms/event_broadcast shows up in faults, state and the event log
        sim = _runtime.manager.simulator
        sim.inject_hms_fault("0xF0080003", 0)
        time.sleep(0.3)
        r = call("faults")
        assert [f["code"] for f in r["result"]["faults"]] == ["0xF0080003"], r
        assert r["result"]["faults"][0]["recovery_hint"]
        assert [f["code"] for f in call("state")["result"]["faults"]] == ["0xF0080003"]
        r = call("faults", clear="0xF0080003")
        assert r["ok"] and r["result"]["count"] == 0
        evs = [e["type"] for e in call("events", since=0)["result"]["events"]]
        assert "fault_raised" in evs and "fault_cleared" in evs, evs

        assert call("stop")["ok"]
        assert call("sit")["result"]["posture"] == "FOLDED"
        assert call("drive", direction="forward", distance=0.2)["error"]["code"] == "NOT_STANDING"
        r = call("mode", mode="ego")
        assert r["error"]["code"] == "EGO_MODE_NEEDS_STANDING" and "Stand" in r["error"]["hint"], r


def test_say_streams_voice_to_simulator(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("PYNEBI_TTS_ENGINE", raising=False)
    app, _runtime = create_agent_app(port=0)
    with TestClient(app) as http:

        def call(cmd: str, **args):
            return http.post("/agent/call", json={"command": cmd, "args": args}).json()

        assert call("connect", sim=True)["ok"]
        out = tmp_path / "line.wav"
        r = call("say", text="Ball? 👀", to="robot", out=str(out))
        assert r["ok"], r
        res = r["result"]
        assert res["emotion"] == "curious" and res["spoken"] == "Ball?" and res["engine"] == "chirp"
        # Real-time paced 20 ms Opus frames covering the whole utterance.
        assert abs(res["frames_sent"] - res["duration_s"] / 0.02) <= 3
        assert out.exists() and out.with_suffix(".json").exists()
        assert json.loads(out.with_suffix(".json").read_text())["beats"]

        r = call("say", babble=True, emotion="happy", to="robot", no_wait=True)
        assert r["ok"] and r["result"]["mode"] == "babble" and r["result"]["playing_in_background"]
        assert call("stop")["ok"]

        assert call("say", text="", to="robot")["result"]["mode"] == "babble"  # no words -> pet babble
        assert call("say", text="hi", preset="nope", to="robot")["exit_code"] == S.EXIT_USAGE


def test_every_error_code_is_catalogued() -> None:
    """errors.ERROR_CODES is the one list of agent error codes (with default exit codes)."""
    import re
    from pathlib import Path

    import nebi_cli as agent_pkg
    from nebi_cli.errors import ERROR_CODES

    pat = re.compile(r'(?:AgentError|CliError)\(\s*"([A-Z_]+)"|"code":\s*"([A-Z_]+)"')
    raised: set[str] = set()
    for path in Path(agent_pkg.__file__).parent.glob("*.py"):
        for m in pat.finditer(path.read_text()):
            raised.add(m.group(1) or m.group(2))
    assert raised and not raised - set(ERROR_CODES), raised - set(ERROR_CODES)


async def test_event_log_long_poll_wakes_on_emit() -> None:
    import asyncio

    from nebi_cli.events import EventLog

    log = EventLog()
    waiter = asyncio.create_task(log.wait_for_new(0, 5.0))
    await asyncio.sleep(0.01)
    log.emit("x")
    await asyncio.wait_for(waiter, 1.0)
    assert [e["type"] for e in log.since(0)] == ["x"]
