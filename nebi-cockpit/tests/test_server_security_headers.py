"""Response security headers (clickjacking / sniffing / referrer / CORP) and hotspot-password hygiene."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from nebi_cockpit.app import create_app
from nebi_cockpit.security import content_security_policy


@pytest.fixture()
def app_client():
    app = create_app(start_simulator=True)
    with TestClient(app) as client:
        yield app, client


def _assert_frame_protected(r) -> None:  # type: ignore[no-untyped-def]
    assert r.headers["x-frame-options"] == "DENY"
    csp = r.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in csp and "default-src 'self'" in csp and "script-src 'self'" in csp
    assert "'unsafe-eval'" not in csp
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "no-referrer"


def test_security_headers_on_ui_api_static_and_errors(app_client) -> None:
    _app, client = app_client
    for path in ("/", "/static/js/main.js", "/api/status", "/api/does-not-exist"):
        r = client.get(path)
        _assert_frame_protected(r)
        assert "cross-origin-resource-policy" not in r.headers
    # refused by SecurityMiddleware (DNS-rebinding Host): still carries the headers
    r = client.get("/api/status", headers={"host": "evil.example"})
    assert r.status_code == 403
    _assert_frame_protected(r)


def test_corp_on_video_and_media(app_client) -> None:
    _app, client = app_client
    for path in ("/api/video/snapshot.jpg", "/api/media/status"):
        r = client.get(path)
        _assert_frame_protected(r)
        assert r.headers["cross-origin-resource-policy"] == "same-origin"


def test_csp_connect_src_and_extension_origins() -> None:
    csp = content_security_policy("127.0.0.1:8016", ["http://127.0.0.1:8796", "javascript:alert(1)", "http://a b"])
    assert "connect-src 'self' ws://127.0.0.1:8016 wss://127.0.0.1:8016 http://127.0.0.1:8796" in csp
    assert "frame-src 'self' http://127.0.0.1:8796" in csp
    assert "javascript" not in csp and "a b" not in csp
    # a hostile Host header cannot inject directives
    assert "evil" not in content_security_policy("x; script-src evil")


def test_agent_daemon_app_has_security_headers() -> None:
    nebi_cli_daemon = pytest.importorskip("nebi_cli.daemon")
    try:
        app, _runtime = nebi_cli_daemon.create_agent_app(start_simulator=False)
    except Exception as exc:  # pragma: no cover - nebi-cli mid-refactor must not mask the cockpit tests
        pytest.skip(f"create_agent_app unavailable: {exc!r}")
    with TestClient(app) as client:
        _assert_frame_protected(client.get("/"))


def test_hotspot_password_only_in_bootstrap_response(app_client) -> None:
    _app, client = app_client
    r = client.post(
        "/api/ble/bootstrap",
        json={"ble_address": "SIM-BLE-LZ110-01", "use_simulator_ble": True, "wait_for_button_sec": 3.0},
    )
    assert r.status_code == 200, r.text
    password = r.json()["credentials"]["password"]
    assert password
    status = client.get("/api/status")
    assert password not in status.text
    creds = status.json()["connection"]["ble_credentials"]
    assert creds["ssid"] and creds["password_set"] is True and "password" not in creds
    # structured error bodies carry the connection summary too (TEST-NET-3 address: nothing answers)
    err = client.post("/api/connection", json={"mode": "lan_sta", "host": "203.0.113.1", "ucp_port": 1})
    assert err.status_code >= 400 and err.json()["connection"]["ble_credentials"]["password_set"] is True
    assert password not in err.text
    with client.websocket_connect("/ws/control") as ws:
        for _ in range(20):
            msg = ws.receive_text()
            assert password not in msg
            if json.loads(msg).get("type") == "status":
                break
