"""Unit tests for 64-bit `RcJoystick` SIMD bitfields, enums, and `NebiCatalog` Protobuf resolution."""

from __future__ import annotations

import math

from nebi import (
    CATALOG,
    JoystickState,
    PostureCmd,
    SpeedGear,
    StuntDirection,
)
from nebi.types import JOYSTICK_NEUTRAL_WORD


def test_joystick_neutral_word_layout() -> None:
    neutral = JoystickState()
    word = neutral.pack()
    assert word == JOYSTICK_NEUTRAL_WORD
    # Bits 0..10 (ch0=vx/ly) == 1000, 11..21 (ch1=vy/lx) == 1000, 22..32 (ch2=yaw/rx) == 1000
    # Bits 33..38 (ch3=pitch/ry) == 31, 39..44 (roll) == 31, 45..49 (height = app resting raw 16) == 16
    assert (word & 0x7FF) == 1000
    assert ((word >> 11) & 0x7FF) == 1000
    assert ((word >> 22) & 0x7FF) == 1000
    assert ((word >> 33) & 0x3F) == 31
    assert ((word >> 39) & 0x3F) == 31
    assert ((word >> 45) & 0x1F) == 16
    # Verify ly packs into ch0 (bits 0..10) and lx packs into ch1 (bits 11..21)
    w_fwd = JoystickState(ly=1.0).pack()
    assert (w_fwd & 0x7FF) == 2000
    assert ((w_fwd >> 11) & 0x7FF) == 1000
    w_right = JoystickState(lx=1.0).pack()
    assert (w_right & 0x7FF) == 1000
    assert ((w_right >> 11) & 0x7FF) == 2000

    unpacked = JoystickState.unpack(word)
    assert unpacked.lx == 0.0
    assert unpacked.ly == 0.0
    assert unpacked.rx == 0.0
    assert unpacked.ry == 0.0
    assert unpacked.roll == 0.0
    assert round(unpacked.height * 31) == 16
    assert not unpacked.estop


def test_joystick_full_roundtrip_all_fields() -> None:
    orig = JoystickState(
        lx=-0.75,
        ly=0.925,
        rx=-0.35,
        ry=0.5,
        roll=-0.5,
        height=0.65,
        jump_trigger=True,
        stunt=StuntDirection.BACK_FLIP,
        posture=PostureCmd.STAND,
        speed_gear=SpeedGear.SPORT,
        estop=True,
        turn_around=True,
        action_button=True,
        roll_mode=True,
        gamepad_source=True,
    )
    packed = orig.pack()
    assert 0 <= packed <= 0xFFFFFFFFFFFFFFFF
    dec = JoystickState.unpack(packed)
    assert math.isclose(dec.lx, orig.lx, abs_tol=0.002)
    assert math.isclose(dec.ly, orig.ly, abs_tol=0.002)
    assert math.isclose(dec.rx, orig.rx, abs_tol=0.002)
    assert math.isclose(dec.ry, orig.ry, abs_tol=0.04)
    assert math.isclose(dec.roll, orig.roll, abs_tol=0.04)
    assert math.isclose(dec.height, orig.height, abs_tol=0.04)
    assert dec.jump_trigger is True
    assert dec.stunt == StuntDirection.BACK_FLIP
    assert dec.posture == PostureCmd.STAND
    assert dec.speed_gear == SpeedGear.SPORT
    assert dec.estop is True
    assert dec.turn_around is True
    assert dec.action_button is True
    assert dec.roll_mode is True
    assert dec.gamepad_source is True


