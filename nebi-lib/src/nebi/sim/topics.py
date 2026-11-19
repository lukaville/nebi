"""Robot-side handlers for `app/**` command topics (what the robot's motion controller, app bridge and network service answer).

Each handler is registered for one topic in `TOPIC_HANDLERS`; `dispatch_app_topic` is subscribed on the device
bus by `MockNebiRobot`. To simulate a new command, add a function decorated with `@_handler("/cmd/...")`.
"""

from __future__ import annotations

import asyncio
import math
import socket
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from google.protobuf.message import Message

from nebi.catalog.registry import CATALOG
from nebi.intercom import AudioOperation
from nebi.transport.zenoh_bus import HEADERLESS_TOPICS
from nebi.types import (
    SOFTAP_HOST,
    CameraCmdType,
    CameraSubMode,
    GameAction,
    JoystickState,
    MissionId,
    PetSubMode,
    RobotPostureState,
)

if TYPE_CHECKING:
    from nebi.sim.robot import MockNebiRobot

TopicHandler = Callable[["MockNebiRobot", Any, int], None]

# topic -> (handler, needs_data): a `needs_data` handler is skipped when the message has no `data` field.
TOPIC_HANDLERS: dict[str, tuple[TopicHandler, bool]] = {}


def _handler(topic: str, *, needs_data: bool = False) -> Callable[[TopicHandler], TopicHandler]:
    def register(fn: TopicHandler) -> TopicHandler:
        if topic in TOPIC_HANDLERS:
            raise ValueError(f"duplicate simulator handler for {topic}")
        TOPIC_HANDLERS[topic] = (fn, needs_data)
        return fn

    return register


def dispatch_app_topic(robot: MockNebiRobot, topic: str, msg: Message, seq: int) -> None:
    """Route one decoded `app/**` message to its handler (unknown topics are ignored, like the robot)."""
    entry = TOPIC_HANDLERS.get(topic)
    if entry is None:
        return
    handler, needs_data = entry
    if topic in HEADERLESS_TOPICS and msg.HasField("header"):
        return  # the robot's app-bridge codec rejects a header on these topics and drops the message
    d = getattr(msg, "data", None)
    if needs_data and d is None:
        return
    handler(robot, d, seq)


@_handler("/cmd/rc/joystick", needs_data=True)
def _cmd_rc_joystick(robot: MockNebiRobot, d: Any, _seq: int) -> None:
    st = JoystickState.unpack(int(d.joystick))
    robot.apply_joystick_state(st, from_ucp_wire=False)


@_handler("/cmd/mission/request", needs_data=True)
def _cmd_mission_request(robot: MockNebiRobot, d: Any, seq: int) -> None:
    mission, sub = int(d.mission_id), int(d.sub_mode)
    # Like the robot's motion controller: ego/pet interaction is acknowledged but only entered while standing.
    ego = mission == int(MissionId.CAMERA) and sub == int(CameraSubMode.EGO_INTERACTION)
    if not ego or robot.posture_state == RobotPostureState.BALANCING:
        robot.mission_id = mission
        robot.sub_mode = sub
    if robot.mission_id == int(MissionId.PET) and robot.sub_mode == int(PetSubMode.DOCK):
        robot.posture_state = RobotPostureState.DOCKING
    elif robot.mission_id == int(MissionId.PET) and robot.sub_mode == int(PetSubMode.UNDOCK):
        robot.docked = False
        robot.charging = False
        robot.posture_state = RobotPostureState.BALANCING
        robot.pos_x -= 0.6 * math.cos(robot.yaw_rad)
        robot.pos_y -= 0.6 * math.sin(robot.yaw_rad)
    robot.bus.publish(
        "/cmd/mission/request_ack",
        seq_num=seq,
        result_code=0,
        current_mission_id=robot.mission_id,
        current_sub_mode=robot.sub_mode,
    )


