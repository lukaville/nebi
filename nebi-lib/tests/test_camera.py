"""Camera domain against the simulator, which mirrors the LZ110 firmware's camera behaviour.

Firmware facts the simulator encodes (see `nebi.domains.camera`): `unified_cmd` is dropped if it carries a
header, zoom is in tenths (10..20), there is no still-photo capture, highlights need an active recording, and
`record_req` uses action 1 = start / 2 = stop with a late stop reply (confirmed from camera status instead).
"""

from __future__ import annotations

import asyncio
import time

import pytest

from nebi import AsyncNebiClient
from nebi.exceptions import NebiError
from nebi.sim.topics import dispatch_app_topic
from nebi.simulator import MockNebiRobot
from nebi.transport.zenoh_bus import HEADERLESS_TOPICS
from nebi.types import CameraCmdType


async def _wait(cond, timeout: float = 2.0) -> bool:  # type: ignore[no-untyped-def]
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        await asyncio.sleep(0.05)
    return bool(cond())


def test_unified_cmd_is_headerless_and_dropped_with_header() -> None:
    assert "/cmd/camera/unified_cmd" in HEADERLESS_TOPICS
    assert "/cmd/camera/relative_trajectory_replay" in HEADERLESS_TOPICS
    from nebi.catalog.registry import CATALOG

    cls = CATALOG.get_proto_class("/cmd/camera/unified_cmd")
    assert cls is not None

    class _Robot:
        zoom_x = 1.0
        recording = False

    robot = _Robot()
    msg = cls()
    msg.data.cmd_type = int(CameraCmdType.ZOOM)
    msg.data.zoom_data = 15
    msg.header.seq_num = 7  # firmware drops this
    dispatch_app_topic(robot, "/cmd/camera/unified_cmd", msg, 7)  # type: ignore[arg-type]
    assert robot.zoom_x == 1.0
    msg.ClearField("header")
    dispatch_app_topic(robot, "/cmd/camera/unified_cmd", msg, 0)  # type: ignore[arg-type]
    assert robot.zoom_x == 1.5


@pytest.mark.asyncio
async def test_camera_zoom_photo_highlight_and_recording() -> None:
    sim = MockNebiRobot(ucp_port=0, http_port=0, video_fps=20)
    await sim.start()
    try:
        async with AsyncNebiClient(**sim.client_kwargs()) as robot:
            cam = robot.camera
            assert cam.photo_supported is False

            # Zoom: tenths on the wire, clamped to the firmware's 1.0..2.0x
            await cam.set_zoom(1.5)
            assert await _wait(lambda: sim.zoom_x == 1.5)
            assert await _wait(lambda: robot.telemetry.zoom_x == 1.5)
            await cam.set_zoom(4.0)
            assert await _wait(lambda: sim.zoom_x == 2.0)
            await cam.set_zoom(1.0)
            assert await _wait(lambda: sim.zoom_x == 1.0)

            # No still photos on this firmware: clear, LLM-friendly error
            with pytest.raises(NebiError) as exc:
                await cam.take_photo()
            assert exc.value.code == "CAMERA_PHOTO_UNSUPPORTED" and exc.value.hint and not exc.value.recoverable
            with pytest.raises(NebiError) as exc:
                await cam.set_horizon_lock(True)
            assert exc.value.code == "CAMERA_HORIZON_LOCK_UNSUPPORTED"

            # Highlight outside a recording is refused once camera status is known
            assert await _wait(lambda: cam._status_recording() is False)
            with pytest.raises(NebiError) as exc:
                await cam.mark_highlight()
            assert exc.value.code == "CAMERA_NOT_RECORDING"

            start = await cam.start_recording()
            assert start["recording"] is True and start["ret_code"] == 0
            assert sim.recording
            await asyncio.sleep(0.3)
            await cam.mark_highlight()
            assert await _wait(lambda: len(sim.rec_highlights) == 1)

            # Stop uses action=2; the simulated reply is late (like the robot), so status confirms it
            t0 = time.monotonic()
            stop = await cam.stop_recording()
            assert stop["recording"] is False and stop["confirmed_by"] == "status"
            assert time.monotonic() - t0 < 2.5
            assert not sim.recording
    finally:
        await sim.stop()


@pytest.mark.asyncio
async def test_record_req_invalid_action_is_rejected() -> None:
    sim = MockNebiRobot(ucp_port=0, http_port=0, video_fps=20)
    await sim.start()
    try:
        async with AsyncNebiClient(**sim.client_kwargs()) as robot:
            rsp = await robot.bus.request(
                "/cmd/camera/record_req", "/cmd/camera/record_rsp", timeout=2.0, action=0, request_id=1
            )
            assert int(rsp.data.ret_code) == 1  # action 0 is not "stop" on this firmware
            assert not sim.recording
    finally:
        await sim.stop()


@pytest.mark.asyncio
async def test_late_record_reply_for_other_request_id_does_not_confirm() -> None:
    """Regression (H1): a late `record_rsp` from an earlier transaction (other request_id, no seq echo) must not
    confirm this one; without a matching reply or camera status the transaction stays unconfirmed."""
    from nebi.catalog.registry import CATALOG
    from nebi.domains.camera import CameraController
    from nebi.transport.zenoh_ucp import ZenohTopicBus

    class _Sess:
        def put(self, *_a: object, **_kw: object) -> None:
            pass

    bus = ZenohTopicBus(role="app")
    bus._session = _Sess()  # type: ignore[assignment]
    bus._loop = asyncio.get_running_loop()
    cam = CameraController(bus)
    txn = asyncio.create_task(cam.stop_recording(timeout=0.6))
    await asyncio.sleep(0.05)
    cls = CATALOG.get_proto_class("/cmd/camera/record_rsp")
    assert cls is not None
    late = cls()
    late.data.ret_code = 0
    late.data.request_id = 12345  # not ours
    late.data.state = 1
    bus._dispatch_decoded("device/cmd/camera/record_rsp", "/cmd/camera/record_rsp", late.SerializeToString())
    with pytest.raises(NebiError) as exc_info:
        await txn
    assert exc_info.value.code == "RECORD_STATE_UNCONFIRMED"