def test_joystick_hardware_bit_positions_and_ucp_0x1051_mux() -> None:
    import struct

    from nebi.domains.system import SystemController
    from nebi.transport.ucp import UcpEndpointSession, decode_ucp_datagram

    # Verify the exact RcJoystick bit positions
    assert ((JoystickState(estop=True).pack() >> 58) & 1) == 1
    assert ((JoystickState(stand_up_fold=True).pack() >> 59) & 1) == 1
    assert ((JoystickState(action_button=True).pack() >> 60) & 1) == 1
    assert ((JoystickState(turn_around=True).pack() >> 62) & 1) == 1

    # Verify UcpEndpointSession 31-byte 0x1051 packet preserves RcJoystick across ACK/heartbeat probes
    tx_pkts: list[bytes] = []
    sess = UcpEndpointSession(send_datagram=tx_pkts.append)
    target_word = JoystickState(stand_up_fold=True, ly=0.5).pack() & 0x7FFF_FFFF_FFFF_FFFF
    sess.send_joystick_word(target_word)
    sess.send_ack_only()
    assert len(tx_pkts) == 2
    for pkt in tx_pkts:
        assert len(pkt) == 31
        segs = decode_ucp_datagram(pkt)
        assert len(segs) == 1
        assert segs[0].sack is not None
        (raw_u64,) = struct.unpack("<Q", segs[0].payload)
        assert (raw_u64 >> 63) == 0  # liveview enabled -> no_liveview_flag == 0
        assert (raw_u64 & 0x7FFF_FFFF_FFFF_FFFF) == target_word

    # Direct eye LEDs work on every board (LZ110 P2 / 0x3001 verified on hardware) & binary firmware version decoding
    assert SystemController.is_direct_led_supported("LZ110 P2") is True
    assert SystemController.is_direct_led_supported("0x3001") is True
    assert SystemController.is_direct_led_supported("LZ110 P4") is True
    assert SystemController.is_direct_led_supported("LZ110-X5-SIM") is True
    assert SystemController._decode_firmware_version(b"\x1a\x0a\x1c\x04") == "V26.10.28.04"


def test_catalog_loaded_counts_and_protobuf_classes() -> None:
    assert len(CATALOG.topics) >= 180
    assert len(CATALOG.camera_profiles) == 46
    assert len(CATALOG.fault_codes) > 50
    assert len(CATALOG.params) == 9
    assert not hasattr(CATALOG, "motions")

    # Every topic in the table resolves to an importable message class with the table's name
    for spec in CATALOG.topics.values():
        cls = CATALOG.get_proto_class("app" + spec.path)
        assert cls is not None and cls.__name__ == spec.message, spec

    # Key command, status, and data topics resolve to compiled Protobuf classes
    for topic in (
        "/cmd/rc/joystick",
        "/cmd/mission/request",
        "/cmd/mission/request_ack",
        "/cmd/camera/unified_cmd",
        "/cmd/camera/record_req",
        "/cmd/tracking/ctrl_cmd",
        "/cmd/game/control_req",
        "/status/robot/status",
        "/status/pmu/osd_status",
        "/status/camera/status",
        "/data/tracking/target_data",
    ):
        cls = CATALOG.get_proto_class(topic)
        assert cls is not None, f"Failed to resolve {topic}"
        inst = cls()
        assert hasattr(inst, "header")
        assert hasattr(inst, "data")

    # HMS decode from both int and hex string produces canonical 0xXXXXXXXX without double prefix
    hms_from_int = CATALOG.decode_hms(0xF0010012, 0)
    assert hms_from_int["code"] == "0xF0010012"
    assert int(hms_from_int["code"], 16) == 0xF0010012
    assert "Log export" in hms_from_int["message_en"] and hms_from_int["recovery_kind"] == "auto"
    assert CATALOG.decode_hms("0xF00A0001", 768)["message_en"].endswith("(main controller)")
    assert CATALOG.decode_hms(0xF00A0002)["message_en"].startswith("Firmware upgrade in progress")
    assert CATALOG.decode_hms(0xF0030301)["message_en"].startswith("Depth camera timeout")
    assert CATALOG.decode_hms(0xF0030403)["message_en"].startswith("Flip failed")
    assert CATALOG.decode_hms(0xF0039999)["message_en"] == "Locomotion fault 0xF0039999"
    assert CATALOG.decode_hms(0xF0FF0001)["message_en"].startswith("Unknown fault")