@_handler("/cmd/vt/set_mode_req", needs_data=True)
def _cmd_vt_set_mode_req(robot: MockNebiRobot, d: Any, seq: int) -> None:
    robot.vt_streaming = int(d.mode) > 0
    if robot.vt_streaming:
        robot.force_idr = True
    robot.bus.publish("/cmd/vt/set_mode_rsp", seq_num=seq, ret_code=0)


@_handler("/cmd/vt/status_req", needs_data=False)
def _cmd_vt_status_req(robot: MockNebiRobot, _d: Any, seq: int) -> None:
    robot.bus.publish(
        "/cmd/vt/status_rsp",
        seq_num=seq,
        ready=1 if robot.vt_streaming else 0,
        target_fps=robot.target_fps,
        target_kbps=robot.vt_bitrate_kbps,
        restart=0,
        target_resolution=1,
    )


@_handler("/cmd/vt/reset_conn_req", needs_data=False)
def _cmd_vt_reset_conn_req(robot: MockNebiRobot, _d: Any, seq: int) -> None:
    robot.force_idr = True
    robot.bus.publish("/cmd/vt/reset_conn_rsp", seq_num=seq, ret_code=0)


@_handler("/cmd/vt/disconn_req", needs_data=False)
def _cmd_vt_disconn_req(robot: MockNebiRobot, _d: Any, seq: int) -> None:
    robot.vt_streaming = False
    robot.bus.publish("/cmd/vt/disconn_rsp", seq_num=seq, ret_code=0)


@_handler("/cmd/vt/req_iframe", needs_data=False)
def _cmd_vt_req_iframe(robot: MockNebiRobot, _d: Any, _seq: int) -> None:
    robot.force_idr = True


@_handler("/cmd/vt/set_bitrate_req", needs_data=True)
def _cmd_vt_set_bitrate_req(robot: MockNebiRobot, d: Any, _seq: int) -> None:
    robot.vt_bitrate_kbps = max(500, min(12000, int(d.bitrate)))


@_handler("/cmd/vt/set_fps_req", needs_data=True)
def _cmd_vt_set_fps_req(robot: MockNebiRobot, d: Any, _seq: int) -> None:
    robot.target_fps = max(10, min(60, int(d.fps)))


@_handler("/cmd/camera/unified_cmd", needs_data=True)
def _cmd_camera_unified_cmd(robot: MockNebiRobot, d: Any, _seq: int) -> None:
    # Mirrors the real camera: no still-photo (CAPTURE) branch, zoom in tenths 10..20,
    # highlight only while recording, horizon lock not supported, EIS always on (level only).
    mask = CameraCmdType(int(d.cmd_type))
    if mask & CameraCmdType.ZOOM and 10 <= int(d.zoom_data) <= 20:
        robot.zoom_x = int(d.zoom_data) / 10.0
    if mask & CameraCmdType.EIS:
        robot.eis_enabled = True
        robot.eis_level = int(d.eis_level)
    if mask & CameraCmdType.RECORD_PROFILE and not robot.recording:
        robot.record_profile_id = int(d.record_profile_id)
    if mask & CameraCmdType.HIGHLIGHT_TAG and robot.recording:
        robot.rec_highlights.append(round(time.monotonic() - robot.rec_started_at, 2))


@_handler("/cmd/camera/config_switch_cmd", needs_data=True)
def _cmd_camera_config_switch_cmd(robot: MockNebiRobot, d: Any, _seq: int) -> None:
    robot.record_profile_id = int(d.profile_id)


_RECORD_STOP_RSP_DELAY_S = 3.0  # the real camera finalizes on a worker; its stop reply often arrives late


@_handler("/cmd/camera/record_req", needs_data=True)
def _cmd_camera_record_req(robot: MockNebiRobot, d: Any, seq: int) -> None:
    # Camera service record txn: 1 = start, 2 = stop; anything else is rejected with ret_code 1.
    act = int(d.action)

    def reply(ret_code: int) -> None:
        robot.bus.publish(
            "/cmd/camera/record_rsp",
            seq_num=seq,
            action=act,
            request_id=int(d.request_id),
            ret_code=ret_code,
            state=1 if robot.recording else 0,
        )

    if act == 1:
        if not robot.recording:
            robot.recording = True
            robot.rec_started_at = time.monotonic()
            robot.rec_frames.clear()
            robot.rec_highlights.clear()
        reply(0)
    elif act == 2 and robot.recording:
        robot.recording = False
        robot.finalize_video_recording()
        asyncio.get_running_loop().call_later(_RECORD_STOP_RSP_DELAY_S, reply, 0)
    else:
        reply(1)


