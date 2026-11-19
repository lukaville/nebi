"""End-to-end integration tests for `AsyncNebiClient` and `MockNebiRobot` (H.265 decoding: nebi-cockpit tests)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from nebi import AsyncNebiClient, CameraSubMode, GameType, StuntDirection
from nebi.exceptions import NebiError
from nebi.simulator import MockNebiRobot


async def wait_until(predicate: Callable[[], object], timeout: float = 3.0, interval: float = 0.02) -> bool:
    """Poll `predicate` until truthy or `timeout` elapses (instead of fixed sleeps that race the simulator)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        if predicate():
            return True
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(interval)


@pytest.mark.asyncio
async def test_full_sdk_domains_against_simulator() -> None:
    sim = MockNebiRobot(ucp_port=0, http_port=0, video_fps=20)
    await sim.start()
    access_units: list[bytes] = []

    try:
        async with AsyncNebiClient(**sim.client_kwargs()) as robot:
            robot.add_video_listener(lambda au, *_a, **_k: access_units.append(au))
            robot.camera.request_keyframe()

            # 1. System: Ping, Version, SN, Config HTTP :8010, Wi-Fi, HMS, UWB
            ping = await robot.system.ping()
            assert ping["ret_code"] == 0
            assert ping["echo"] == "nebi"

            ver = await robot.system.get_version()
            assert ver["firmware_version"] == "v1.4.2-sim"
            sn = await robot.system.get_serial_number()
            assert sn.startswith("LZ110")

            params_before = await robot.system.get_parameters(["md_function.max_speed"])
            assert "md_function.max_speed" in params_before["values"]
            await robot.system.set_parameters({"md_function.max_speed": 2.4})
            params_after = await robot.system.get_parameters(["md_function.max_speed"])
            assert params_after["values"]["md_function.max_speed"] == 2.4

            nets = await robot.system.scan_wifi()
            assert len(nets) >= 2
            conn_res = await robot.system.connect_wifi("Studio-5G", "secret123")
            assert conn_res["result"] == 0

            # HMS: faults arrive as raise/clear events on /status/hms/event_broadcast (no periodic snapshot)
            sim.inject_hms_fault("0xF0080003", 0)
            assert await wait_until(lambda: len(robot.telemetry.hms_alerts) == 1)
            assert [a.code for a in robot.telemetry.hms_alerts] == ["0xF0080003"]
            assert robot.telemetry.hms_alerts[0].source == "event"
            assert "Obstacle avoidance" in robot.telemetry.hms_alerts[0].message_en
            assert robot.telemetry.hms_events[-1]["action"] == "raised"
            snap = await robot.system.query_active_faults()
            assert snap["fault_codes"] == ["0xF0080003"]
            await robot.system.clear_hms_fault("0xF0080003", 0)
            assert await wait_until(lambda: len(robot.telemetry.hms_alerts) == 0)
            assert len(robot.telemetry.hms_alerts) == 0
            assert robot.telemetry.hms_events[-1]["action"] == "cleared"

            vib = await robot.system.vibrate_remote(slot=0, level=2)
            assert vib["level"] == 2

            # 2. Motion: Stand, Sit/Fold, Stand, Drive, Jump, Flip
            await robot.motion.stand()
            assert await wait_until(lambda: robot.telemetry.posture_name == "BALANCING")
            assert 10 <= robot.telemetry.height_cm <= 12  # default = app resting height raw 16 (~11 cm)
            await robot.motion.sit()
            assert await wait_until(lambda: robot.telemetry.posture_name == "FOLDED")
            await robot.motion.stand()
            assert await wait_until(lambda: robot.telemetry.posture_name == "BALANCING")

            def _driving() -> bool:
                # Keep the sticks fresh like a live UI does: axes expire after AXIS_TTL_S (0.5 s) without input.
                robot.motion.set_axes(ly=0.75, rx=0.15)
                return robot.telemetry.velocity_mps > 0.2

            assert await wait_until(_driving, interval=0.1)
            robot.motion.set_axes(ly=0.0, rx=0.0)

            await robot.motion.jump(height=0.7)
            await robot.motion.flip(StuntDirection.FRONT_FLIP)
            assert not hasattr(robot.motion, "play_motion")  # choreographed motions are not offered

            # Pet / ego mode: refused while folded, confirmed via scheduler status while standing
            await robot.motion.sit()
            assert await wait_until(lambda: robot.telemetry.posture_name == "FOLDED")
            with pytest.raises(NebiError) as exc:
                await robot.motion.start_ego_mode()
            assert exc.value.code == "EGO_MODE_NEEDS_STANDING"
            forced = await robot.motion.start_ego_mode(force=True, confirm_timeout=0.4)
            assert forced["entered"] is False and "hint" in forced
            await robot.motion.stand()
            assert await wait_until(lambda: robot.telemetry.posture_name == "BALANCING")
            ego = await robot.motion.start_ego_mode()
            assert ego["entered"] is True
            await robot.motion.start_manual_camera_mode()

            # 3. Tracking: Lock point & follow mode
            lock = await robot.tracking.lock_point(0.5, 0.5)
            assert lock["start_tracking"] == 1
            fmode = await robot.tracking.set_follow_mode(CameraSubMode.SPOTLIGHT, distance_m=2.0)
            assert fmode["current_sub_mode"] == int(CameraSubMode.SPOTLIGHT)
            await robot.tracking.unlock_target()

            # 4. Camera & Media HTTP :8084: Photo + MP4 Video Recording + Range Download
            await robot.camera.set_zoom(1.5)
            with pytest.raises(NebiError) as exc:
                await robot.camera.take_photo()
            assert exc.value.code == "CAMERA_PHOTO_UNSUPPORTED"
            await robot.camera.start_recording()
            await asyncio.sleep(0.35)
            await robot.camera.mark_highlight()
            await robot.camera.stop_recording()

            listing = await robot.media.list_files()
            files = listing["files"]
            assert any(f["file_type"] == "photo" for f in files)
            vid_file = next(f for f in files if f["file_type"] == "video")

            part_bytes, ctype = await robot.media.download_bytes(vid_file["file_id"], byte_range="bytes=0-63")
            assert len(part_bytes) == 64
            assert "video/mp4" in ctype

            # 5. Games & Audio
            g_start = await robot.games.start_game(GameType.MOTION_SENSE)
            assert g_start["ret_code"] == 0
            robot.games.send_player_input(pitch_deg=-12.0, roll_deg=4.0)
            await robot.games.stop_game()

            sfx = await robot.audio.play_sound_effect(1)
            assert sfx["ret_code"] == 0

            # 6. Verify the H.265 access units arrived (decoding to JPEG is tested in nebi-cockpit)
            assert await wait_until(lambda: len(access_units) >= 3)
    finally:
        await sim.stop()