def test_official_sdk_protocol_decoders_and_helpers() -> None:
    from types import SimpleNamespace

    from nebi.domains.audio import AudioController
    from nebi.telemetry import apply_topic_message, hms_alert
    from nebi.types import CameraCmdType, RobotTelemetrySnapshot

    assert int(CameraCmdType.RECORD_AUDIO) == 0x0800

    # Audio MAC encoding: 6 raw bytes for Bluetooth MACs, 6 zero bytes for empty
    assert AudioController._encode_mac6("") == b"\x00" * 6
    assert AudioController._encode_mac6("AA:BB:CC:11:22:33") == bytes.fromhex("AABBCC112233")
    assert AudioController._encode_mac6("aabbcc112233") == bytes.fromhex("AABBCC112233")
    assert AudioController._encode_mac6(b"\x01\x02\x03\x04\x05\x06") == b"\x01\x02\x03\x04\x05\x06"

    # HMS split (module_id, 16-bit fault_code) reconstruction: (0xF003 << 16) | 0x0301 == 0xF0030301
    alert = hms_alert(0x0301, 0, module_id=0xF003)
    assert alert.code == "0xF0030301"
    assert "Depth camera timeout" in alert.message_en

    snap = RobotTelemetrySnapshot()

    # RobotStatus.height is in 0.1 cm
    apply_topic_message(
        snap,
        "/status/robot/status",
        SimpleNamespace(
            robot_state=3,
            velocity=150,
            pitch_angle=-12,
            gimbal_angle=25,
            roll_angle=5,
            height=110,
            obstacle_dis=85,
            sport_mode=2,
            trip_odom_dis=12.34,
            session_odom_dis=1.23,
        ),
    )
    assert snap.height_cm == 11.0
    assert snap.velocity_mps == 1.5

    # StorageStatus capacities are in KiB
    apply_topic_message(
        snap,
        "/status/storage/status",
        SimpleNamespace(
            storage_type=1,
            sd_total_space=64 * 1024 * 1024,
            sd_used_space=16 * 1024 * 1024,
            internal_total_space=8 * 1024 * 1024,
            internal_used_space=2 * 1024 * 1024,
        ),
    )
    assert snap.sd_total_gb == 64.0
    assert snap.sd_free_gb == 48.0

    # TrackingStatus.status: 1=tracking, 2=lost, 255=error; target_state: 0=candidate, 1=tracked, 2=lost
    apply_topic_message(snap, "/status/tracking/status", SimpleNamespace(status=2, tracking_id=5))
    assert snap.tracking_active is False
    apply_topic_message(snap, "/status/tracking/status", SimpleNamespace(status=255, tracking_id=255))
    assert snap.tracking_active is False
    apply_topic_message(snap, "/status/tracking/status", SimpleNamespace(status=1, tracking_id=7))
    assert snap.tracking_active is True

    apply_topic_message(
        snap,
        "/data/tracking/target_data",
        SimpleNamespace(
            target_count=3,
            targets=[
                SimpleNamespace(id=1, center_pos_x=0.2, center_pos_y=0.5, width=0.1, height=0.3, target_state=0, reserved_state_flags=0),
                SimpleNamespace(id=7, center_pos_x=0.5, center_pos_y=0.5, width=0.2, height=0.4, target_state=1, reserved_state_flags=0),
                SimpleNamespace(id=9, center_pos_x=0.8, center_pos_y=0.5, width=0.1, height=0.2, target_state=2, reserved_state_flags=0),
            ],
        ),
    )
    by_id = {t.target_id: t for t in snap.targets}
    assert by_id[1].locked is False
    assert by_id[7].locked is True
    assert by_id[9].locked is False
