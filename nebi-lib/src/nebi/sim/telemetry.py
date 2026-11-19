"""Periodic `device/status/**` telemetry publishers of the simulated robot."""

from __future__ import annotations

import math
import time
from typing import TYPE_CHECKING

from nebi.catalog.registry import CATALOG
from nebi.types import CameraSubMode, MissionId

if TYPE_CHECKING:
    from nebi.sim.robot import MockNebiRobot


def publish_fast_telemetry(robot: MockNebiRobot) -> None:
    """12.5 Hz: robot status, tracking targets, tracking status."""
    if not robot.bus.is_open:
        return
    obst_cm = int(max(25.0, min(400.0, math.hypot(robot.dock_x - robot.pos_x, robot.dock_y - robot.pos_y) * 100.0)))
    robot.bus.publish(
        "/status/robot/status",
        robot_state=int(robot.posture_state),
        velocity=round(robot.vx_mps * 100.0),
        trip_odom_dis=float(robot.trip_odom_m),
        obstacle_dis=obst_cm,
        pitch_angle=round(robot.pitch_deg * 10.0),
        gimbal_angle=round(robot.gimbal_pitch_deg * 10.0),
        roll_angle=round(robot.roll_deg * 10.0),
        height=round(robot.body_height_cm),
        sport_mode=robot.speed_gear,
        abnormal_flags=1 if robot.hms_active_codes else 0,
        session_odom_dis=float(robot.session_odom_m),
    )

    # Tracking targets
    cls = CATALOG.get_proto_class("/data/tracking/target_data")
    if cls is not None:
        msg = cls()
        msg.data.target_count = len(robot.latest_boxes)
        for b in robot.latest_boxes:
            t = msg.data.targets.add()
            t.id = int(b["id"])
            t.center_pos_x = float(b["cx"])
            t.center_pos_y = float(b["cy"])
            t.width = float(b["w"])
            t.height = float(b["h"])
            t.target_state = 2 if (robot.tracking_enabled and int(b["id"]) == robot.locked_target_id) else 1
            t.reserved_state_flags = 1 if b["kind"] == "pet" else 0
        robot.bus.publish_proto("/data/tracking/target_data", msg)

    robot.bus.publish(
        "/status/tracking/status",
        status=1 if robot.tracking_enabled else 0,
        tracking_id=robot.locked_target_id,
        target_type=robot.target_type_filter,
        horizontal_dis=int(robot.follow_distance_m * 100),
        horizontal_speed=int(abs(robot.vx_mps) * 100),
        uwb_follow_enabled=0,
    )


