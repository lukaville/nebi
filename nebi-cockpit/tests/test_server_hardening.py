"""Server hardening: dead-man neutral, non-finite input, Origin/Host/CSRF/token checks, SSID cache,
video queue policy, media range proxy and cancellable link operations."""

from __future__ import annotations

import asyncio
import json
import threading
import time

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from nebi.exceptions import NebiError
from nebi_cockpit import connection_manager as cm
from nebi_cockpit import wifi_cache
from nebi_cockpit.app import configure_security, create_app
from nebi_cockpit.video import VideoStreamHub


def _wait_for(pred, timeout: float = 2.0) -> bool:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


def _recv_until(ws, mtype: str, limit: int = 60) -> dict:
    for _ in range(limit):
        msg = json.loads(ws.receive_text())
        if msg.get("type") == mtype:
            return msg
    raise AssertionError(f"no {mtype!r} message")


@pytest.fixture()
def app_client():
    app = create_app(start_simulator=True)
    with TestClient(app) as client:
        yield app, client


# ---------------------------------------------------------------- dead-man


def test_neutral_applied_while_disconnected(app_client) -> None:
    app, client = app_client
    robot = app.state.manager.client
    js = robot.motion.joystick
    with client.websocket_connect("/ws/control") as ws:
        ws.send_text(json.dumps({"type": "joystick", "lx": 0.0, "ly": 0.6, "rx": 0.3, "ry": 0.2, "roll": 0.1}))
        assert _wait_for(lambda: abs(js.ly - 0.6) < 1e-6)
        robot._connected = False  # link drops while the stick is held
        try:
            ws.send_text(json.dumps({"type": "joystick", "lx": 0, "ly": 0, "rx": 0, "ry": 0}))
            # must land in the latched state right away (well before motion's own 0.5 s axis TTL)
            assert _wait_for(lambda: js.ly == 0 and js.rx == 0 and js.ry == 0 and js.roll == 0, timeout=0.3)
            # a non-neutral stick is never latched while the link is down
            ws.send_text(json.dumps({"type": "joystick", "ly": 0.9}))
            time.sleep(0.1)
            assert js.ly == 0
        finally:
            robot._connected = True


def test_deadman_fires_while_disconnected_and_on_close(app_client) -> None:
    app, client = app_client
    robot = app.state.manager.client
    js = robot.motion.joystick
    with client.websocket_connect("/ws/control?deadman_ms=200") as ws:
        ws.send_text(json.dumps({"type": "joystick", "ly": 0.5, "roll": 0.4}))
        assert _wait_for(lambda: abs(js.ly - 0.5) < 1e-6)
        robot._connected = False
        try:
            assert _wait_for(lambda: js.ly == 0 and js.roll == 0, timeout=1.5)
        finally:
            robot._connected = True
    with client.websocket_connect("/ws/control?deadman_ms=5000") as ws:
        ws.send_text(json.dumps({"type": "joystick", "rx": 0.7}))
        assert _wait_for(lambda: abs(js.rx - 0.7) < 1e-6)
    assert _wait_for(lambda: js.rx == 0, timeout=1.0)  # socket closed -> neutral


def test_nan_and_out_of_range_rejected_or_clamped(app_client) -> None:
    app, client = app_client
    js = app.state.manager.client.motion.joystick
    with client.websocket_connect("/ws/control") as ws:
        ws.send_text('{"type": "joystick", "lx": NaN, "ly": 0.5}')
        err = _recv_until(ws, "error")
        assert "rejected" in err["message"]
        assert js.ly == 0
        ws.send_text('{"type": "joystick", "ly": Infinity}')
        _recv_until(ws, "error")
        ws.send_text(json.dumps({"type": "joystick", "ly": "fast"}))
        _recv_until(ws, "error")
        ws.send_text(json.dumps({"type": "joystick", "ly": 7.0, "height": -3}))
        assert _wait_for(lambda: js.ly == 1.0)
        assert js.height == 0.0
        ws.send_text(json.dumps({"type": "joystick"}))
    # REST bodies refuse non-finite floats too
    r = client.post("/api/motion/jump", content='{"height": NaN}', headers={"content-type": "application/json"})
    assert r.status_code == 422