@_handler("/cmd/camera/capture_controls_set_req", needs_data=True)
def _cmd_camera_capture_controls_set_req(robot: MockNebiRobot, d: Any, seq: int) -> None:
    robot.pro_mode = bool(d.is_pro_mode)
    robot.ev_comp = float(d.ev_compensation)
    if int(d.wb_kelvin) > 0:
        robot.wb_kelvin = int(d.wb_kelvin)
    robot.bus.publish(
        "/cmd/camera/capture_controls_set_rsp",
        seq_num=seq,
        result=0,
        applied_mask=int(d.update_mask),
        failed_mask=0,
    )
    robot.bus.publish(
        "/status/camera/capture_controls",
        is_pro_mode=robot.pro_mode,
        wb_kelvin=robot.wb_kelvin,
        ev_compensation=robot.ev_comp,
    )


@_handler("/cmd/camera/pre_record_set_req", needs_data=True)
def _cmd_camera_pre_record_set_req(robot: MockNebiRobot, d: Any, seq: int) -> None:
    robot.pre_record_enabled = bool(int(d.enabled))
    robot.bus.publish("/cmd/camera/pre_record_set_rsp", seq_num=seq, error_code=0)


@_handler("/cmd/camera/relative_trajectory_replay_catalog_query_req", needs_data=False)
def _cmd_camera_relative_trajectory_replay_catalog_query_req(robot: MockNebiRobot, _d: Any, seq: int) -> None:
    cls = CATALOG.get_proto_class("/cmd/camera/relative_trajectory_replay_catalog_query_rsp")
    if cls is not None:
        rsp = cls()
        rsp.data.ret_code = 0
        rsp.data.catalog_format_version = 1
        presets = [("orbit_360", "360° Cinematic Orbit", 180), ("dolly_in", "Dramatic Push-In", 120)]
        rsp.data.trajectory_count = len(presets)
        for tid, name, sc in presets:
            desc = rsp.data.descriptors.add()
            tb = tid.encode("utf-8")
            nb = name.encode("utf-8")
            desc.id_len = len(tb)
            desc.id = tb
            desc.name_len = len(nb)
            desc.name = nb
            desc.sample_count = sc
        robot.bus.publish_proto("/cmd/camera/relative_trajectory_replay_catalog_query_rsp", rsp, seq_num=seq)


@_handler("/cmd/tracking/ctrl_cmd", needs_data=True)
def _cmd_tracking_ctrl_cmd(robot: MockNebiRobot, d: Any, seq: int) -> None:
    if int(d.start_tracking) == 0:
        robot.tracking_enabled = False
        robot.locked_target_id = 0
    else:
        sel = int(d.selected_id)
        if sel == 0 and int(d.request_mode) == 2 and robot.latest_boxes:
            # Pick closest projected target to (selected_pos_x, selected_pos_y)
            px, py = float(d.selected_pos_x), float(d.selected_pos_y)
            best = min(
                robot.latest_boxes,
                key=lambda b: (b["cx"] - px) ** 2 + (b["cy"] - py) ** 2,
            )
            sel = int(best["id"])
        elif sel == 0:
            sel = 101
        robot.tracking_enabled = True
        robot.locked_target_id = sel
    robot.bus.publish(
        "/cmd/tracking/ctrl_cmd_ack",
        seq_num=seq,
        result=0,
        selected_id=robot.locked_target_id,
        start_tracking=1 if robot.tracking_enabled else 0,
    )