@pytest.mark.asyncio
async def test_stand_fold_bit59_is_latched() -> None:
    """Real LZ110: bit 59 is a latched switch (0->1 = fold, 1->0 = stand). Fold must keep it raised,
    and unrelated frames (jump, axes) must not drop it, or the robot stands straight back up."""
    sim = MockNebiRobot(ucp_port=0, http_port=0, video_fps=10)
    await sim.start()
    try:
        async with AsyncNebiClient(**sim.client_kwargs()) as robot:
            await robot.motion.stand()
            await asyncio.sleep(0.3)
            assert robot.motion.joystick.stand_up_fold is False
            await robot.motion.sit()
            assert robot.motion.joystick.stand_up_fold is True
            await asyncio.sleep(0.3)
            assert sim.posture_state.name == "FOLDED"
            # frames sent after the fold keep the latch: still folded
            robot.motion.set_axes(ly=0.0)
            await robot.motion.jump()
            await asyncio.sleep(0.3)
            assert robot.motion.joystick.stand_up_fold is True
            assert sim.posture_state.name == "FOLDED"
            await robot.motion.stand()
            await asyncio.sleep(0.3)
            assert robot.motion.joystick.stand_up_fold is False
            assert sim.posture_state.name == "BALANCING"
    finally:
        await sim.stop()


@pytest.mark.asyncio
async def test_reconnect_does_not_stand_up_a_folded_robot(tmp_path, monkeypatch) -> None:
    """The robot remembers the last bit-59 value across client sessions. A new session must not start
    from 0 while the robot is folded with bit 59 = 1 (that 1->0 edge stands it up; seen live 2026-11-16)."""
    monkeypatch.setenv("PYNEBI_LATCH_FILE", str(tmp_path / "latch.json"))
    monkeypatch.setenv("PYNEBI_LATCH_FORCE_KEY", "sim-robot")
    sim = MockNebiRobot(ucp_port=0, http_port=0, video_fps=10)
    await sim.start()
    try:
        async with AsyncNebiClient(**sim.client_kwargs()) as robot:
            await robot.motion.stand()
            await asyncio.sleep(0.3)
            await robot.motion.sit()
            await asyncio.sleep(0.3)
            assert sim.posture_state.name == "FOLDED"
        async with AsyncNebiClient(**sim.client_kwargs()) as robot2:
            await asyncio.sleep(0.6)
            assert robot2.motion.joystick.stand_up_fold is True
            assert sim.posture_state.name == "FOLDED"
            await robot2.motion.stand()
            await asyncio.sleep(0.3)
            assert sim.posture_state.name == "BALANCING"
    finally:
        await sim.stop()


def test_latch_is_per_robot_and_skips_simulator(tmp_path, monkeypatch) -> None:
    """A simulator session must never overwrite the real robot's latch (that stood the robot up live)."""
    from nebi import latch
    monkeypatch.setenv("PYNEBI_LATCH_FILE", str(tmp_path / "latch.json"))
    monkeypatch.delenv("PYNEBI_LATCH_FORCE_KEY", raising=False)
    assert latch.latch_key_for_host("127.0.0.1") is None
    latch.save_latch("192.0.2.10", True)
    latch.save_latch(latch.latch_key_for_host("127.0.0.1"), False)    # simulator: no-op
    assert latch.load_latch("192.0.2.10") is True
    assert latch.apply_latch(0, "192.0.2.10") == latch.BIT59
    assert latch.apply_latch(latch.BIT59, None) == 0
