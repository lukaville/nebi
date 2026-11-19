"""`/api/eyes/*` (eye LEDs) against the simulator: colour hold / until release, expressions, release, errors, and
the legacy `/api/system/led_*` routes."""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from nebi_cockpit.app import create_app


def _wait(pred, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


def test_eyes_catalog_works_and_lists_expressions() -> None:
    app = create_app(start_simulator=True)
    with TestClient(app) as tc:
        r = tc.get("/api/eyes").json()
        assert r["ok"] and r["status"]["mode"] == "firmware"
        assert "happy" in [e["name"] for e in r["expressions"]]
        assert any(p["hex"] == "#000000" for p in r["presets"])


def test_color_until_release_reaches_sim_then_release_stops_frames() -> None:
    app = create_app(start_simulator=True)
    with TestClient(app) as tc:
        sim = app.state.manager.simulator
        r = tc.post("/api/eyes/color", json={"hex": "#0000ff", "hold_sec": None})
        assert r.status_code == 200, r.text
        st = r.json()
        assert st["mode"] == "color" and st["until_release"] and st["color"] == "#0000ff"
        assert _wait(lambda: sim.led_rgb == (0, 0, 255) and sim.led_frames >= 3)
        assert tc.get("/api/eyes").json()["status"]["mode"] == "color"
        st = tc.post("/api/eyes/release").json()
        assert st["mode"] == "firmware"
        n = sim.led_frames
        time.sleep(0.5)
        assert sim.led_frames == n


def test_expression_and_timed_color() -> None:
    app = create_app(start_simulator=True)
    with TestClient(app) as tc:
        st = tc.post("/api/eyes/expression", json={"name": "wink", "loops": 1}).json()
        assert st["mode"] == "expression" and st["expression"] == "wink" and st["seconds_left"] > 1
        st = tc.post("/api/eyes/color", json={"r": 255, "g": 0, "b": 0, "hold_sec": 5, "brightness": 0.5}).json()
        assert st["mode"] == "color" and 4 < st["seconds_left"] <= 5
        sim = app.state.manager.simulator
        assert _wait(lambda: sim.led_rgb == (128, 0, 0))
        tc.post("/api/eyes/release")


def test_eye_errors_are_structured_400() -> None:
    app = create_app(start_simulator=True)
    with TestClient(app) as tc:
        for path, body, code in (
            ("/api/eyes/expression", {"name": "nope"}, "LED_UNKNOWN_EXPRESSION"),
            ("/api/eyes/color", {"hex": "blue"}, "LED_BAD_COLOR"),
            ("/api/eyes/color", {"r": 300}, "LED_BAD_COLOR"),
            ("/api/eyes/color", {"hold_sec": -2}, "LED_BAD_HOLD"),
            ("/api/eyes/color", {"brightness": 3}, "LED_BAD_BRIGHTNESS"),
        ):
            r = tc.post(path, json=body)
            assert r.status_code == 400, (path, body, r.text)
            err = r.json()["error"]
            assert err["code"] == code and err["hint"]


def test_legacy_led_routes_still_work_without_hardware_note() -> None:
    app = create_app(start_simulator=True)
    with TestClient(app) as tc:
        r = tc.post("/api/system/led_rgb", json={"r": 0, "g": 0, "b": 255}).json()
        assert r["ok"] and r["led_direct_supported"] is True and "hardware_note" not in r
        assert r["eyes"]["mode"] == "color"
        r = tc.post("/api/system/led_expression", json={"effect": "happy.csv", "play_count": 1}).json()
        assert r["ok"] and r["eyes"]["expression"] == "happy"
        tc.post("/api/eyes/release")