@_handler("/cmd/tracking/model_switch_cmd", needs_data=True)
def _cmd_tracking_model_switch_cmd(robot: MockNebiRobot, d: Any, seq: int) -> None:
    robot.target_type_filter = int(d.target_type)
    robot.bus.publish(
        "/cmd/tracking/model_switch_cmd_ack",
        seq_num=seq,
        ret_code=0,
        current_target_type=robot.target_type_filter,
    )


@_handler("/cmd/tracking/algo_switch_cmd", needs_data=True)
def _cmd_tracking_algo_switch_cmd(robot: MockNebiRobot, d: Any, seq: int) -> None:
    robot.bus.publish(
        "/cmd/tracking/algo_switch_cmd_ack",
        seq_num=seq,
        ret_code=0,
        current_model_version=int(d.model_version),
    )


@_handler("/cmd/tracking/param_update_req", needs_data=True)
def _cmd_tracking_param_update_req(robot: MockNebiRobot, d: Any, seq: int) -> None:
    if int(d.param_id) == 1:
        robot.follow_distance_m = max(0.8, min(5.0, float(d.param_value)))
    elif int(d.param_id) == 2:
        robot.orbit_speed = max(0.1, min(2.0, float(d.param_value)))
    robot.bus.publish("/cmd/tracking/param_update_rsp", seq_num=seq, ret_code=0)


@_handler("/cmd/pnl/mode_ctrl_req", needs_data=True)
def _cmd_pnl_mode_ctrl_req(robot: MockNebiRobot, d: Any, seq: int) -> None:
    prev = robot.pnl_mode
    robot.pnl_mode = int(d.pnl_mode)
    robot.bus.publish(
        "/cmd/pnl/mode_ctrl_rsp",
        seq_num=seq,
        result_code=0,
        previous_pnl_mode=prev,
        current_pnl_mode=robot.pnl_mode,
    )


@_handler("/cmd/pnl/mode_query_req", needs_data=False)
def _cmd_pnl_mode_query_req(robot: MockNebiRobot, _d: Any, seq: int) -> None:
    robot.bus.publish("/cmd/pnl/mode_query_rsp", seq_num=seq, result_code=0, current_pnl_mode=robot.pnl_mode)


@_handler("/cmd/audio/op_req", needs_data=True)
def _cmd_audio_op_req(robot: MockNebiRobot, d: Any, seq: int) -> None:
    op = int(d.operation_type)
    ret = 0
    if op == AudioOperation.START_RECORD:
        robot.set_intercom_active(True)
    elif op in (AudioOperation.STOP_RECORD, AudioOperation.RESET):
        robot.set_intercom_active(False)
        if op == AudioOperation.RESET:
            robot.intercom.reset_decoder()
    elif op != AudioOperation.PCM_STREAM:
        ret = 1  # the robot rejects unsupported operation types
    robot.bus.publish("/cmd/audio/op_rsp", seq_num=seq, ret_code=ret)


@_handler("/data/audio/playback_data", needs_data=True)
def _data_audio_playback_data(robot: MockNebiRobot, d: Any, _seq: int) -> None:
    robot.intercom.on_playback(d)


@_handler("/cmd/speaker/play_cmd", needs_data=False)
def _cmd_speaker_play_cmd(robot: MockNebiRobot, _d: Any, seq: int) -> None:
    robot.bus.publish("/cmd/speaker/play_rsp", seq_num=seq, ret_code=0)


@_handler("/cmd/audio/play_file_req", needs_data=True)
def _cmd_audio_play_file_req(robot: MockNebiRobot, d: Any, seq: int) -> None:
    robot.bus.publish(
        "/cmd/audio/play_file_rsp",
        seq_num=seq,
        return_code=0,
        session_id=int(d.session_id),
        timestamp_ns=time.time_ns(),
    )


@_handler("/cmd/audio/voice_session_control", needs_data=True)
def _cmd_audio_voice_session_control(robot: MockNebiRobot, d: Any, _seq: int) -> None:
    robot.voice_wakeup = int(d.state) > 0


