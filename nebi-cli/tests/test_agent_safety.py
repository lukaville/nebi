"""Agent safety + dispatch rules: carried check vs --force/--ignore-carried (unknown without a provider), people speed
cap, stop vs idle, speech while carried, structured error mapping. Simulator + the test extension (fake_extension)
providing the carried state and people through the public extension API."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from fake_extension import FakeExtension
from nebi_cli import pet_ops
from nebi_cli import spec as S
from nebi_cli.daemon import create_agent_app


def _app(tmp_path, monkeypatch, extensions, *objs):
    monkeypatch.setattr(pet_ops, "MEMORY_FILE", tmp_path / "memory.json")
    monkeypatch.setattr(pet_ops, "FACES_FILE", tmp_path / "faces.json")
    extensions(*objs)
    return create_agent_app(port=0)


@pytest.fixture()
def env(tmp_path, monkeypatch, extensions):
    fake = FakeExtension()
    app, runtime = _app(tmp_path, monkeypatch, extensions, fake)
    with TestClient(app) as http:

        def call(cmd: str, **args):
            return http.post("/agent/call", json={"command": cmd, "args": args}).json()

        assert call("connect", sim=True)["ok"]
        assert call("stand")["ok"]
        yield call, runtime, fake, http


def _carry(runtime, fake, carried) -> None:
    fake.sensing.carried = carried


def test_force_never_bypasses_carried_check(env) -> None:
    call, runtime, fake, _ = env
    _carry(runtime, fake, True)
    for cmd, args in (("drive", {"direction": "forward", "distance": 0.2, "force": True}),
                      ("move", {"forward": 0.2, "for": 0.2, "force": True}),
                      ("jump", {"force": True}),
                      ("flip", {"force": True}),
                      ("body", {"gesture": "wiggle", "force": True, "silent": True}),
                      ("turn", {"direction": "left", "degrees": 10, "force": True})):
        r = call(cmd, **args)
        assert r["error"]["code"] == "BEING_CARRIED" and r["exit_code"] == S.EXIT_SAFETY, (cmd, r)
    r = call("drive", direction="forward", distance=0.1, ignore_carried=True)
    assert r["ok"], r
    assert any("ignore-carried" in w for w in r["warnings"])
    # sitting (safe resting pose) and gimbal tilt stay allowed while carried
    assert call("tilt", direction="up", **{"for": 0.2})["ok"]
    _carry(runtime, fake, False)


def test_unknown_carried_state_warns_and_blocks_stunts(tmp_path, monkeypatch, extensions) -> None:
    app, _runtime = _app(tmp_path, monkeypatch, extensions)  # core only: no carried-state source at all
    with TestClient(app) as http:

        def call(cmd: str, **args):
            return http.post("/agent/call", json={"command": cmd, "args": args}).json()

        assert call("connect", sim=True)["ok"] and call("stand")["ok"]
        r = call("drive", direction="forward", distance=0.1)
        assert r["ok"] and any(w.startswith("carried-check unavailable: no carried-state source") for w in r["warnings"]), r
        r = call("flip", force=True)
        assert r["error"]["code"] == "CARRIED_UNKNOWN" and r["exit_code"] == S.EXIT_SAFETY
        r = call("jump")
        assert r["error"]["code"] == "CARRIED_UNKNOWN"
        r = call("jump", ignore_carried=True)
        assert r.get("error", {}).get("code") != "CARRIED_UNKNOWN"
        r = call("move", forward=0.8, **{"for": 0.2})
        assert r["ok"] and r["result"]["axes"]["ly"] == pytest.approx(0.8)  # nobody known around: no cap


def test_provider_reporting_unknown(env) -> None:
    call, runtime, fake, _ = env
    _carry(runtime, fake, None)
    r = call("drive", direction="forward", distance=0.1)
    assert r["ok"] and "carried-check unavailable: fake sensor unknown" in r["warnings"], r
    assert call("jump")["error"]["code"] == "CARRIED_UNKNOWN"
    _carry(runtime, fake, False)


def test_move_speed_cap_near_people(env) -> None:
    call, _runtime, _fake, _ = env  # FakeSensing reports a person 0.8 m ahead
    r = call("move", forward=0.8, strafe=-0.6, yaw=0.5, **{"for": 0.2})
    assert r["ok"], r
    axes = r["result"]["axes"]
    assert axes["ly"] == pytest.approx(0.3) and axes["lx"] == pytest.approx(-0.3) and axes["rx"] == 0.5
    assert any("speed capped to 0.3" in w for w in r["warnings"])
    r = call("move", forward=0.2, **{"for": 0.2})
    assert r["ok"] and not any("capped" in w for w in r.get("warnings", [])), r  # no false warning
    r = call("drive", direction="forward", distance=0.1, speed=0.2)
    assert r["ok"] and not any("capped" in w for w in r.get("warnings", [])), r


def test_stop_stops_idle(env) -> None:
    call, _runtime, _fake, http = env
    assert call("idle", action="start", silent=True)["result"]["idle"] == "running"
    r = call("stop")
    assert r["ok"] and r["result"]["idle_stopped"] is True and "idle start" in r["result"]["idle_resume"]
    assert http.get("/agent/activity").json()["idle_running"] is False
    assert call("stop")["result"]["idle_stopped"] is False


def test_speech_and_eyes_allowed_while_carried(env) -> None:
    call, runtime, fake, _ = env
    _carry(runtime, fake, True)
    assert call("eyes", what="happy")["ok"]
    assert call("sound", name="meow")["ok"]
    r = call("emote", emotion="happy")
    assert r["ok"], r
    assert "motion" not in r["result"]  # emote never moves the body; eyes/voice played
    r = call("say", babble=True, emotion="happy", to="robot", no_wait=True)
    assert r["ok"], r
    call("stop")
    _carry(runtime, fake, False)


def test_error_mapping(env) -> None:
    call, runtime, _fake, _ = env
    r = call("drive", direction="forward", distance="far")
    assert r["error"]["code"] == "BAD_ARGUMENT" and r["exit_code"] == S.EXIT_USAGE and "distance" in r["error"]["message"]
    r = call("drive", distance=1)
    assert r["error"]["code"] == "BAD_ARGUMENT" and "direction" in r["error"]["message"]
    r = call("drive", direction="up", distance=1)
    assert r["error"]["code"] == "BAD_ARGUMENT"
    assert call("track", target="a,b")["error"]["code"] == "BAD_ARGUMENT"
    assert call("pub", topic="/cmd/x", fields="{nope")["error"]["code"] == "BAD_ARGUMENT"

    async def boom(a):
        return {}["missing"]

    runtime.handlers["info"] = boom
    r = call("info")
    assert r["error"]["code"] == "INTERNAL_ERROR" and "KeyError" in r["error"]["message"] and r["exit_code"] == S.EXIT_FAILED
    # internal keys cannot be injected from outside
    r = call("move", forward=0.8, _speed_cap=[1.0, "x"], **{"for": 0.1})
    assert r["ok"] and r["result"]["axes"]["ly"] == pytest.approx(0.3)


def test_idle_gating(env) -> None:
    from types import SimpleNamespace

    from nebi_cli.idle_ops import IdleConfig, autonomy_reason

    _call, runtime, _fake, _ = env

    def tel(**kw):
        base = dict(docked=False, tracking_active=False, locked_target_id=0, active_game=0,
                    mission_name="CAMERA", sub_mode_name="MANUAL")
        base.update(kw)
        return SimpleNamespace(telemetry=SimpleNamespace(**base))

    assert autonomy_reason(tel()) is None
    assert autonomy_reason(tel(sub_mode_name="FOLLOW_TRAIL"))
    assert autonomy_reason(tel(mission_name="PET", sub_mode_name="DOCK"))
    assert autonomy_reason(tel(tracking_active=True))
    assert autonomy_reason(tel(docked=True))

    # busy (say/listen) and running commands block idle entirely
    tok = runtime.activity.begin_busy("listen")
    assert runtime.idle.blocked()
    runtime.activity.end_busy(tok)
    assert runtime.idle.blocked() is None

    # without body permission a wave gets eyes only, no nod
    c = runtime.manager.client
    calls: list[str] = []
    orig = runtime.invoke

    async def spy(command, args, **kw):
        calls.append(command)
        return await orig(command, args, **kw)

    runtime.invoke = spy  # type: ignore[method-assign]
    cfg = IdleConfig(quiet=None, level="calm", speak=False)
    portal_result = _run_in_app_loop(env, runtime.idle.react(c, {"kind": "gesture", "gesture": "palm"}, False, cfg, False))
    assert portal_result == "waved back (wink)" and "body" not in calls
    runtime.invoke = orig  # type: ignore[method-assign]


def _run_in_app_loop(env, coro):
    _call, _runtime, _fake, http = env
    return http.portal.call(lambda: coro)
