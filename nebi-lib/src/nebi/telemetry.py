"""Decode robot status/data topics into `RobotTelemetrySnapshot` (kept out of the connection code in client.py).

Each topic has one decoder registered with `@topic_decoder("/status/...")`; `apply_topic_message()` dispatches to
it. New topics plug in by registering a decoder (also from outside this module).
"""

from __future__ import annotations

import socket as _socket
import time
from collections.abc import Callable
from enum import IntEnum
from typing import Any

from nebi.catalog.faults import RECOVERY_HINTS
from nebi.catalog.registry import CATALOG
from nebi.domains._base import decode_fixed_str
from nebi.types import (
    CameraSubMode,
    DetectedTarget,
    GameType,
    HmsAlert,
    MissionId,
    PetSubMode,
    RobotPostureState,
    RobotTelemetrySnapshot,
    UwbRemoteState,
)

_GAME_STATES = {0: "idle", 1: "running", 2: "paused", 3: "finished"}
_OTA_STAGES = {0: "idle", 1: "verifying", 2: "installing", 3: "completed", 4: "failed"}

TopicDecoder = Callable[[RobotTelemetrySnapshot, Any], None]
TOPIC_DECODERS: dict[str, TopicDecoder] = {}


def topic_decoder(topic: str) -> Callable[[TopicDecoder], TopicDecoder]:
    """Register `fn(snapshot, data)` as the decoder for `topic` (replaces an existing one)."""

    def _register(fn: TopicDecoder) -> TopicDecoder:
        TOPIC_DECODERS[topic] = fn
        return fn

    return _register


def _enum_name(enum_cls: type[IntEnum], value: int, unknown_prefix: str) -> str:
    try:
        return enum_cls(value).name
    except ValueError:
        return f"{unknown_prefix}_{value}"


_SUBMODE_ENUMS: dict[int, tuple[type[IntEnum], str]] = {
    int(MissionId.CAMERA): (CameraSubMode, "CAM"),
    int(MissionId.PET): (PetSubMode, "PET"),
    int(MissionId.GAME): (GameType, "GAME"),
}


def _submode_label(mission_id: int, sub_mode: int) -> str:
    if mission_id in _SUBMODE_ENUMS:
        enum_cls, prefix = _SUBMODE_ENUMS[mission_id]
        return _enum_name(enum_cls, sub_mode, prefix)
    return str(sub_mode)


@topic_decoder("/status/robot/status")
def _on_status_robot_status(t: RobotTelemetrySnapshot, d: Any) -> None:
    t.robot_state = int(d.robot_state)
    t.robot_state_ts = time.monotonic()
    t.posture_name = _enum_name(RobotPostureState, t.robot_state, "STATE")
    t.velocity_mps = round(int(d.velocity) / 100.0, 2)
    t.pitch_deg = round(int(d.pitch_angle) / 10.0, 1)
    t.gimbal_pitch_deg = round(int(d.gimbal_angle) / 10.0, 1)
    t.roll_deg = round(int(d.roll_angle) / 10.0, 1)
    t.height_cm = int(d.height)
    t.obstacle_cm = int(d.obstacle_dis)
    t.sport_mode = int(d.sport_mode)
    t.trip_odom_m = round(float(d.trip_odom_dis), 2)
    t.session_odom_m = round(float(d.session_odom_dis), 2)


@topic_decoder("/status/pmu/osd_status")
def _on_status_pmu_osd_status(t: RobotTelemetrySnapshot, d: Any) -> None:
    t.battery_soc = int(d.soc)
    pbits = int(d.power_bits)
    # `power_bits` as observed on a live LZ110:
    #   bit 0 (0x01) = battery present / normal discharge (`power_bits == 0x01` when unplugged on battery)
    #   bit 1 (0x02) = charging (external USB-PD / dock charger active)
    #   bit 2 (0x04) = docked on charging station
    t.charging = bool(pbits & 0x02)
    t.docked = bool(pbits & 0x04)


@topic_decoder("/status/scheduler/status")
def _on_status_scheduler_status(t: RobotTelemetrySnapshot, d: Any) -> None:
    t.mission_id = int(d.current_mode)
    t.sub_mode = int(d.sub_mode)
    t.is_locked = bool(int(getattr(d, "is_locked", 0)))
    t.mission_name = _enum_name(MissionId, t.mission_id, "MISSION")
    t.sub_mode_name = _submode_label(t.mission_id, t.sub_mode)


@topic_decoder("/status/function/status")
def _on_status_function_status(t: RobotTelemetrySnapshot, d: Any) -> None:
    t.is_locked = bool(int(getattr(d, "is_locked", 0)))