@_handler("/cmd/mic/scan_req", needs_data=False)
def _cmd_mic_scan_req(robot: MockNebiRobot, _d: Any, seq: int) -> None:
    robot.bus.publish("/cmd/mic/scan_rsp", seq_num=seq, ret_code=0)
    cls = CATALOG.get_proto_class("/status/mic/scan_result")
    if cls is not None:
        m = cls()
        m.data.scanning = 0
        e = m.data.entries.add()
        e.mac = b"AA:BB:CC:11:22:33"
        e.name = b"SimMic-01"
        e.rssi = -48
        e.model_id = 1
        e.saved = 1
        robot.bus.publish_proto("/status/mic/scan_result", m)


@_handler("/cmd/mic/source_select_req", needs_data=True)
def _cmd_mic_source_select_req(robot: MockNebiRobot, d: Any, seq: int) -> None:
    robot.active_mic_kind = int(d.kind)
    robot.active_mic_mac = bytes(d.mac).decode("utf-8", errors="replace")
    robot.bus.publish("/cmd/mic/source_select_rsp", seq_num=seq, ret_code=0)


@_handler("/cmd/mic/forget_req", needs_data=False)
def _cmd_mic_forget_req(robot: MockNebiRobot, _d: Any, seq: int) -> None:
    robot.bus.publish("/cmd/mic/forget_rsp", seq_num=seq, ret_code=0, removed_count=1)


@_handler("/cmd/game/control_req", needs_data=True)
def _cmd_game_control_req(robot: MockNebiRobot, d: Any, seq: int) -> None:
    act = int(d.action)
    robot.active_game = int(d.game_type)
    if act in (int(GameAction.START), int(GameAction.RESTART)):
        robot.game_state = 1
        robot.game_score = 0.0
    elif act == int(GameAction.PAUSE):
        robot.game_state = 2
    elif act == int(GameAction.RESUME):
        robot.game_state = 1
    elif act == int(GameAction.STOP):
        robot.game_state = 0
        robot.active_game = 0
    robot.bus.publish(
        "/cmd/game/control_rsp",
        seq_num=seq,
        ret_code=0,
        timestamp_ns=time.time_ns(),
        game_type=robot.active_game,
        action=act,
        session_id=int(d.session_id),
    )


@_handler("/cmd/game/player_input", needs_data=True)
def _cmd_game_player_input(robot: MockNebiRobot, d: Any, _seq: int) -> None:
    if robot.game_state == 1 and len(d.attitude_deg) >= 2:
        pitch_in, roll_in = float(d.attitude_deg[0]), float(d.attitude_deg[1])
        robot.vx_mps = max(-1.5, min(1.5, -pitch_in / 25.0))
        robot.yaw_rate_rps = max(-1.8, min(1.8, roll_in / 25.0))
        robot.game_score += 0.5


@_handler("/cmd/game/menu_get_req", needs_data=False)
def _cmd_game_menu_get_req(robot: MockNebiRobot, _d: Any, seq: int) -> None:
    robot.bus.publish(
        "/cmd/game/menu_get_rsp",
        seq_num=seq,
        ret_code=0,
        effective_slots=bytes([1, 2, 3, 4, 5]),
    )


@_handler("/cmd/common/ping_req", needs_data=True)
def _cmd_common_ping_req(robot: MockNebiRobot, d: Any, seq: int) -> None:
    robot.bus.publish(
        "/cmd/common/ping_rsp",
        seq_num=seq,
        ret_code=0,
        ping_data=bytes(d.ping_data),
    )


@_handler("/cmd/common/get_version_req", needs_data=False)
def _cmd_common_get_version_req(robot: MockNebiRobot, _d: Any, seq: int) -> None:
    robot.bus.publish(
        "/cmd/common/get_version_rsp",
        seq_num=seq,
        ret_code=0,
        major_version=1,
        minor_version=4,
        hardware_version=b"LZ110-X5-SIM",
        loader_version=b"uboot-2024.04",
        firmware_version=b"v1.4.2-sim",
        is_mass_product=1,
    )


