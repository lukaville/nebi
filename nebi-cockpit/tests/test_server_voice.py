"""`/api/voice/*` (text-to-speech on the robot speaker) against the simulator: engines, say/stop, one voice at a
time, the intercom push-to-talk holding the speaker, and LLM-friendly errors."""

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


def test_voice_engines_lists_builtin_chirp() -> None:
    app = create_app(start_simulator=True)
    with TestClient(app) as tc:
        r = tc.get("/api/voice/engines").json()
        chirp = next(e for e in r["engines"] if e["name"] == "chirp")
        assert "happy" in chirp["emotions"] and chirp["presets"][0] == chirp["default_preset"] == "robot"
        assert r["default"] and "neutral" in r["emotions"]


def test_say_plays_on_sim_speaker_and_waits() -> None:
    app = create_app(start_simulator=True)
    with TestClient(app) as tc:
        sim = app.state.manager.simulator
        before = sim.intercom.rx_frames
        r = tc.post("/api/voice/say", json={"text": "Hello there 😊", "engine": "chirp", "wait": True})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] and body["engine"] == "chirp" and body["spoken"] == "Hello there"
        assert body["emotion"] == "happy" and body["mode"] == "words" and body["duration_s"] > 0.2
        assert body["frames_sent"] >= int(body["duration_s"] / 0.02) - 2 and not body.get("interrupted")
        # the simulated robot audio service accepted every downlink frame (headerless AudioPlaybackData, valid Opus)
        assert _wait(lambda: sim.intercom.rx_frames - before >= body["frames_sent"] - 2)
        assert sim.intercom.rx_rejected == 0
        st = tc.get("/api/voice/status").json()
        assert st["connected"] and st["speaking"] is False and st["last"]["owner"] == "cockpit"


def test_say_babble_stop_and_new_speech_interrupts() -> None:
    app = create_app(start_simulator=True)
    with TestClient(app) as tc:
        r1 = tc.post("/api/voice/say", json={"babble": True, "emotion": "excited", "volume": 0.5}).json()
        assert r1["mode"] == "babble" and r1["spoken"] == ""
        assert tc.get("/api/voice/status").json()["speaking"] is True
        r2 = tc.post("/api/voice/say", json={"text": "one two three four five six seven"}).json()
        assert r2["turn_id"] > r1["turn_id"]
        st = tc.get("/api/voice/status").json()
        assert st["speaking"] and st["turn"]["id"] == r2["turn_id"] and st["turn"]["remaining_s"] > 0
        stop = tc.post("/api/voice/stop").json()
        assert stop["stopped"] and stop["turn"]["id"] == r2["turn_id"]
        st = tc.get("/api/voice/status").json()
        assert st["speaking"] is False and st["last"]["interrupted_by"] == "cockpit stop"
        assert tc.post("/api/voice/stop").json()["stopped"] is False


def test_say_refused_while_intercom_mic_held() -> None:
    app = create_app(start_simulator=True)
    with TestClient(app) as tc, tc.websocket_connect("/ws/intercom") as ws:
        ws.receive_json()  # hello
        ws.send_json({"type": "start"})
        ws.send_json({"type": "talk", "on": True})
        assert _wait(lambda: tc.get("/api/voice/status").json()["mic_held"])
        r = tc.post("/api/voice/say", json={"text": "hi"})
        assert r.status_code == 409
        err = r.json()["error"]
        assert err["code"] == "SPEAKER_BUSY" and "talk button" in err["hint"]
        ws.send_json({"type": "talk", "on": False})
        assert _wait(lambda: not tc.get("/api/voice/status").json()["mic_held"])
        assert tc.post("/api/voice/say", json={"text": "hi"}).status_code == 200
        ws.send_json({"type": "talk", "on": True})  # talking interrupts speech
        assert _wait(lambda: tc.get("/api/voice/status").json().get("last", {}).get("interrupted_by") == "intercom")
        ws.send_json({"type": "stop"})


def test_voice_errors_are_structured() -> None:
    app = create_app(start_simulator=True)
    with TestClient(app) as tc:
        r = tc.post("/api/voice/say", json={"text": "hi", "engine": "nope"})
        assert r.status_code == 400
        err = r.json()["error"]
        assert err["code"] == "UNKNOWN_ENGINE" and "chirp" in err["hint"] and r.json()["ok"] is False
        r = tc.post("/api/voice/say", json={"text": "hi", "engine": "chirp", "preset": "opera"})
        assert r.status_code == 400 and r.json()["error"]["code"] == "UNKNOWN_PRESET"
        r = tc.post("/api/voice/say", json={"text": "!!!", "engine": "chirp"})  # nothing speakable
        assert r.status_code == 400 and r.json()["error"]["code"] == "BAD_TEXT" and "babble" in r.json()["error"]["hint"]
        r = tc.post("/api/voice/say", json={"text": "hi", "emotion": "melancholy"})
        assert r.status_code == 200 and r.json()["emotion"] == "neutral" and r.json()["warnings"]
        assert tc.post("/api/voice/say", json={"text": "hi", "volume": 5}).status_code == 422


def test_say_without_robot_is_503() -> None:
    app = create_app(start_simulator=False)
    with TestClient(app) as tc:
        r = tc.post("/api/voice/say", json={"text": "hi"})
        assert r.status_code == 503 and r.json()["error"]["code"]
        assert tc.get("/api/voice/status").json() == {"connected": False, "speaking": False, "mic_held": False, "turn": None}
        assert tc.post("/api/voice/stop").json()["stopped"] is False