@topic_decoder("/status/camera/status")
def _on_status_camera_status(t: RobotTelemetrySnapshot, d: Any) -> None:
    t.recording = bool(int(d.rec_state) == 1 or int(d.recording_state) == 1)
    t.recording_sec = float(d.rec_duration_s)
    if int(d.zoom_data) > 0:
        t.zoom_x = round(int(d.zoom_data) / 10.0, 2)  # tenths: 10 = 1.0x
    t.eis_enabled = int(d.eis_status) > 0
    t.eis_level = int(d.eis_status)
    t.record_profile_id = int(d.current_profile_id)
    if int(d.vt_bitrate) > 0:
        t.vt_bitrate_kbps = int(d.vt_bitrate)
    if int(d.vt_frame_rate) > 0:
        t.vt_fps = int(d.vt_frame_rate)


@topic_decoder("/status/camera/capture_controls")
def _on_status_camera_capture_controls(t: RobotTelemetrySnapshot, d: Any) -> None:
    t.pro_mode = bool(d.is_pro_mode)


@topic_decoder("/status/tracking/status")
def _on_status_tracking_status(t: RobotTelemetrySnapshot, d: Any) -> None:
    t.tracking_active = int(d.status) > 0
    t.locked_target_id = int(d.tracking_id)


@topic_decoder("/data/tracking/target_data")
def _on_data_tracking_target_data(t: RobotTelemetrySnapshot, d: Any) -> None:
    targets: list[DetectedTarget] = []
    for item in d.targets:
        tid = int(item.id)
        label = "pet" if (int(item.reserved_state_flags) & 1) else "person"
        locked = int(item.target_state) == 2 or (t.tracking_active and tid == t.locked_target_id)
        targets.append(
            DetectedTarget(
                target_id=tid,
                label=label,
                confidence=None,  # not reported by the firmware
                cx=round(float(item.center_pos_x), 4),
                cy=round(float(item.center_pos_y), 4),
                width=round(float(item.width), 4),
                height=round(float(item.height), 4),
                distance_m=round(max(0.6, 1.35 / max(0.08, float(item.height))), 2),
                locked=locked,
            )
        )
    t.targets = targets


@topic_decoder("/status/game/status")
def _on_status_game_status(t: RobotTelemetrySnapshot, d: Any) -> None:
    t.active_game = int(d.game_type)
    t.game_state = _GAME_STATES.get(int(d.state), f"state_{int(d.state)}")
    t.game_score = round(float(d.score[0])) if len(d.score) > 0 else 0


@topic_decoder("/status/uwb/rc_status_list")
def _on_status_uwb_rc_status_list(t: RobotTelemetrySnapshot, d: Any) -> None:
    remotes: list[UwbRemoteState] = []
    for entry in d.entries:
        remotes.append(
            UwbRemoteState(
                slot=int(entry.receiver_index),
                mac=str(entry.sn_suffix or f"UWB-{entry.receiver_index}"),
                online=True,
                battery_pct=int(entry.battery),
                distance_m=round(max(0.4, (-int(entry.rssi) - 35) * 0.08), 2),
                azimuth_deg=0.0,
            )
        )
    t.uwb_remotes = remotes


# --- HMS faults --------------------------------------------------------------------------------------------------
# The real robot (LZ110, V26.10.28.x) does not publish /status/hms/active_snapshot on its own (only as the answer to
# /status/hms/query_active); faults arrive as raise/clear events on /status/hms/event_broadcast. `hms_alerts` is the
# ACTIVE fault set maintained from both. Event `kind` values: 1 = asserted (raised), 2 = cleared (any other kind is
# treated as a raise and kept in `hms_events` for diagnosis).
HMS_KIND_RAISE = 1
HMS_KIND_CLEAR = 2
HMS_EVENT_HISTORY = 20


def _epoch_ms(v: int) -> bool:
    """Plausible Unix time in milliseconds (2001..2286), as opposed to a monotonic counter."""
    return 1_000_000_000_000 <= v < 10_000_000_000_000


def hms_alert(code: int, instance: int, *, module_id: int = 0, source: str = "snapshot", ts: float | None = None) -> HmsAlert:
    """Decode one fault into an `HmsAlert` via the fault table (`catalog/faults.py`)."""
    decoded = CATALOG.decode_hms(int(code), int(instance))
    rk = decoded["recovery_kind"]
    return HmsAlert(
        code=decoded["code"],
        instance=int(instance),
        severity=decoded["severity"],
        message_en=decoded["message_en"],
        recovery_kind=rk,
        timestamp=ts if ts is not None else time.time(),
        module_id=int(module_id),
        recovery_hint=RECOVERY_HINTS.get(rk, ""),
        source=source,
    )


@topic_decoder("/status/hms/active_snapshot")
def _on_status_hms_active_snapshot(t: RobotTelemetrySnapshot, d: Any) -> None:
    """Authoritative active set (answer to /status/hms/query_active, or the simulator). Keeps raise times."""
    codes = list(d.fault_codes)
    insts = list(d.instance_ids)
    mods = list(d.module_ids)
    firsts = list(d.first_assert_ts)
    prev = {(a.code, a.instance): a for a in t.hms_alerts}
    alerts: list[HmsAlert] = []
    for idx, fcode in enumerate(codes):
        inst = int(insts[idx]) if idx < len(insts) else 0
        alert = hms_alert(int(fcode), inst, module_id=int(mods[idx]) if idx < len(mods) else 0)
        old = prev.get((alert.code, alert.instance))
        if old is not None:
            alert.timestamp = old.timestamp
            alert.source = old.source
        elif idx < len(firsts) and _epoch_ms(int(firsts[idx])):
            alert.timestamp = int(firsts[idx]) / 1000.0
        alerts.append(alert)
    t.hms_alerts = alerts