# ---------------------------------------------------------------- Origin / Host / CSRF / token


def test_websocket_origin_checks(app_client) -> None:
    _app, client = app_client
    for bad in ("http://evil.example", "null", "http://127.0.0.1:9999", "http://localhost.evil.example"):
        with pytest.raises(WebSocketDisconnect), client.websocket_connect("/ws/control", headers={"origin": bad}) as ws:
            ws.receive_text()
    for path in ("/ws/video", "/ws/intercom"):
        with pytest.raises(WebSocketDisconnect), client.websocket_connect(path, headers={"origin": "https://evil.example"}) as ws:
            ws.receive_text()
    for bad in ("http://127.0.0.1:8796", "http://localhost:8796"):  # no built-in helper-service origins any more
        with pytest.raises(WebSocketDisconnect), client.websocket_connect("/ws/control", headers={"origin": bad}) as ws:
            ws.receive_text()
    ok = (None, "http://testserver")
    for origin in ok:
        headers = {"origin": origin} if origin else {}
        with client.websocket_connect("/ws/control", headers=headers) as ws:
            assert json.loads(ws.receive_text())["type"] == "status"


def test_extra_origin_from_env(monkeypatch) -> None:
    monkeypatch.setenv("PYNEBI_ALLOWED_ORIGINS", "https://cockpit.example:8443")
    app = create_app(start_simulator=False)
    with TestClient(app) as client:
        assert client.post("/api/connection/disconnect", headers={"origin": "https://cockpit.example:8443"}).status_code == 200
        assert client.post("/api/connection/disconnect", headers={"origin": "https://cockpit.example"}).status_code == 403


def test_cross_site_posts_refused(app_client) -> None:
    _app, client = app_client
    assert client.post("/api/motion/estop", headers={"origin": "http://evil.example"}).status_code == 403
    assert client.post("/api/media/format", headers={"referer": "http://evil.example/page"}).status_code == 403
    files = {"file": ("fw.tar", b"x" * 10)}
    assert client.post("/api/system/ota/upload", files=files, headers={"origin": "http://evil.example"}).status_code == 403
    # no Origin/Referer (curl, nebi CLI) and same-origin stay allowed
    assert client.post("/api/motion/stand").status_code == 200
    assert client.post("/api/motion/stand", headers={"origin": "http://testserver"}).status_code == 200
    assert client.post("/api/motion/stand", headers={"referer": "http://testserver/"}).status_code == 200
    # cross-site GETs are harmless reads and stay allowed (img/video embeds)
    assert client.get("/api/health", headers={"origin": "http://evil.example"}).status_code == 200


def test_dns_rebinding_host_refused(app_client) -> None:
    _app, client = app_client
    assert client.get("/api/status", headers={"host": "evil.example:8000"}).status_code == 403
    assert client.get("/api/health", headers={"host": "127.0.0.1:8000"}).status_code == 200
    assert client.get("/api/health", headers={"host": "localhost:8000"}).status_code == 200
    assert client.get("/api/health", headers={"host": "192.0.2.30:8000"}).status_code == 200
    with (
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect("/ws/control", headers={"host": "rebind.evil.example:8000"}) as ws,
    ):
        ws.receive_text()


def test_token_required_for_remote_peers_when_exposed() -> None:
    app = create_app(start_simulator=False)
    configure_security(app, bind_host="0.0.0.0", token="s3cret-token")
    with TestClient(app) as client:  # peer "testclient" is not loopback
        assert client.get("/").status_code == 200
        assert client.get("/static/js/main.js").status_code == 200
        assert client.get("/api/status").status_code == 403
        assert client.get("/api/status", headers={"x-pynebi-token": "wrong"}).status_code == 403
        assert client.get("/api/status", headers={"x-pynebi-token": "s3cret-token"}).status_code == 200
        assert client.get("/api/status?token=s3cret-token").status_code == 200
        assert client.get("/api/status", cookies={"pynebi_token": "s3cret-token"}).status_code == 200
        with pytest.raises(WebSocketDisconnect), client.websocket_connect("/ws/control") as ws:
            ws.receive_text()
        with client.websocket_connect("/ws/control?token=s3cret-token") as ws:
            assert json.loads(ws.receive_text())["type"] == "status"