@_handler("/cmd/robot/sn_req", needs_data=True)
def _cmd_robot_sn_req(robot: MockNebiRobot, d: Any, seq: int) -> None:
    sn = b"LZ110SIM2026X5"
    robot.bus.publish(
        "/cmd/robot/sn_rsp",
        seq_num=seq,
        sn_type=int(d.sn_type),
        lens=len(sn),
        sn=sn,
    )


@_handler("/cmd/time/correction_req", needs_data=False)
def _cmd_time_correction_req(robot: MockNebiRobot, _d: Any, seq: int) -> None:
    robot.bus.publish("/cmd/time/correction_rsp", seq_num=seq, ret_code=0)


@_handler("/cmd/parameter/query_req", needs_data=True)
def _cmd_parameter_query_req(robot: MockNebiRobot, d: Any, seq: int) -> None:
    pid = int(d.param_id)
    spec = CATALOG.param_by_id(pid)
    val = float(robot.config_store.get(spec.key, 0.0)) if spec else 0.0
    robot.bus.publish(
        "/cmd/parameter/query_rsp",
        seq_num=seq,
        ret_code=0 if spec else 1,
        mode_type=int(d.mode_type),
        param_id=pid,
        param_value=val,
    )


@_handler("/cmd/parameter/update_req", needs_data=True)
def _cmd_parameter_update_req(robot: MockNebiRobot, d: Any, seq: int) -> None:
    pid = int(d.param_id)
    spec = CATALOG.param_by_id(pid)
    if spec is not None:
        robot.config_store[spec.key] = round(float(d.param_value), 4)
    robot.bus.publish("/cmd/parameter/update_rsp", seq_num=seq, ret_code=0)


@_handler("/cmd/led/effect_ctrl", needs_data=True)
def _cmd_led_effect_ctrl(robot: MockNebiRobot, d: Any, _seq: int) -> None:
    robot.led_effect = bytes(d.csv_path).decode("utf-8", errors="replace").rstrip("\x00")


@_handler("/cmd/led/ctrl_oneway", needs_data=True)
def _cmd_led_ctrl_oneway(robot: MockNebiRobot, d: Any, _seq: int) -> None:
    robot.led_frames += 1
    robot.led_last_frame_t = time.monotonic()
    if d.rgbw_data:
        w = int(d.rgbw_data[0])
        robot.led_rgb = ((w >> 24) & 0xFF, (w >> 16) & 0xFF, (w >> 8) & 0xFF)


@_handler("/cmd/rc/vibration_set_req", needs_data=True)
def _cmd_rc_vibration_set_req(robot: MockNebiRobot, d: Any, seq: int) -> None:
    robot.bus.publish("/cmd/rc/vibration_set_rsp", seq_num=seq, level=int(d.level))


@_handler("/cmd/rc/function_key_mode", needs_data=False)
def _cmd_rc_function_key_mode(robot: MockNebiRobot, _d: Any, seq: int) -> None:
    robot.bus.publish("/cmd/rc/function_key_mode_ack", seq_num=seq, ret_code=0)


@_handler("/cmd/hms/action_response", needs_data=True)
def _cmd_hms_action_response(robot: MockNebiRobot, d: Any, _seq: int) -> None:
    """The app confirmed an HMS action request: the simulator treats that as the user recovering the fault."""
    pair = robot.hms_pending_commands.pop(int(d.command_id), None)
    if pair is not None and int(d.result_code) == 0:
        robot.clear_sim_hms_fault(pair[0], pair[1])


@_handler("/status/hms/query_active", needs_data=False)
def _status_hms_query_active(robot: MockNebiRobot, d: Any, seq: int) -> None:
    robot.publish_hms_snapshot(seq_num=seq, request_id=int(getattr(d, "request_id", 0)) if d is not None else 0)