@topic_decoder("/status/hms/event_broadcast")
def _on_status_hms_event_broadcast(t: RobotTelemetrySnapshot, d: Any) -> None:
    code = int(d.fault_code) & 0xFFFFFFFF
    inst = int(d.instance_id)
    kind = int(d.kind)
    ts_ms = int(d.timestamp_ms)
    ts = ts_ms / 1000.0 if _epoch_ms(ts_ms) else time.time()  # robot clock may be unsynced / monotonic
    alert = hms_alert(code, inst, module_id=int(d.module_id), source="event", ts=ts)
    action = "cleared" if kind == HMS_KIND_CLEAR else "raised"
    key = (alert.code, alert.instance)
    rest = [a for a in t.hms_alerts if (a.code, a.instance) != key]
    t.hms_alerts = rest if action == "cleared" else [*rest, alert]
    if action == "cleared":
        t.hms_pending_actions.pop(hms_action_key(alert.code, inst), None)
    t.hms_events.append({
        "action": action,
        "code": alert.code,
        "instance": inst,
        "kind": kind,
        "severity": alert.severity,
        "message": alert.message_en,
        "module_id": int(d.module_id),
        "offset": int(d.offset),
        "origin": int(d.origin),
        "recovery_state": int(d.recovery_state),
        "ts": round(time.time(), 3),
    })
    if len(t.hms_events) > HMS_EVENT_HISTORY:
        del t.hms_events[:-HMS_EVENT_HISTORY]


@topic_decoder("/cmd/hms/action_request")
def _on_cmd_hms_action_request(t: RobotTelemetrySnapshot, d: Any) -> None:
    """Health monitor -> app reaction request (e.g. "ask the user to confirm"); kept until answered or the fault clears."""
    code = f"0x{int(d.source_fault_code) & 0xFFFFFFFF:08X}"
    inst = int(d.instance_id)
    t.hms_pending_actions[hms_action_key(code, inst)] = {
        "code": code,
        "instance": inst,
        "command_id": int(d.command_id),
        "action_id": int(d.action_id),
        "operation": int(d.operation),
        "dispatcher_instance_id": int(d.dispatcher_instance_id),
        "delivery_store_id": bytes(d.delivery_store_id).hex(),
        "execution_timeout_ms": int(d.execution_timeout_ms),
        "received_ts": round(time.time(), 3),
    }


def hms_action_key(code: str, instance: int) -> str:
    return f"{code.upper().replace('0X', '0x')}:{int(instance)}"


@topic_decoder("/status/wifi/status")
def _on_status_wifi_status(t: RobotTelemetrySnapshot, d: Any) -> None:
    t.wifi_rssi = int(d.rssi)
    if int(d.connection_status) == 2:
        t.wifi_mode = "STA"
    elif int(d.connection_status) == 1:
        t.wifi_mode = "AP"


@topic_decoder("/status/nm/net_online_notify")
def _on_status_nm_net_online_notify(t: RobotTelemetrySnapshot, d: Any) -> None:
    t.wifi_mode = "STA"
    t.wifi_rssi = int(d.rssi)
    ssid_s = decode_fixed_str(d.ssid, d.ssid_len)
    if ssid_s:
        t.wifi_ssid = ssid_s
    raw_ip = bytes(d.ipv4)
    if len(raw_ip) >= 4 and any(raw_ip[:4]):
        try:
            t.wifi_ipv4 = _socket.inet_ntoa(raw_ip[:4])
        except OSError:  # malformed address: keep the previous value
            pass


@topic_decoder("/status/storage/status")
def _on_status_storage_status(t: RobotTelemetrySnapshot, d: Any) -> None:
    total_mb = int(d.sd_total_space) or int(d.internal_total_space)
    used_mb = int(d.sd_used_space) or int(d.internal_used_space)
    if total_mb > 0:
        t.sd_total_gb = round(total_mb / 1024.0, 1)
        t.sd_free_gb = round(max(0, total_mb - used_mb) / 1024.0, 1)


@topic_decoder("/status/ota/upgrade_progress")
def _on_status_ota_upgrade_progress(t: RobotTelemetrySnapshot, d: Any) -> None:
    t.ota_progress_pct = int(d.progress_percent)
    t.ota_state = _OTA_STAGES.get(int(d.upgrade_stage), f"stage_{int(d.upgrade_stage)}")


def apply_topic_message(t: RobotTelemetrySnapshot, topic: str, d: Any) -> None:
    """Update `t` in place from the `data` sub-message of one received topic."""
    t.topic_ts[topic] = time.monotonic()
    decoder = TOPIC_DECODERS.get(topic)
    if decoder is not None:
        decoder(t, d)