# ---------------------------------------------------------------- SSID cache


@pytest.mark.asyncio
async def test_ssid_cache_never_blocks_loop() -> None:
    calls: list[int] = []

    def slow_fetch() -> str:
        calls.append(threading.get_ident())
        time.sleep(0.4)
        return "HomeNet"

    cache = cm.WifiSsidCache(ttl=10.0, fetch=slow_fetch)
    t0 = time.perf_counter()
    for _ in range(50):
        assert cache.get() is None
    assert time.perf_counter() - t0 < 0.05
    assert await cache.refresh_now() == "HomeNet"
    assert len(calls) == 1 and calls[0] != threading.get_ident()
    assert cache.get() == "HomeNet"  # fresh: no new refresh
    await asyncio.sleep(0)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_status_dict_does_not_shell_out_on_loop(monkeypatch) -> None:
    threads: list[int] = []

    def fake_ssid(_iface=None):
        threads.append(threading.get_ident())
        time.sleep(0.3)
        return "X"

    monkeypatch.setattr(wifi_cache, "get_current_wifi_ssid", fake_ssid)
    mgr = cm.NebiServerManager(start_simulator=False)
    t0 = time.perf_counter()
    for _ in range(30):
        mgr.status_dict()
    assert time.perf_counter() - t0 < 0.1
    for _ in range(100):
        if mgr.wifi_ssid.value == "X":
            break
        await asyncio.sleep(0.01)
    assert mgr.connection_summary()["current_wifi_ssid"] == "X"
    assert threads and all(t != threading.get_ident() for t in threads)
    await mgr.shutdown()


# ---------------------------------------------------------------- video queue policy


def test_video_queue_never_drops_idr_for_p_frames(monkeypatch) -> None:
    requests: list[int] = []
    hub = VideoStreamHub(on_keyframe_needed=lambda: requests.append(1))
    monkeypatch.setattr(hub, "_ensure_worker", lambda: None)  # no consumer: queue fills up
    hub.on_h265_access_unit(b"P0", False, 0)  # before any IDR: ignored
    assert not hub._queue
    hub.on_h265_access_unit(b"I1", True, 1)
    for i in range(5):
        hub.on_h265_access_unit(f"P{i}".encode(), False, 2 + i)
    assert len(hub._queue) == 6 and hub._queue[0][1] == b"I1"
    # overflow with only the head IDR to resync on: drop all and skip P-frames until the next IDR
    hub.on_h265_access_unit(b"Px", False, 10)
    assert not hub._queue and requests == [1]
    hub.on_h265_access_unit(b"Py", False, 11)
    assert not hub._queue
    hub.on_h265_access_unit(b"I2", True, 12)
    for i in range(3):
        hub.on_h265_access_unit(f"Q{i}".encode(), False, 13 + i)
    hub.on_h265_access_unit(b"I3", True, 20)
    hub.on_h265_access_unit(b"R0", False, 21)
    assert len(hub._queue) == 6
    hub.on_h265_access_unit(b"R1", False, 22)  # overflow: resync on newest queued IDR (I3)
    assert [it[1] for it in hub._queue] == [b"I3", b"R0", b"R1"]
    # an IDR on a full queue replaces everything before it
    for i in range(3):
        hub.on_h265_access_unit(f"S{i}".encode(), False, 30 + i)
    hub.on_h265_access_unit(b"I4", True, 40)
    assert [it[1] for it in hub._queue] == [b"I4"]
    hub.reset_decoder()
    assert hub.latest_jpeg is None and not hub._queue


# ---------------------------------------------------------------- media range proxy


