"""Cockpit REST: discovery, structured errors, Wi-Fi provisioning, disconnect/retry (against the simulator)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from nebi_cockpit.app import create_app


def test_server_flows_discovery_and_structured_errors() -> None:
    app = create_app(start_simulator=True)
    with TestClient(app) as tc:
        st = tc.get("/api/status").json()
        assert st["connected"] is True
        assert st["connection"]["stage"] == "connected"

        ifaces = tc.get("/api/network/interfaces").json()
        assert "interfaces" in ifaces

        disc = tc.post("/api/discovery/scan", json={"include_ble": True, "include_lan": False, "timeout": 0.6}).json()
        assert any(r["flow"] == "simulator" for r in disc["robots"])

        # Wrong Wi-Fi password returns structured NebiWifiProvisionError JSON
        bad_wifi = tc.post("/api/system/wifi/provision", json={"ssid": "Studio-5G", "password": "wrong"})
        assert bad_wifi.status_code == 502
        err_json = bad_wifi.json()
        assert err_json["error"]["code"] == "WIFI_PROVISION_WRONG_KEY"

        # Valid Wi-Fi provision succeeds
        ok_wifi = tc.post("/api/system/wifi/provision", json={"ssid": "Studio-5G", "password": "correctpassword"})
        assert ok_wifi.status_code == 200
        assert ok_wifi.json()["status"] == "succeeded"

        # Disconnect -> commands return structured 503 NOT_CONNECTED -> retry restores Simulator
        tc.post("/api/connection/disconnect")
        not_conn = tc.post("/api/motion/stand")
        assert not_conn.status_code == 503
        assert not_conn.json()["error"]["code"] == "NOT_CONNECTED"

        retried = tc.post("/api/connection/retry")
        assert retried.status_code == 200
        assert retried.json()["connected"] is True

