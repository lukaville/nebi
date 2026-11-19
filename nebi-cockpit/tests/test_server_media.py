"""Cockpit media tab API: album states (ok / empty / auth error / unreachable), sidecar folding, typed proxy."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from nebi.http import media_support as ms
from nebi_cockpit.app import create_app


@pytest.fixture()
def app_client(monkeypatch, tmp_path):
    monkeypatch.setattr(ms.Path, "home", lambda: tmp_path)  # no stray ~/.config/pynebi/media_token
    monkeypatch.delenv(ms.MEDIA_TOKEN_ENV, raising=False)
    app = create_app(start_simulator=True)
    with TestClient(app) as client:
        yield app, client


def test_media_listing_and_status(app_client) -> None:
    app, client = app_client
    app.state.manager.simulator.capture_photo()  # firmware has no photo command; seed one directly
    status = client.get("/api/media/status").json()
    assert status["ok"] and status["state"] == "ok" and status["stats"]["photos"] >= 2

    files = client.get("/api/media/files?limit=200").json()
    assert files["ok"] and files["count"] == len(files["files"]) >= 2
    assert all(f["file_type"] not in ("thumbnail", "lrv") for f in files["files"])
    photo = next(f for f in files["files"] if f["file_type"] == "photo")
    assert photo["thumbnail_file_id"] == photo["file_id"] and photo["mime_type"] == "image/jpeg"
    assert client.get(f"/api/media/files/{photo['file_id']}").json()["filename"] == photo["filename"]
    assert "sessions" in client.get("/api/media/sessions").json()


def test_sidecar_thumbnail_proxy_is_typed(app_client) -> None:
    app, client = app_client
    sim = app.state.manager.simulator
    sim.rec_frames = [sim.render_fpv_frame(0.0)]
    sim.rec_started_at = 0.0
    sim.finalize_video_recording()
    vid = next(f for f in client.get("/api/media/files").json()["files"] if f["file_type"] == "video")
    r = client.get(f"/api/media/files/{vid['thumbnail_file_id']}/content")
    # the robot's media server sends .thm as octet-stream; the proxy types it from content-disposition
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg" and r.content[:2] == b"\xff\xd8"


def test_media_auth_error_is_explicit(app_client, monkeypatch) -> None:
    app, client = app_client
    monkeypatch.setattr(ms.MediaTokenProvider, "_resolve", lambda self: [("test", "rotated-by-firmware")])
    app.state.manager.client.media_http.tokens.reset()

    r = client.get("/api/media/files")
    body = r.json()
    assert r.status_code == 502 and body["ok"] is False
    assert body["error"]["code"] == "MEDIA_AUTH_REQUIRED" and body["error"]["hint"]
    status = client.get("/api/media/status").json()
    assert status["state"] == "auth_error" and status["error"]["code"] == "MEDIA_AUTH_REQUIRED"


def test_media_unreachable_state(app_client) -> None:
    app, client = app_client
    http = app.state.manager.client.media_http
    http._client.base_url = "http://127.0.0.1:1"
    status = client.get("/api/media/status").json()
    assert status["state"] == "unreachable" and status["error"]["code"] == "MEDIA_UNREACHABLE"
    r = client.get("/api/media/files")
    assert r.json()["error"]["code"] == "MEDIA_UNREACHABLE"


def test_media_proxy_encodes_file_id_once(app_client) -> None:
    """Regression: the cockpit used to quote() the id and the media client quoted it again (`%20` -> `%2520`)."""
    app, client = app_client
    http = app.state.manager.client.media_http
    paths: list[str] = []
    orig = http._open_stream

    async def spy(path: str, headers: dict[str, str]):  # type: ignore[no-untyped-def]
        paths.append(path)
        return await orig(path, headers)

    http._open_stream = spy
    client.get("/api/media/files/clip%20one.mp4/content")  # unknown on the simulator: the status is irrelevant
    assert paths and paths[0] == "/media/files/clip%20one.mp4/content"
