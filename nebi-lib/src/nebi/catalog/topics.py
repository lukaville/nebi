"""Zenoh topic table: every topic nebi can send or decode, with its Protobuf message and delivery hints.

Each row names the topic path (without the `app/` / `device/` key prefix), which side sends it and whether it is
delivered reliably. The Protobuf module lives in `nebi.proto.md.protocol.<group>` where `<group>` is the first
path segment (`cmd`, `status`, `data`); by default the module name is the rest of the path joined with `_`
(`/cmd/rc/joystick` -> `cmd.rc_joystick_pb2`), and `msg=` overrides it. The message class is the module name in
CamelCase (`rc_joystick` -> `RcJoystick`).
"""

from __future__ import annotations

from dataclasses import dataclass

OUT = "out"    # app -> robot
IN = "in"      # robot -> app
BOTH = "both"  # either side may send
RELIABLE = True


@dataclass(frozen=True)
class TopicSpec:
    """One row of the topic table."""

    path: str        # "/cmd/rc/joystick"
    direction: str   # OUT | IN | BOTH
    reliable: bool   # sent with reliable delivery (best effort otherwise)
    module: str      # Protobuf module relative to nebi.proto.md.protocol, e.g. "cmd.rc_joystick_pb2"
    message: str     # Protobuf class name, e.g. "RcJoystick"

    @property
    def app_sends(self) -> bool:
        return self.direction in (OUT, BOTH)

    @property
    def app_receives(self) -> bool:
        return self.direction in (IN, BOTH)


def _camel(stem: str) -> str:
    return "".join(part.capitalize() for part in stem.split("_") if part)


def _t(path: str, direction: str, reliable: bool = False, *, msg: str | None = None) -> TopicSpec:
    group, *rest = path.strip("/").split("/")
    stem = msg or "_".join(rest)
    return TopicSpec(path, direction, reliable, f"{group}.{stem}_pb2", _camel(stem))