@_handler("/cmd/nm/wifi_scan_req", needs_data=False)
def _cmd_nm_wifi_scan_req(robot: MockNebiRobot, _d: Any, seq: int) -> None:
    cls = CATALOG.get_proto_class("/cmd/nm/wifi_scan_rsp")
    if cls is not None:
        rsp = cls()
        rsp.data.result = 0
        demo_aps = [
            ("Studio-5G", 36, -41, 2),
            ("Robotics-Lab", 6, -53, 2),
            ("Guest-Open", 11, -68, 0),
        ]
        rsp.data.ap_count = len(demo_aps)
        rsp.data.total_found = len(demo_aps)
        for ssid, ch, rssi, sec in demo_aps:
            ap = rsp.data.aps.add()
            sb = ssid.encode("utf-8")
            ap.ssid_len = len(sb)
            ap.ssid = sb
            ap.channel = ch
            ap.rssi = rssi
            ap.security = sec
        robot.bus.publish_proto("/cmd/nm/wifi_scan_rsp", rsp, seq_num=seq)


@_handler("/cmd/nm/wifi_connect_req", needs_data=True)
def _cmd_nm_wifi_connect_req(robot: MockNebiRobot, d: Any, seq: int) -> None:
    ssid = bytes(d.ssid[: d.ssid_len or len(d.ssid)]).decode("utf-8", errors="replace").rstrip("\x00")
    psk = bytes(d.psk[: d.psk_len or len(d.psk)]).decode("utf-8", errors="replace").rstrip("\x00")
    robot.pending_wifi_ssid = ssid or robot.wifi_ssid
    robot.pending_wifi_psk = psk
    if any(d.uuid):
        robot.prov_uuid = bytes(d.uuid)
    else:
        robot.wifi_ssid = robot.pending_wifi_ssid
    robot.bus.publish(
        "/cmd/nm/wifi_connect_rsp",
        seq_num=seq,
        result=0 if ssid else 1,
        ssid_len=len(robot.pending_wifi_ssid),
        ssid=robot.pending_wifi_ssid.encode("utf-8"),
    )


@_handler("/cmd/nm/wifi_provision_start_req", needs_data=True)
def _cmd_nm_wifi_provision_start_req(robot: MockNebiRobot, d: Any, seq: int) -> None:
    robot.prov_uuid = bytes(d.uuid) or robot.prov_uuid
    robot.prov_state = 1  # RUNNING
    robot.prov_fail_reason = 0
    robot.bus.publish(
        "/cmd/nm/wifi_provision_start_rsp",
        seq_num=seq,
        result=0,
        timeout_sec=15,
        total_timeout_sec=30,
        uuid=robot.prov_uuid,
    )


@_handler("/cmd/nm/wifi_provision_commit_req", needs_data=True)
def _cmd_nm_wifi_provision_commit_req(robot: MockNebiRobot, d: Any, seq: int) -> None:
    tx_uuid = bytes(d.uuid) or robot.prov_uuid
    robot.prov_uuid = tx_uuid
    robot.bus.publish(
        "/cmd/nm/wifi_provision_commit_rsp",
        seq_num=seq,
        result=0,
        timeout_sec=15,
        total_timeout_sec=30,
        uuid=tx_uuid,
    )
    # Simulate nm_switch_watchdog validation: wrong password or NOT_FOUND rolls back to AP!
    if robot.pending_wifi_ssid.upper() == "NOT_FOUND":
        robot.prov_state = 3  # FAILED
        robot.prov_fail_reason = 2  # NETWORK_NOT_FOUND
        robot.wifi_mode = 0
        robot.wifi_ipv4 = SOFTAP_HOST
    elif robot.pending_wifi_psk.lower() in ("wrong", "wrongpass", "badpassword") or robot.pending_wifi_ssid.upper() == "FAIL_TEST":
        robot.prov_state = 3  # FAILED
        robot.prov_fail_reason = 1  # WRONG_KEY
        robot.wifi_mode = 0
        robot.wifi_ipv4 = SOFTAP_HOST
    else:
        robot.prov_state = 2  # SUCCEEDED
        robot.prov_fail_reason = 0
        robot.wifi_mode = 1  # STA_CONNECTED
        robot.wifi_ssid = robot.pending_wifi_ssid or "Studio-5G"
        robot.wifi_ipv4 = robot.host
        robot.ble_peripheral.advertising = False
        sb = robot.wifi_ssid.encode("utf-8")
        robot.bus.publish(
            "/status/nm/net_online_notify",
            trigger_source=1,
            net_type=1,
            channel=36,
            rssi=-41,
            ipv4=socket.inet_aton(robot.host),
            ssid_len=len(sb),
            ssid=sb,
            uuid=tx_uuid,
        )