def test_media_range_proxy(app_client) -> None:
    app, client = app_client
    app.state.manager.simulator.capture_photo()  # firmware has no photo command; seed one directly
    files = client.get("/api/media/files").json()["files"]
    fid = files[0]["file_id"]
    full = client.get(f"/api/media/files/{fid}/content")
    assert full.status_code == 200 and full.headers.get("accept-ranges") == "bytes"
    total = len(full.content)
    part = client.get(f"/api/media/files/{fid}/content", headers={"range": "bytes=0-63"})
    assert part.status_code == 206
    assert part.headers["content-range"] == f"bytes 0-63/{total}"
    assert part.content == full.content[:64]
    tail = client.get(f"/api/media/files/{fid}/content", headers={"range": "bytes=-100"})
    assert tail.status_code == 206 and tail.content == full.content[-100:]
    assert tail.headers["content-range"] == f"bytes {total - 100}-{total - 1}/{total}"


# ---------------------------------------------------------------- cancellable link operations


@pytest.mark.asyncio
async def test_link_op_is_cancelled_by_disconnect() -> None:
    mgr = cm.NebiServerManager(start_simulator=False)
    started = asyncio.Event()

    async def slow_ble_wait() -> dict:
        started.set()
        await asyncio.sleep(30)
        return {}

    pending = asyncio.create_task(mgr.run_link_op("ble_provision_home_wifi", slow_ble_wait))
    await started.wait()
    assert mgr.connection_summary()["link_op"] == "ble_provision_home_wifi"
    t0 = time.monotonic()
    res = await mgr.disconnect()
    assert time.monotonic() - t0 < 1.0
    assert res["connection"]["stage"] == "disconnected"
    with pytest.raises(NebiError) as ei:
        await pending
    assert ei.value.code == "LINK_OP_CANCELLED"
    await mgr.shutdown()


@pytest.mark.asyncio
async def test_link_op_non_nebi_error_is_structured() -> None:
    mgr = cm.NebiServerManager(start_simulator=False)

    async def boom() -> dict:
        try:
            raise OSError("bluetooth adapter off")
        except OSError as exc:
            raise mgr.fail_link(exc, "turn bluetooth on") from exc

    with pytest.raises(NebiError) as ei:
        await mgr.run_link_op("ble_bootstrap", boom)
    assert ei.value.code == "CONNECTION_FAILED"
    assert mgr.current_stage == "error" and mgr.last_error["hint"] == "turn bluetooth on"
    await mgr.shutdown()


# ---------------------------------------------------------------- pure helpers


def test_resolve_flow_maps_legacy_robot_mode() -> None:
    from nebi_cockpit.schemas import ConnectionSwitchBody as B

    assert cm.resolve_flow(B()) == "simulator"
    assert cm.resolve_flow(B(mode="auto", flow=" LAN_STA ")) == "lan_sta"
    assert cm.resolve_flow(B(mode="robot", host="192.168.123.1")) == "usb_ncm"
    assert cm.resolve_flow(B(mode="robot")) == "softap"
    assert cm.resolve_flow(B(mode="robot", host="192.168.7.1")) == "softap"
    assert cm.resolve_flow(B(mode="robot", host="192.0.2.40")) == "lan_sta"
    assert cm.resolve_flow(B(mode="robot", host="127.0.0.1", ucp_port=19007)) == cm.DIRECT_FLOW
    assert cm.resolve_flow(B(mode="robot", ucp_port=19007)) == "softap"  # custom ports need a host


def test_deadman_query_parsing() -> None:
    from nebi_cockpit.ws_routes import parse_deadman_ms

    assert parse_deadman_ms(None) == 0.5
    assert parse_deadman_ms("200") == 0.2
    assert parse_deadman_ms("50") == 0.2
    assert parse_deadman_ms("99999") == 5.0
    assert parse_deadman_ms("soon") == 0.5


def test_cli_parser_defaults_to_serve() -> None:
    from nebi_cockpit.cli import _cmd_serve, build_parser

    parser = build_parser()
    args = parser.parse_args(["serve", "--port", "8012", "--disable-extension", "nebi-cli"])
    assert args.func is _cmd_serve and args.port == 8012 and not args.sim and not args.no_extensions
    assert parser.parse_args(["serve", "--sim"]).sim
    assert not parser.parse_args(["serve", "--no-sim"]).sim  # old flag still accepted (now the default)
    assert args.disable_extension == ["nebi-cli"]
    assert parser.parse_args([]).cmd is None