def publish_slow_telemetry(robot: MockNebiRobot) -> None:
    """2.5 Hz: battery, scheduler, camera, game, Wi-Fi, storage, UWB, OTA and HMS status."""
    if not robot.bus.is_open:
        return
    rec_dur = int(time.monotonic() - robot.rec_started_at) if robot.recording else 0
    robot.bus.publish(
        "/status/pmu/osd_status",
        soc=round(robot.battery_soc),
        power_bits=0x01 | (0x02 if robot.charging else 0) | (0x04 if robot.docked else 0),
        abnormal_flags=0,
    )
    robot.bus.publish(
        "/status/scheduler/status",
        current_mode=robot.mission_id,
        sub_mode=robot.sub_mode,
        request_source=1,
        ego_active=1 if (robot.mission_id == int(MissionId.CAMERA) and robot.sub_mode == int(CameraSubMode.EGO_INTERACTION)) else 0,
        is_locked=1 if robot.charging else 0,
    )
    robot.bus.publish(
        "/status/camera/status",
        rec_state=1 if robot.recording else 0,
        recording_state=1 if robot.recording else 0,
        rec_duration_s=rec_dur,
        zoom_data=round(robot.zoom_x * 10.0),  # tenths, like the camera service
        eis_status=robot.eis_level if robot.eis_enabled else 0,
        current_profile_id=robot.record_profile_id,
        vt_width=robot.video_width,
        vt_height=robot.video_height,
        vt_frame_rate=robot.target_fps,
        vt_bitrate=robot.vt_bitrate_kbps,
    )
    robot.bus.publish(
        "/status/game/status",
        timestamp_ns=time.time_ns(),
        game_type=robot.active_game,
        state=robot.game_state,
        score=[float(robot.game_score)],
    )
    robot.bus.publish(
        "/status/wifi/status",
        tx_bitrate=144,
        rx_bitrate=144,
        rssi=-39,
        connection_status=2 if robot.wifi_mode == 1 else 1,
    )
    used_mb = max(120, int(sum(a.size_bytes for a in robot.media_files.values()) / (1024 * 1024)) + 4200)
    robot.bus.publish(
        "/status/storage/status",
        sd_used_space=used_mb,
        sd_total_space=65536,
        storage_type=1,
        sd_status=1,
    )

    # UWB Remotes (2 paired tags)
    uwb_cls = CATALOG.get_proto_class("/status/uwb/rc_status_list")
    if uwb_cls is not None:
        um = uwb_cls()
        for idx, (bat, rssi, sn) in enumerate([(94, -46, "RC-A108"), (78, -58, "TAG-B204")]):
            e = um.data.entries.add()
            e.battery = bat
            e.is_charging = 0
            e.rssi = rssi
            e.receiver_type = 1
            e.receiver_index = idx
            e.sn_suffix = sn
        robot.bus.publish_proto("/status/uwb/rc_status_list", um)

    if robot.ota_stage > 0:
        robot.bus.publish(
            "/status/ota/upgrade_progress",
            return_code=0,
            upgrade_stage=robot.ota_stage,
            progress_percent=robot.ota_progress,
            current_bytes=robot.ota_progress * 1024,
            total_bytes=100 * 1024,
        )


def publish_hms_event(robot: MockNebiRobot, code: int, instance: int, kind: int) -> None:
    """Like the robot's health monitor: one /status/hms/event_broadcast per raise (kind 1) / clear (kind 2)."""
    if not robot.bus.is_open:
        return
    robot.hms_event_offset += 1
    robot.bus.publish(
        "/status/hms/event_broadcast",
        offset=robot.hms_event_offset,
        module_id=(code >> 16) & 0xFF,
        fault_code=code & 0xFFFFFFFF,
        instance_id=instance,
        kind=kind,
        severity=2,
        timestamp_ms=int(time.time() * 1000),
        state_revision=robot.hms_event_offset,
    )


def publish_hms_action_request(robot: MockNebiRobot, code: int, instance: int) -> None:
    """Like the health monitor's /rpc/app/hms_action reaction: ask the app to confirm the fault (robot -> app)."""
    if not robot.bus.is_open:
        return
    robot.hms_event_offset += 1
    robot.bus.publish(
        "/cmd/hms/action_request",
        command_id=robot.hms_event_offset,
        source_event_offset=robot.hms_event_offset,
        source_fault_code=code & 0xFFFFFFFF,
        action_id=1,
        execution_timeout_ms=1000,
        instance_id=instance,
        operation=1,
        dispatcher_instance_id=7,
        delivery_store_id=robot.hms_event_offset.to_bytes(16, "big"),
    )


def publish_hms_snapshot(robot: MockNebiRobot, seq_num: int | None = None, request_id: int = 0) -> None:
    """The active set; like the real robot, only sent as the answer to /status/hms/query_active."""
    if not robot.bus.is_open:
        return
    robot.bus.publish(
        "/status/hms/active_snapshot",
        seq_num=seq_num,
        request_id=request_id,
        count=len(robot.hms_active_codes),
        module_ids=[1 for _ in robot.hms_active_codes],
        fault_codes=[c for c, _ in robot.hms_active_codes],
        instance_ids=[i for _, i in robot.hms_active_codes],
        severities=[2 for _ in robot.hms_active_codes],
        ret_code=0,
    )