TOPICS: tuple[TopicSpec, ...] = (
    _t("/cmd/audio/op_req", OUT),
    _t("/cmd/audio/op_rsp", IN),
    _t("/cmd/audio/play_file_req", OUT, RELIABLE),
    _t("/cmd/audio/play_file_rsp", IN, RELIABLE),
    _t("/cmd/audio/voice_session_control", OUT),
    _t("/cmd/camera/capture_controls_set_req", OUT, msg="capture_controls_set_req"),
    _t("/cmd/camera/capture_controls_set_rsp", IN, msg="capture_controls_set_rsp"),
    _t("/cmd/camera/config_switch_cmd", OUT),
    _t("/cmd/camera/pre_record_get_req", OUT),
    _t("/cmd/camera/pre_record_get_rsp", IN),
    _t("/cmd/camera/pre_record_set_req", OUT),
    _t("/cmd/camera/pre_record_set_rsp", IN),
    _t("/cmd/camera/record_req", OUT),
    _t("/cmd/camera/record_rsp", IN),
    _t("/cmd/camera/relative_trajectory_replay", OUT, msg="camera_relative_trajectory_replay_cmd"),
    _t("/cmd/camera/relative_trajectory_replay_catalog_query_req", OUT),
    _t("/cmd/camera/relative_trajectory_replay_catalog_query_rsp", IN),
    _t("/cmd/camera/relative_trajectory_replay_query_req", OUT),
    _t("/cmd/camera/relative_trajectory_replay_query_rsp", IN),
    _t("/cmd/camera/relative_trajectory_replay_upload_and_start_rsp", IN),
    _t("/cmd/camera/unified_cmd", OUT),
    _t("/cmd/capability/hardware_snapshot_req", OUT, RELIABLE),
    _t("/cmd/capability/hardware_snapshot_rsp", IN, RELIABLE),
    _t("/cmd/capability/software_snapshot_req", OUT, RELIABLE),
    _t("/cmd/common/accessory_activation_report_ack", OUT),
    _t("/cmd/common/accessory_control_req", OUT),
    _t("/cmd/common/accessory_control_rsp", IN),
    _t("/cmd/common/accessory_deactivate_req", OUT),
    _t("/cmd/common/accessory_deactivate_rsp", IN),
    _t("/cmd/common/accessory_list_req", OUT),
    _t("/cmd/common/accessory_list_rsp", IN),
    _t("/cmd/common/get_crypto_info_req", OUT),
    _t("/cmd/common/get_crypto_info_rsp", IN),
    _t("/cmd/common/get_module_version_req", OUT),
    _t("/cmd/common/get_module_version_rsp", IN),
    _t("/cmd/common/get_parameter_req", OUT),
    _t("/cmd/common/get_parameter_rsp", IN),
    _t("/cmd/common/get_partition_size_req", OUT),
    _t("/cmd/common/get_partition_size_rsp", IN),
    _t("/cmd/common/get_version_req", OUT),
    _t("/cmd/common/get_version_rsp", IN),
    _t("/cmd/common/ping_req", OUT),
    _t("/cmd/common/ping_rsp", IN),
    _t("/cmd/common/set_parameter_req", OUT),
    _t("/cmd/common/set_parameter_rsp", IN),
    _t("/cmd/game/control_req", OUT, RELIABLE),
    _t("/cmd/game/control_rsp", IN, RELIABLE),
    _t("/cmd/game/menu_get_req", OUT, RELIABLE),
    _t("/cmd/game/menu_get_rsp", IN, RELIABLE),
    _t("/cmd/game/menu_set_req", OUT, RELIABLE),
    _t("/cmd/game/menu_set_rsp", IN, RELIABLE),
    _t("/cmd/game/player_input", OUT),
    _t("/cmd/gateway/analytics_event_receipt", OUT, RELIABLE),
    _t("/cmd/hms/action_request", IN),
    _t("/cmd/hms/action_response", OUT),
    _t("/cmd/led/ctrl_oneway", OUT),
    _t("/cmd/led/effect_ctrl", OUT),
    _t("/cmd/log/with_metadata_req", OUT),
    _t("/cmd/md_gateway/mqtt_endpoint_update_req", OUT),
    _t("/cmd/mic/forget_req", OUT),
    _t("/cmd/mic/forget_rsp", IN),
    _t("/cmd/mic/saved_list_req", OUT),
    _t("/cmd/mic/saved_list_rsp", IN),
    _t("/cmd/mic/scan_req", OUT),
    _t("/cmd/mic/scan_rsp", IN),
    _t("/cmd/mic/source_select_req", OUT),
    _t("/cmd/mic/source_select_rsp", IN),
    _t("/cmd/mission/request", OUT),
    _t("/cmd/mission/request_ack", IN),
    _t("/cmd/nm/wifi_connect_req", OUT, RELIABLE),
    _t("/cmd/nm/wifi_connect_rsp", IN, RELIABLE),
    _t("/cmd/nm/wifi_provision_commit_req", OUT, RELIABLE),
    _t("/cmd/nm/wifi_provision_commit_rsp", IN, RELIABLE),
    _t("/cmd/nm/wifi_provision_result_req", OUT, RELIABLE),
    _t("/cmd/nm/wifi_provision_result_rsp", IN, RELIABLE),
    _t("/cmd/nm/wifi_provision_start_req", OUT, RELIABLE),
    _t("/cmd/nm/wifi_provision_start_rsp", IN, RELIABLE),
    _t("/cmd/nm/wifi_scan_req", OUT, RELIABLE),
    _t("/cmd/nm/wifi_scan_rsp", IN, RELIABLE),
    _t("/cmd/nm/wifi_switch_to_ap_req", OUT),
    _t("/cmd/nm/wifi_switch_to_station_req", OUT, RELIABLE),
    _t("/cmd/nm/wifi_switch_to_station_result_req", OUT, RELIABLE),
    _t("/cmd/nm/wifi_switch_to_station_result_rsp", IN, RELIABLE),
    _t("/cmd/nm/wifi_switch_to_station_rsp", IN, RELIABLE),
    _t("/cmd/ota/upgrade_control_req", OUT),
    _t("/cmd/ota/upgrade_control_rsp", IN),
    _t("/cmd/parameter/query_req", OUT),
    _t("/cmd/parameter/query_rsp", IN),
    _t("/cmd/parameter/update_req", OUT),
    _t("/cmd/parameter/update_rsp", IN),
    _t("/cmd/pnl/mode_ctrl_req", OUT),
    _t("/cmd/pnl/mode_ctrl_rsp", IN),
    _t("/cmd/pnl/mode_query_req", OUT),
    _t("/cmd/pnl/mode_query_rsp", IN),
    _t("/cmd/rc/function_key_mode", OUT),
    _t("/cmd/rc/function_key_mode_ack", IN),
    _t("/cmd/rc/joystick", BOTH),
    _t("/cmd/rc/joystick_calib_req", OUT),
    _t("/cmd/rc/vibration_get_req", OUT),
    _t("/cmd/rc/vibration_get_rsp", IN),
    _t("/cmd/rc/vibration_set_req", OUT),
    _t("/cmd/rc/vibration_set_rsp", IN),
    _t("/cmd/real_rl/control_ack", IN, RELIABLE),
    _t("/cmd/real_rl/control_request", OUT, RELIABLE),
    _t("/cmd/robot/sn_req", OUT),
    _t("/cmd/robot/sn_rsp", IN),
    _t("/cmd/security/core_board_sn_req", OUT, msg="device_security_core_board_sn_req"),
    _t("/cmd/security/core_board_sn_rsp", IN, msg="device_security_core_board_sn_rsp"),
    _t("/cmd/security/device_activate_req", OUT, msg="device_security_device_activate_req"),
    _t("/cmd/security/device_activate_rsp", IN, msg="device_security_device_activate_rsp"),
    _t("/cmd/security/device_activation_state_req", OUT, msg="device_security_device_activation_state_req"),
    _t("/cmd/security/device_activation_state_rsp", IN, msg="device_security_device_activation_state_rsp"),
    _t("/cmd/security/device_attest_req", OUT, msg="device_security_device_attest_req"),
    _t("/cmd/security/device_attest_rsp", IN, msg="device_security_device_attest_rsp"),
    _t("/cmd/security/device_deactivate_req", OUT, msg="device_security_device_deactivate_req"),
    _t("/cmd/security/device_deactivate_rsp", IN, msg="device_security_device_deactivate_rsp"),
    _t("/cmd/security/e2e_provision_req", OUT, msg="device_security_e2e_provision_req"),
    _t("/cmd/security/e2e_provision_rsp", IN, msg="device_security_e2e_provision_rsp"),
    _t("/cmd/security/local_tls_identity_req", OUT, msg="device_security_local_tls_identity_req"),
    _t("/cmd/security/local_tls_identity_rsp", IN, msg="device_security_local_tls_identity_rsp"),
    _t("/cmd/snapback/capture_control", OUT),
    _t("/cmd/speaker/play_cmd", OUT),
    _t("/cmd/speaker/play_rsp", IN),
    _t("/cmd/storage/op_req", OUT),
    _t("/cmd/storage/op_rsp", IN),
    _t("/cmd/system/factory_reset_req", OUT),
    _t("/cmd/system/factory_reset_rsp", IN),
    _t("/cmd/test/result", OUT),
    _t("/cmd/time/correction_req", OUT),
    _t("/cmd/time/correction_rsp", IN),
    _t("/cmd/tracking/algo_switch_cmd", OUT),
    _t("/cmd/tracking/algo_switch_cmd_ack", IN),
    _t("/cmd/tracking/ctrl_cmd", OUT),
    _t("/cmd/tracking/ctrl_cmd_ack", IN),
    _t("/cmd/tracking/model_switch_cmd", OUT),
    _t("/cmd/tracking/model_switch_cmd_ack", IN),
    _t("/cmd/tracking/param_query_req", OUT),
    _t("/cmd/tracking/param_query_rsp", IN),
    _t("/cmd/tracking/param_update_req", OUT),
    _t("/cmd/tracking/param_update_rsp", IN),
    _t("/cmd/vt/disconn_req", IN),
    _t("/cmd/vt/disconn_rsp", OUT),
    _t("/cmd/vt/req_iframe", OUT),
    _t("/cmd/vt/reset_conn_req", OUT),
    _t("/cmd/vt/reset_conn_rsp", IN),
    _t("/cmd/vt/set_bitrate_req", OUT),
    _t("/cmd/vt/set_fps_req", OUT),
    _t("/cmd/vt/set_mode_req", BOTH, RELIABLE),
    _t("/cmd/vt/set_mode_rsp", BOTH, RELIABLE),
    _t("/cmd/vt/set_resolution_req", OUT),
    _t("/cmd/vt/status_req", BOTH),
    _t("/cmd/vt/status_rsp", BOTH, RELIABLE),
    _t("/data/audio/data", BOTH),
    _t("/data/audio/playback_data", OUT),
    _t("/data/main/image_stream", IN),
    _t("/data/robot/chase_game_params", OUT),
    _t("/data/tracking/target_data", IN),
    _t("/data/vt/perf_data", IN),
    _t("/status/audio/status", IN),
    _t("/status/camera/capture_controls", IN, msg="capture_controls_status"),
    _t("/status/camera/relative_trajectory_replay_catalog", IN),
    _t("/status/camera/relative_trajectory_replay_status", IN),
    _t("/status/camera/status", IN),
    _t("/status/capability/voice_language", IN, RELIABLE, msg="voice_language_status"),
    _t("/status/common/accessory_activation_report", IN),
    _t("/status/function/status", IN),
    _t("/status/game/analytics_context", IN),
    _t("/status/game/status", IN),
    _t("/status/game/tug_status", IN, msg="tug_status"),
    _t("/status/gateway/analytics_event_transfer", IN, RELIABLE),
    _t("/status/hms/active_snapshot", IN),
    _t("/status/hms/event_broadcast", IN),
    _t("/status/hms/query_active", OUT),
    _t("/status/media/catalog_changed", IN),
    _t("/status/mic/saved_list", IN),
    _t("/status/mic/scan_result", IN),
    _t("/status/mic/status", IN),
    _t("/status/nm/net_online_notify", IN),
    _t("/status/ota/upgrade_progress", IN),
    _t("/status/pmu/osd_status", IN),
    _t("/status/rc/fsm_state", IN),
    _t("/status/rc/joystick_calib_progress", IN),
    _t("/status/rc/joystick_raw", IN),
    _t("/status/real_rl/status", IN),
    _t("/status/robot/status", IN),
    _t("/status/scheduler/status", IN),
    _t("/status/snapback/ready", IN),
    _t("/status/storage/status", IN),
    _t("/status/tracking/status", IN),
    _t("/status/uwb/rc_status_list", IN),
    _t("/status/wifi/status", BOTH),
)
