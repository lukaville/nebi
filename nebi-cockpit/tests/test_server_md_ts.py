"""Cockpit BLE (MD-TS) home Wi-Fi provisioning endpoint."""

from __future__ import annotations

from fastapi.testclient import TestClient

from nebi_cockpit.app import create_app


def test_fastapi_ble_provision_home_wifi_endpoint() -> None:
    app = create_app(start_simulator=True)
    with TestClient(app) as http:
        r = http.post(
            "/api/ble/provision_home_wifi",
            json={
                "ssid": "StudioHome-5G",
                "password": "studio-password-123",
                "ble_address": "SIM-BLE-LZ110-01",
                "use_simulator_ble": True,
                "wait_for_button_sec": 3.0,
            },
        )
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["ok"] is True
        assert data["ssid"] == "StudioHome-5G"
        assert data["connection"]["connected"] is True
        assert data["connection"]["md_ts_active"] is True

        ping = http.post("/api/system/ping")
        assert ping.status_code == 200
        assert ping.json()["rtt_ms"] >= 0

