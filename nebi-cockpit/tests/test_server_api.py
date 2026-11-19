"""Integration tests for FastAPI control, media proxy, video snapshot, and WebSocket endpoints."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from nebi_cockpit.app import create_app


def test_fastapi_endpoints_and_websockets() -> None:
    app = create_app(start_simulator=True)
    with TestClient(app) as client:
        # Root HTML UI
        r_root = client.get("/")
        assert r_root.status_code == 200
        assert "<title>nebi cockpit</title>" in r_root.text

        # Health & Catalog
        r_health = client.get("/api/health")
        assert r_health.status_code == 200 and r_health.json()["connected"] is True

        r_cat = client.get("/api/catalog")
        assert r_cat.status_code == 200
        cat = r_cat.json()
        assert cat["topic_count"] >= 180
        assert len(cat["camera_profiles"]) == 46 and cat["camera_profiles"][0]["label"]
        assert "motions" not in cat and {"key", "label", "default", "group"} <= set(cat["params"][0])

        # Control WS: send joystick & receive telemetry status
        with client.websocket_connect("/ws/control") as ws_ctrl:
            ws_ctrl.send_text(json.dumps({"type": "joystick", "lx": 0.0, "ly": 0.6, "rx": 0.0, "ry": 0.0}))
            raw_msg = ws_ctrl.receive_text()
            msg = json.loads(raw_msg)
            assert msg["type"] == "status"
            assert msg["connected"] is True
            assert msg["telemetry"]["serial_number"].startswith("LZ110")

        # Video WS: receive decoded JPEG frame
        with client.websocket_connect("/ws/video") as ws_vid:
            jpg = ws_vid.receive_bytes()
            assert jpg.startswith(b"\xff\xd8")

        # REST actions: Photo capture + Media gallery proxy + Snapshot
        assert client.post("/api/motion/stand").status_code == 200
        # Still photos are unsupported by the robot firmware (no camera photo handler): 501 + structured error
        r_photo = client.post("/api/camera/photo")
        assert r_photo.status_code == 501 and r_photo.json()["error"]["code"] == "CAMERA_PHOTO_UNSUPPORTED"
        app.state.manager.simulator.capture_photo()  # seed a second media file directly

        media_list = client.get("/api/media/files").json()["files"]
        assert len(media_list) >= 2
        fid = media_list[0]["file_id"]
        r_media = client.get(f"/api/media/files/{fid}/content")
        assert r_media.status_code == 200 and len(r_media.content) > 100

        r_snap = client.get("/api/video/snapshot.jpg")
        assert r_snap.status_code == 200 and r_snap.content.startswith(b"\xff\xd8")

        # HMS fault inject + clear roundtrip via exact telemetry alert code
        assert client.post("/api/system/hms/inject_sim_fault", json={"code": "0xF0010012", "instance": 0}).status_code == 200
        import time

        time.sleep(0.15)
        alerts = client.get("/api/status").json()["telemetry"]["hms_alerts"]
        assert len(alerts) == 1
        assert alerts[0]["code"] == "0xF0010012"
        r_clr = client.post("/api/system/hms/clear", json={"code": alerts[0]["code"], "instance": 0})
        assert r_clr.status_code == 200
        time.sleep(0.15)
        assert client.get("/api/status").json()["telemetry"]["hms_alerts"] == []


def test_hms_pet_mode_and_connection_menu() -> None:
    """Firmware limits surfaced as clear errors; HMS from raise/clear events; plain connection menu."""
    import time

    app = create_app(start_simulator=True)
    with TestClient(app) as client:
        cat = client.get("/api/catalog").json()
        # Choreographed motions are gone entirely (the firmware never accepted them from apps)
        assert "motion_play" not in cat
        assert client.post("/api/motion/play", json={"motion_id": 501}).status_code in (404, 405)

        # Connection menu: plain-language flows, Home Wi-Fi default, USB-C cable as a normal option
        ids = [f["id"] for f in cat["flows"]]
        assert ids[0] == cat["default_flow"] == "lan_sta"
        assert {"ble_home_wifi", "softap", "ble_bootstrap", "usb_ncm", "simulator"} <= set(ids)
        assert cat["unsupported_flows"] == []

        # HMS: active set + event log from /status/hms/event_broadcast, authoritative refresh via query_active
        assert client.post("/api/system/hms/inject_sim_fault", json={"code": "0xF0080003", "instance": 0}).status_code == 200
        time.sleep(0.2)
        hms = client.get("/api/system/hms").json()
        assert [a["code"] for a in hms["active"]] == ["0xF0080003"]
        assert hms["active"][0]["source"] == "event" and hms["events"][-1]["action"] == "raised"
        r = client.post("/api/system/hms/refresh").json()
        assert r["ok"] and r["fault_codes"] == ["0xF0080003"]
        assert client.post("/api/system/hms/clear", json={"code": "0xF0080003", "instance": 0}).status_code == 200
        time.sleep(0.2)
        hms = client.get("/api/system/hms").json()
        assert hms["active"] == [] and hms["events"][-1]["action"] == "cleared"

        # Pet mode: entered while standing, refused with a hint while folded
        assert client.post("/api/motion/stand").status_code == 200
        time.sleep(0.2)
        r = client.post("/api/motion/ego").json()
        assert r["entered"] is True, r
        client.post("/api/motion/mission", json={"mission_id": 1, "sub_mode": 0})
        assert client.post("/api/motion/sit").status_code == 200
        time.sleep(0.3)
        r = client.post("/api/motion/ego")
        assert r.status_code == 409 and r.json()["error"]["code"] == "EGO_MODE_NEEDS_STANDING"
        assert "Stand" in r.json()["error"]["hint"]

        # No USB network adapter on this machine: fails fast with a structured error and a cable hint, not a timeout
        r = client.post("/api/connection", json={"flow": "usb_ncm"})
        err = r.json()["error"]
        assert r.status_code >= 400 and err["code"] == "USB_NETWORK_NOT_FOUND" and "cable" in err["hint"]
