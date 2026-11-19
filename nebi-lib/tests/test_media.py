"""Media album (robot media server, :8084): bearer token, firmware-shaped simulator, normalization and errors."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio

from nebi.exceptions import NebiError
from nebi.http import media_support as ms
from nebi.http.clients import MediaHttpClient
from nebi.sim.http import build_http_app
from nebi.simulator import MockNebiRobot

# Shape of a firmware `/media/files` page (synthetic values): one recording + its sidecars.
FIRMWARE_PAGE = {
    "items": [
        {"id": 214, "file_name": "VID_20260101_120000_000.thm", "file_type": "thumbnail", "file_size": 123456,
         "download_url": "/files/internal/DCIM/100MONDO/VID_20260101_120000_000.thm", "duration_ms": None,
         "container_duration_ms": None, "width": None, "height": None, "fps": None, "session_id": "VID_20260101_120000",
         "status": "available", "created_at": 1767268800000, "source_platform": "beni_lite", "source_sn": None},
        {"id": 201, "file_name": "VID_20260101_120000_000.mp4", "file_type": "video", "file_size": 1234567890,
         "download_url": "/files/internal/DCIM/100MONDO/VID_20260101_120000_000.mp4", "duration_ms": 61000,
         "container_duration_ms": 61250, "width": 3840, "height": 2160, "fps": 60, "session_id": "VID_20260101_120000",
         "status": "available", "created_at": 1767268800000, "source_platform": "beni", "source_sn": "TEST000000000000"},
        {"id": 200, "file_name": "VID_20260101_120000_000.lrv", "file_type": "lrv", "file_size": 45678901,
         "download_url": "/files/internal/DCIM/100MONDO/VID_20260101_120000_000.lrv", "duration_ms": None,
         "container_duration_ms": None, "width": None, "height": None, "fps": None, "session_id": "VID_20260101_120000",
         "status": "available", "created_at": 1767268800000, "source_platform": "beni_lite", "source_sn": None},
    ],
    "next_cursor": None,
    "has_more": False,
}


def test_no_retail_token_is_shipped() -> None:
    import nebi.http.media_support as mod

    # no module-level string is the retail token (the firmware only checks its SHA-256)
    assert not [k for k, v in vars(mod).items() if isinstance(v, str) and ms.token_matches_firmware(v)]
    assert not ms.token_matches_firmware(ms.SIM_MEDIA_TOKEN) and not ms.token_matches_firmware("nope")
    assert len(ms.MEDIA_TOKEN_SHA256) == 64 and hashlib.sha256(b"x").hexdigest() != ms.MEDIA_TOKEN_SHA256


def test_token_resolution_order_and_rotation(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setattr(ms.Path, "home", lambda: tmp_path)
    monkeypatch.delenv(ms.MEDIA_TOKEN_ENV, raising=False)
    p0 = ms.MediaTokenProvider()
    assert p0.token == ms.SIM_MEDIA_TOKEN and not p0.user_configured

    ms.media_token_file().parent.mkdir(parents=True)
    ms.media_token_file().write_text("from-file\n")
    monkeypatch.setenv(ms.MEDIA_TOKEN_ENV, "from-env")
    p = ms.MediaTokenProvider("explicit")
    seen = [p.token]
    while p.rotate():
        seen.append(p.token)
    assert seen == ["explicit", "from-env", "from-file", ms.SIM_MEDIA_TOKEN]
    assert p.user_configured and p.describe()["matches_retail_firmware"] is False


def test_normalize_firmware_page() -> None:
    files = [ms.normalize_file(f) for f in FIRMWARE_PAGE["items"]]
    vid = next(f for f in files if f["file_type"] == "video")
    assert vid["file_id"] == "201" and vid["filename"] == "VID_20260101_120000_000.mp4"
    assert vid["size_bytes"] == 1234567890 and vid["duration_s"] == pytest.approx(61.0)
    assert vid["created_at"] == pytest.approx(1767268800.0) and vid["mime_type"] == "video/mp4"
    ms.link_aux_files([vid], [f for f in files if f["file_type"] in ms.AUX_FILE_TYPES])
    assert vid["thumbnail_file_id"] == "214" and vid["proxy_file_id"] == "200"

    stats = ms.normalize_stats({"total_videos": 3, "total_photos": 0, "total_sessions": 3, "total_size_bytes": 10})
    assert (stats["videos"], stats["photos"], stats["total_bytes"]) == (3, 0, 10)


# ----------------------------------------------------------------------------- simulator-backed


@pytest.fixture()
def sim(monkeypatch: pytest.MonkeyPatch) -> MockNebiRobot:
    robot = MockNebiRobot(ucp_port=0, http_port=0)
    monkeypatch.setattr(robot.bus, "publish", lambda *_a, **_k: None)  # not started: no Zenoh session
    robot._seed_demo_media()
    robot.capture_photo()
    robot.rec_frames = [robot.render_fpv_frame(0.0), robot.render_fpv_frame(0.1)]
    robot.rec_started_at = 0.0
    robot.rec_highlights = []
    robot.finalize_video_recording()
    return robot


def _media_client(robot: MockNebiRobot, **kw) -> MediaHttpClient:
    client = MediaHttpClient("sim.invalid", **kw)
    client._client = httpx.AsyncClient(transport=httpx.ASGITransport(app=build_http_app(robot)), base_url="http://sim")
    return client


@pytest_asyncio.fixture()
async def media(sim: MockNebiRobot, monkeypatch: pytest.MonkeyPatch, tmp_path) -> AsyncIterator[MediaHttpClient]:
    monkeypatch.setattr(ms.Path, "home", lambda: tmp_path)
    monkeypatch.delenv(ms.MEDIA_TOKEN_ENV, raising=False)
    client = _media_client(sim)
    yield client
    await client.close()


@pytest.mark.asyncio
async def test_simulator_requires_bearer_like_firmware(sim: MockNebiRobot) -> None:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_http_app(sim)), base_url="http://sim") as raw:
        r = await raw.get("/media/stats")
        assert r.status_code == 401 and r.text == "AUTH_REQUIRED" and r.headers["www-authenticate"] == "Bearer"
        r = await raw.get("/media/stats", headers={"Authorization": "Bearer wrong"})
        assert r.status_code == 401
        r = await raw.get("/media/stats", headers={"Authorization": f"Bearer {ms.SIM_MEDIA_TOKEN}"})
        assert r.status_code == 200 and r.json()["total_photos"] == 2
        files = (await raw.get("/media/files", headers={"Authorization": f"Bearer {ms.SIM_MEDIA_TOKEN}"})).json()
        assert set(files) == {"items", "next_cursor", "has_more"} and isinstance(files["items"][0]["id"], int)
        assert (await raw.get("/config/v1/local/health")).status_code == 200  # other daemons are not gated
    assert sim.media_auth_failures == 2


@pytest.mark.asyncio
async def test_list_folds_sidecars_and_downloads(media: MediaHttpClient) -> None:
    stats = await media.stats()
    assert stats["photos"] == 2 and stats["videos"] == 1

    listing = await media.list_files()
    types = sorted(f["file_type"] for f in listing["files"])
    assert types == ["photo", "photo", "video"] and listing["has_more"] is False
    vid = next(f for f in listing["files"] if f["file_type"] == "video")
    assert vid["thumbnail_file_id"] and vid["filename"].endswith(".mp4") and vid["duration_s"] > 0

    only_vids = await media.list_files(file_type="video")  # sidecars fetched separately for filtered pages
    assert [f["thumbnail_file_id"] for f in only_vids["files"]] == [vid["thumbnail_file_id"]]
    assert len((await media.list_files(include_aux=True))["files"]) == 4

    thumb, ctype = await media.download_content(vid["thumbnail_file_id"])
    assert thumb[:2] == b"\xff\xd8" and ctype == "application/octet-stream"  # firmware sends sidecars untyped
    part, ctype = await media.download_content(vid["file_id"], byte_range="bytes=0-63")
    assert len(part) == 64 and ctype == "video/mp4"
    async with media.open_content_stream(vid["file_id"], "bytes=0-9") as resp:
        assert resp.status_code == 206 and resp.headers["content-range"].startswith("bytes 0-9/")

    assert (await media.get_file(vid["file_id"]))["file_id"] == vid["file_id"]
    sessions = await media.list_sessions()
    assert any(s["session_id"] == vid["session_id"] and s["segment_count"] == 1 for s in sessions["sessions"])


@pytest.mark.asyncio
async def test_pagination_cursor(media: MediaHttpClient) -> None:
    first = await media.list_files(limit=1, include_aux=True)
    assert len(first["files"]) == 1 and first["has_more"] and first["next_cursor"]
    second = await media.list_files(limit=1, include_aux=True, cursor=first["next_cursor"])
    assert second["files"][0]["file_id"] != first["files"][0]["file_id"]


@pytest.mark.asyncio
async def test_delete_video_removes_sidecars(media: MediaHttpClient, sim: MockNebiRobot) -> None:
    vid = next(f for f in (await media.list_files())["files"] if f["file_type"] == "video")
    res = await media.delete_file(vid["file_id"])
    assert res["ok"] and res["deleted_count"] == 2
    assert not any(a.file_type in ("video", "thumbnail") for a in sim.media_files.values())
    with pytest.raises(NebiError) as ei:
        await media.get_file(vid["file_id"])
    assert ei.value.code == "MEDIA_NOT_FOUND" and ei.value.hint and ei.value.details["http_status"] == 404


@pytest.mark.asyncio
async def test_rejected_override_falls_back_to_next_candidate(sim: MockNebiRobot, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(ms.Path, "home", lambda: tmp_path)
    monkeypatch.setenv(ms.MEDIA_TOKEN_ENV, "stale-token")
    client = _media_client(sim)
    try:
        assert (await client.stats())["photos"] == 2
        assert client.tokens.source == "simulator token"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_auth_failure_is_structured(sim: MockNebiRobot, monkeypatch) -> None:
    monkeypatch.setattr(ms.MediaTokenProvider, "_resolve", lambda self: [("test", "rotated-by-firmware")])
    client = _media_client(sim)
    try:
        for call in (client.stats(), client.list_files(), client.download_content("1")):
            with pytest.raises(NebiError) as ei:
                await call
            err = ei.value.to_dict()
            assert err["code"] == "MEDIA_AUTH_REQUIRED" and "AUTH_REQUIRED" in err["message"]
            assert "media_token" in err["hint"] and err["details"]["token"]["matches_retail_firmware"] is False
        with pytest.raises(NebiError) as ei:
            async with client.open_content_stream("1"):
                pass
        assert ei.value.code == "MEDIA_AUTH_REQUIRED"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_missing_token_is_explained(sim: MockNebiRobot, monkeypatch) -> None:
    monkeypatch.setattr(ms.MediaTokenProvider, "_resolve", lambda self: [("simulator token", "not-the-sim-token")])
    client = _media_client(sim)
    try:
        with pytest.raises(NebiError) as ei:
            await client.stats()
        assert ei.value.code == "MEDIA_TOKEN_MISSING" and "media_token" in (ei.value.hint or "")
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_unreachable_is_structured() -> None:
    client = MediaHttpClient("127.0.0.1", port=1, timeout=1.0)
    try:
        with pytest.raises(NebiError) as ei:
            await client.list_files()
        assert ei.value.code == "MEDIA_UNREACHABLE" and "8084" in ei.value.hint
    finally:
        await client.close()