@_handler("/cmd/nm/wifi_provision_result_req", needs_data=True)
def _cmd_nm_wifi_provision_result_req(robot: MockNebiRobot, d: Any, seq: int) -> None:
    req_uuid = bytes(d.uuid)
    matched = 1 if (not req_uuid or req_uuid == robot.prov_uuid) else 0
    sb = (robot.wifi_ssid if robot.prov_state == 2 else robot.pending_wifi_ssid).encode("utf-8")
    ip_bytes = socket.inet_aton(robot.wifi_ipv4 if "." in robot.wifi_ipv4 else "127.0.0.1")
    robot.bus.publish(
        "/cmd/nm/wifi_provision_result_rsp",
        seq_num=seq,
        result=0,
        uuid_match=matched,
        state=robot.prov_state,
        fail_reason=robot.prov_fail_reason,
        uuid=robot.prov_uuid,
        ssid_len=len(sb),
        ssid=sb,
        ipv4=ip_bytes,
        channel=36 if robot.wifi_mode == 1 else 6,
        rssi=-41,
    )


@_handler("/cmd/nm/wifi_switch_to_station_req", needs_data=False)
def _cmd_nm_wifi_switch_to_station_req(robot: MockNebiRobot, _d: Any, seq: int) -> None:
    robot.wifi_mode = 1
    robot.wifi_ipv4 = robot.host
    robot.ble_peripheral.advertising = False
    robot.bus.publish("/cmd/nm/wifi_switch_to_station_rsp", seq_num=seq, result=0, current_mode=1)


@_handler("/cmd/nm/wifi_switch_to_station_result_req", needs_data=False)
def _cmd_nm_wifi_switch_to_station_result_req(robot: MockNebiRobot, _d: Any, seq: int) -> None:
    robot.bus.publish(
        "/cmd/nm/wifi_switch_to_station_result_rsp",
        seq_num=seq,
        valid=1,
        pending=0,
        result=0,
        current_mode=robot.wifi_mode,
    )


@_handler("/cmd/nm/wifi_switch_to_ap_req", needs_data=False)
def _cmd_nm_wifi_switch_to_ap_req(robot: MockNebiRobot, _d: Any, _seq: int) -> None:
    robot.wifi_mode = 0
    robot.wifi_ssid = robot.ble_peripheral.ssid
    robot.wifi_ipv4 = SOFTAP_HOST
    robot.prov_state = 0
    robot.ble_peripheral.advertising = True


@_handler("/cmd/storage/op_req", needs_data=False)
def _cmd_storage_op_req(robot: MockNebiRobot, _d: Any, seq: int) -> None:
    robot.media_files.clear()
    robot.bus.publish("/cmd/storage/op_rsp", seq_num=seq, ret_code=0)


@_handler("/cmd/ota/upgrade_control_req", needs_data=False)
def _cmd_ota_upgrade_control_req(robot: MockNebiRobot, _d: Any, seq: int) -> None:
    robot.ota_stage = 2
    robot.ota_progress = 10
    robot.bus.publish(
        "/cmd/ota/upgrade_control_rsp",
        seq_num=seq,
        ret_code=0,
        current_upgrade_stage=robot.ota_stage,
    )


@_handler("/cmd/system/factory_reset_req", needs_data=False)
def _cmd_system_factory_reset_req(robot: MockNebiRobot, _d: Any, seq: int) -> None:
    robot.bus.publish("/cmd/system/factory_reset_rsp", seq_num=seq, ret_code=0)
