"""Core enums, the RcJoystick bitfield codec, and telemetry data models for nebi.

Sections: connection enums - robot/mission enums - joystick word layout + `JoystickState` - telemetry dataclasses.
"""

from __future__ import annotations

import math
import time
from dataclasses import asdict, dataclass, field
from enum import Enum, IntEnum, IntFlag
from typing import Any, ClassVar

# Fixed robot addresses of the direct-link flows.
SOFTAP_HOST = "192.168.7.1"  # robot SoftAP (`IoTWifi`), also the BLE-bootstrap target
SOFTAP_SUBNET = "192.168.7."
USB_NCM_HOST = "192.168.123.1"  # USB-C Ethernet-over-USB
USB_NCM_SUBNET = "192.168.123."


class ConnectionFlow(str, Enum):
    """Supported non-cloud control & bootstrap flows."""

    SIMULATOR = "simulator"
    AUTO = "auto"
    LAN_STA = "lan_sta"
    USB_NCM = "usb_ncm"
    SOFTAP = "softap"
    BLE_BOOTSTRAP = "ble_bootstrap"


class ConnectionStage(str, Enum):
    """Fine-grained connection lifecycle & recovery stages."""

    DISCONNECTED = "disconnected"
    DISCOVERING = "discovering"
    BLE_CONNECTING = "ble_connecting"
    BLE_AWAITING_BUTTON_AUTH = "ble_awaiting_button_auth"
    BLE_FETCHING_WIFI_INFO = "ble_fetching_wifi_info"
    JOINING_SOFTAP = "joining_softap"
    PROBING_HTTP = "probing_http"
    UCP_HANDSHAKING = "ucp_handshaking"
    ZENOH_TUNNELING = "zenoh_tunneling"
    SECURITY_HANDSHAKING = "security_handshaking"
    STREAM_STARTING = "stream_starting"
    CONNECTED = "connected"
    DEGRADED = "degraded"
    RECONNECTING = "reconnecting"
    WIFI_PROVISIONING = "wifi_provisioning"
    ERROR = "error"


class WifiProvisionFailReason(IntEnum):
    """Failure codes reported by the robot's network service in `/cmd/nm/wifi_provision_result_rsp`."""

    OK = 0
    WRONG_KEY = 1
    NETWORK_NOT_FOUND = 2
    ASSOC_TIMEOUT = 3
    DHCP_TIMEOUT = 4
    WATCHDOG_TIMEOUT = 5


class MissionId(IntEnum):
    """Scheduler mission IDs (`/cmd/mission/request`, reported in robot status)."""

    IDLE = 0
    CAMERA = 1
    GAME = 2
    PET = 3
    SELF_CALIBRATION = 4
    QA = 10
    REAL_RL = 11


class CameraSubMode(IntEnum):
    """Sub-modes for MissionId.CAMERA."""

    MANUAL = 0
    FOLLOW_TRAIL = 1
    FOLLOW_PARALLEL = 2
    FOLLOW_LEAD = 3
    FOLLOW_ORBIT = 4
    SPOTLIGHT = 5
    EGO_INTERACTION = 6


class PetSubMode(IntEnum):
    """Sub-modes for MissionId.PET."""

    COMPANION = 0
    DOCK = 1
    UNDOCK = 2


class GameType(IntEnum):
    """Interactive robot games in MissionId.GAME."""

    NONE = 0
    MOTION_SENSE = 1
    TUG_OF_WAR = 2
    CAMERA_BATTLE = 3
    BALANCE = 4
    TREASURE_HUNT = 5


class GameAction(IntEnum):
    """Control actions for /cmd/game/control_req."""

    START = 1
    PAUSE = 2
    RESUME = 3
    STOP = 4
    RESTART = 5


class CameraCmdType(IntFlag):
    """`CameraUnifiedCmd.cmd_type` bitmask (several sub-commands can be combined in one message)."""

    CAPTURE = 0x0001
    RECORD_INTENT = 0x0002
    ZOOM = 0x0004
    EIS = 0x0008
    RECORD_PROFILE = 0x0010
    VT_CONFIG = 0x0020
    RAW_CAPTURE = 0x0040
    PRO_EXPOSURE = 0x0080
    HIGHLIGHT_TAG = 0x0100
    STORAGE_TARGET = 0x0200
    HORIZON_LOCK = 0x0400


class StuntDirection(IntEnum):
    """3-bit stunt / flip selector in RcJoystickData.joystick bits 51..53."""

    NONE = 0
    FRONT_FLIP = 1
    BACK_FLIP = 2
    LEFT_FLIP = 3
    RIGHT_FLIP = 4


class PostureCmd(IntEnum):
    """2-bit posture command in RcJoystickData.joystick bits 54..55."""

    HOLD = 0
    STAND = 1
    FOLD = 2


class SpeedGear(IntEnum):
    """2-bit speed/sensitivity preset in RcJoystickData.joystick bits 56..57."""

    DEFAULT = 0
    SOFT = 1
    STANDARD = 2
    SPORT = 3


class RobotPostureState(IntEnum):
    """Robot posture state reported in RobotStatusData.robot_state.

    On real LZ110 firmware (V26.10.28.04, verified live 2026-11-16) `robot_state` mirrors the
    chassis state machine's `actual_state` (`/status/control/status`), whose enum order is
    Disabled, MotorReset, JointPosCtrl, NetControl, ...:
      0 = Disabled (folded / legs down), 1 = MotorReset (homing at stand-up start),
      2 = JointPosCtrl (scripted pose during stand-up), 3 = NetControl (standing, balancing).
    Values >= 4 on the real robot (Recover, Fold sub-states, ...) are not mapped yet.
    The members >= 100 below exist only for the simulator's richer posture model.
    """

    FOLDED = 0
    MOTOR_RESET = 1
    STANDING_UP = 1  # alias: MotorReset is the first phase of standing up
    JOINT_POS_CTRL = 2
    BALANCING = 3
    # simulator-only states (never reported by real firmware with these values)
    FOLDING = 100
    JUMPING = 101
    FLIPPING = 102
    DOCKING = 103
    CHARGING = 104
    FAULT = 105


def _clamp(val: float, lo: float, hi: float) -> float:
    val = float(val)
    if not math.isfinite(val):  # NaN/inf would crash pack() (or worse): treat as neutral
        val = 0.0 if lo <= 0.0 <= hi else lo
    return lo if val < lo else (hi if val > hi else val)


# --- RcJoystick 64-bit word layout (single source of truth; transport/ucp.py and the simulator use these) -------
# Sticks are offset-binary: 11-bit channels centre at 1000 (+-1000), 6-bit at 31 (+-31).
JOY_LY_SHIFT = 0  # ch0: forward/backward vx, 11 bits
JOY_LX_SHIFT = 11  # ch1: lateral/strafe vy, 11 bits
JOY_RX_SHIFT = 22  # ch2: yaw rate, 11 bits
JOY_RY_SHIFT = 33  # ch3: gimbal pitch, 6 bits
JOY_ROLL_SHIFT = 39  # body roll/tilt, 6 bits
JOY_HEIGHT_SHIFT = 45  # standing body height, 5 bits (0 = crouch, 31 = tall)
JOY_JUMP_BIT = 50
JOY_STUNT_SHIFT = 51  # 3 bits
JOY_POSTURE_SHIFT = 54  # 2 bits
JOY_GEAR_SHIFT = 56  # 2 bits
JOY_ESTOP_BIT = 58  # edge-toggled
JOY_STAND_FOLD_BIT = 59  # LATCHED: 0 -> 1 = fold, 1 -> 0 = stand (see MotionController)
JOY_ACTION_BIT = 60
JOY_ROLL_MODE_BIT = 61
JOY_TURN_AROUND_BIT = 62
JOY_B63_BIT = 63  # gamepad_source; on UCP :9007 the robot's app bridge reads it as no_liveview_flag

JOY_STICK11_MASK = 0x7FF
JOY_STICK11_CENTER = 1000
JOY_STICK11_SPAN = 1000
JOY_STICK6_MASK = 0x3F
JOY_STICK6_CENTER = 31
JOY_HEIGHT_MASK = 0x1F
JOY_HEIGHT_MAX = 31
JOY_GEAR_MASK = 0x3

JOYSTICK_WIRE_MASK = 0x7FFF_FFFF_FFFF_FFFF  # bit 63 cleared: what we put on the wire (liveview stays enabled)
JOYSTICK_STICKS_MASK = (1 << JOY_ROLL_SHIFT) - 1  # bits 0..38: ly, lx, rx (11 bits each) + ry (6 bits)
JOYSTICK_STICKS_NEUTRAL = (
    (JOY_STICK11_CENTER << JOY_LY_SHIFT)
    | (JOY_STICK11_CENTER << JOY_LX_SHIFT)
    | (JOY_STICK11_CENTER << JOY_RX_SHIFT)
    | (JOY_STICK6_CENTER << JOY_RY_SHIFT)
)
# One-shot trigger bits (jump, flip, posture cmd, turn_around): cleared by the transport's dead-man hold word.
JOYSTICK_TRIGGER_MASK = (
    (1 << JOY_JUMP_BIT) | (0x7 << JOY_STUNT_SHIFT) | (0x3 << JOY_POSTURE_SHIFT) | (1 << JOY_TURN_AROUND_BIT)
)


# Body height the official app sends at rest (raw 16/31 ~ 11 cm, captured 2026-11-17). There is no "unset" code:
# every RcJoystick word carries an absolute height, so matching the app avoids a rise/sink when switching clients.
DEFAULT_BODY_HEIGHT: float = 16 / 31


class SpeedGearWire:
    """`speed_gear` as the robot reads it on the wire (0..2 are accepted; 3 is rejected).

    `SpeedGear` enum values (DEFAULT=0, SOFT=1, STANDARD=2, SPORT=3) are the SDK's; `pack()` writes them raw,
    `pack_wire()` translates them with `TO_WIRE`.
    """

    SOFT = 0
    STANDARD = 1
    SPORT = 2
    TO_WIRE: ClassVar[dict[SpeedGear, int]] = {
        SpeedGear.DEFAULT: STANDARD,
        SpeedGear.SOFT: SOFT,
        SpeedGear.STANDARD: STANDARD,
        SpeedGear.SPORT: SPORT,
    }
    FROM_WIRE: ClassVar[dict[int, SpeedGear]] = {SOFT: SpeedGear.SOFT, STANDARD: SpeedGear.STANDARD, SPORT: SpeedGear.SPORT}


def _stick11(v: float) -> int:
    return round(_clamp(v, -1.0, 1.0) * JOY_STICK11_SPAN) + JOY_STICK11_CENTER


def _stick6(v: float) -> int:
    return round(_clamp(v, -1.0, 1.0) * JOY_STICK6_CENTER) + JOY_STICK6_CENTER


def _unstick11(word: int, shift: int) -> float:
    return _clamp((((word >> shift) & JOY_STICK11_MASK) - JOY_STICK11_CENTER) / JOY_STICK11_SPAN, -1.0, 1.0)


def _unstick6(word: int, shift: int) -> float:
    return _clamp((((word >> shift) & JOY_STICK6_MASK) - JOY_STICK6_CENTER) / JOY_STICK6_CENTER, -1.0, 1.0)


def _bit(word: int, bit: int) -> bool:
    return bool((word >> bit) & 1)


@dataclass(slots=True)
class JoystickState:
    """Normalized 6-DoF + button state packed into RcJoystickData.joystick (uint64).

    Bit layout (see the `JOY_*` constants above):
      - bits 0..10:  ch0 = ly (forward/backward vx in [-1, 1], neutral=1000)
      - bits 11..21: ch1 = lx (lateral/strafe vy in [-1, 1], neutral=1000)
      - bits 22..32: ch2 = rx (yaw rate in [-1, 1], neutral=1000)
      - bits 33..38: ch3 = ry (gimbal pitch in [-1, 1], neutral=31)
      - bits 39..44: roll (body roll/tilt in [-1, 1], neutral=31)
      - bits 45..49: height (standing body height in [0.0, 1.0], 0=crouch, 31=1.0=tall; default raw 16 like the app)
      - bit 50:      jump_trigger
      - bits 51..53: stunt / flip (0..7; 1=front, 2=back, 3=left, 4=right)
      - bits 54..55: posture (2-bit auxiliary mode)
      - bits 56..57: speed_gear (`pack()`: SpeedGear value; wire via `pack_wire()`: 0=Soft, 1=Standard, 2=Sport)
      - bit 58:      estop (edge-detected by the robot)
      - bit 59:      stand_up_fold (LATCHED switch: 0->1 edge = Fold, 1->0 edge = Stand Up)
      - bit 60:      action_button (hold timer reset)
      - bit 61:      roll_mode (client-side mode flag)
      - bit 62:      turn_around (180-degree spin)
      - bit 63:      no_liveview / gamepad_source (on UCP :9007, bit 63 is the app bridge's no_liveview_flag)
    """

    lx: float = 0.0  # [-1.0, 1.0] lateral / strafe velocity ch1 (bits 11..21)
    ly: float = 0.0  # [-1.0, 1.0] forward / backward velocity ch0 (bits 0..10)
    rx: float = 0.0  # [-1.0, 1.0] yaw rate ch2 (bits 22..32)
    ry: float = 0.0  # [-1.0, 1.0] gimbal pitch ch3 (bits 33..38)
    roll: float = 0.0  # [-1.0, 1.0] body roll / tilt (bits 39..44, neutral=31)
    height: float = DEFAULT_BODY_HEIGHT  # [0.0, 1.0] standing body height (bits 45..49); default raw 16 = app's
    jump_trigger: bool = False  # bit 50
    stunt: StuntDirection = StuntDirection.NONE  # bits 51..53
    posture: PostureCmd = PostureCmd.HOLD  # bits 54..55
    speed_gear: SpeedGear = SpeedGear.DEFAULT  # bits 56..57
    estop: bool = False  # bit 58 (edge-toggled on hardware)
    stand_up_fold: bool = False  # bit 59 (latched stand/fold switch)
    action_button: bool = False  # bit 60
    roll_mode: bool = False  # bit 61
    turn_around: bool = False  # bit 62 (180-degree spin on hardware)
    gamepad_source: bool = False  # bit 63 (masked off on UCP :9007 where bit 63 = no_liveview_flag)
    no_liveview: bool = False  # bit 63 when sent over UCP 0x1051

    def pack(self) -> int:
        """Pack into the 64-bit unsigned integer expected by RcJoystickData.joystick (SpeedGear value raw)."""
        height_raw = round(_clamp(self.height, 0.0, 1.0) * JOY_HEIGHT_MAX)
        b63 = 1 if (self.no_liveview or self.gamepad_source) else 0

        word = (
            ((_stick11(self.ly) & JOY_STICK11_MASK) << JOY_LY_SHIFT)
            | ((_stick11(self.lx) & JOY_STICK11_MASK) << JOY_LX_SHIFT)
            | ((_stick11(self.rx) & JOY_STICK11_MASK) << JOY_RX_SHIFT)
            | ((_stick6(self.ry) & JOY_STICK6_MASK) << JOY_RY_SHIFT)
            | ((_stick6(self.roll) & JOY_STICK6_MASK) << JOY_ROLL_SHIFT)
            | ((height_raw & JOY_HEIGHT_MASK) << JOY_HEIGHT_SHIFT)
            | ((1 if self.jump_trigger else 0) << JOY_JUMP_BIT)
            | ((int(self.stunt) & 0x7) << JOY_STUNT_SHIFT)
            | ((int(self.posture) & 0x3) << JOY_POSTURE_SHIFT)
            | ((int(self.speed_gear) & JOY_GEAR_MASK) << JOY_GEAR_SHIFT)
            | ((1 if self.estop else 0) << JOY_ESTOP_BIT)
            | ((1 if self.stand_up_fold else 0) << JOY_STAND_FOLD_BIT)
            | ((1 if self.action_button else 0) << JOY_ACTION_BIT)
            | ((1 if self.roll_mode else 0) << JOY_ROLL_MODE_BIT)
            | ((1 if self.turn_around else 0) << JOY_TURN_AROUND_BIT)
            | (b63 << JOY_B63_BIT)
        )
        return word & 0xFFFFFFFFFFFFFFFF

    def pack_wire(self) -> int:
        """Pack for the wire: firmware speed gear (`SpeedGearWire`) and bit 63 (no_liveview) cleared."""
        wire_gear = SpeedGearWire.TO_WIRE.get(self.speed_gear, SpeedGearWire.STANDARD) & JOY_GEAR_MASK
        word = (self.pack() & ~(JOY_GEAR_MASK << JOY_GEAR_SHIFT)) | (wire_gear << JOY_GEAR_SHIFT)
        return word & JOYSTICK_WIRE_MASK

    @classmethod
    def unpack_wire(cls, word: int) -> JoystickState:
        """Decode a word captured ON THE WIRE (e.g. the official app's heartbeat). The wire speed gear is
        0 = Soft, 1 = Standard, 2 = Sport, unlike `pack()`/`unpack()` which use the SpeedGear enum values."""
        st = cls.unpack(word)
        st.speed_gear = SpeedGearWire.FROM_WIRE.get((word >> JOY_GEAR_SHIFT) & JOY_GEAR_MASK, SpeedGear.STANDARD)
        return st

    @classmethod
    def unpack(cls, word: int) -> JoystickState:
        """Unpack a 64-bit RcJoystickData.joystick integer the way the robot interprets it."""
        posture_raw = (word >> JOY_POSTURE_SHIFT) & 0x3
        posture = PostureCmd(posture_raw) if posture_raw in (0, 1, 2) else PostureCmd.HOLD
        stunt_raw = (word >> JOY_STUNT_SHIFT) & 0x7
        stunt = StuntDirection(stunt_raw) if stunt_raw <= 4 else StuntDirection.NONE
        b63 = _bit(word, JOY_B63_BIT)
        return cls(
            lx=_unstick11(word, JOY_LX_SHIFT),
            ly=_unstick11(word, JOY_LY_SHIFT),
            rx=_unstick11(word, JOY_RX_SHIFT),
            ry=_unstick6(word, JOY_RY_SHIFT),
            roll=_unstick6(word, JOY_ROLL_SHIFT),
            height=_clamp(((word >> JOY_HEIGHT_SHIFT) & JOY_HEIGHT_MASK) / JOY_HEIGHT_MAX, 0.0, 1.0),
            jump_trigger=_bit(word, JOY_JUMP_BIT),
            stunt=stunt,
            posture=posture,
            speed_gear=SpeedGear((word >> JOY_GEAR_SHIFT) & JOY_GEAR_MASK),
            estop=_bit(word, JOY_ESTOP_BIT),
            stand_up_fold=_bit(word, JOY_STAND_FOLD_BIT),
            action_button=_bit(word, JOY_ACTION_BIT),
            roll_mode=_bit(word, JOY_ROLL_MODE_BIT),
            turn_around=_bit(word, JOY_TURN_AROUND_BIT),
            gamepad_source=b63,
            no_liveview=b63,
        )


JOYSTICK_NEUTRAL_WORD: int = JoystickState().pack()
# Neutral word as sent on the wire (Standard wire gear, bit 63 clear) = 0x0102_0FBE_FA1F_43E8.
JOYSTICK_WIRE_NEUTRAL_WORD: int = JoystickState().pack_wire()


@dataclass
class DetectedTarget:
    """Single AI vision tracking target (person or pet) in normalized [0..1] image coords."""

    target_id: int
    label: str  # "person" | "pet"
    confidence: float | None  # None: the firmware does not report a detection confidence
    cx: float
    cy: float
    width: float
    height: float
    distance_m: float = 0.0
    distance_estimated: bool = True  # heuristic from the box height, not a measurement
    locked: bool = False


@dataclass
class UwbRemoteState:
    """Status of one paired UWB remote/tag (up to 4)."""

    slot: int
    mac: str
    online: bool
    battery_pct: int
    distance_m: float
    azimuth_deg: float
    distance_estimated: bool = True  # derived from RSSI, not a UWB ranging measurement


@dataclass
class HmsAlert:
    """Decoded Health Management System fault entry."""

    code: str
    instance: int
    severity: str
    message_en: str
    recovery_kind: str  # "info" | "auto" | "confirm" | "user" (see catalog/faults.py)
    timestamp: float = field(default_factory=time.time)  # wall time the fault was (last) raised
    module_id: int = 0
    recovery_hint: str = ""  # plain-English reading of `recovery_kind`
    source: str = "snapshot"  # "event" (/status/hms/event_broadcast) or "snapshot" (/status/hms/active_snapshot)


@dataclass
class LinkDiagnostics:
    """Live UCP + Zenoh + Split-Socket transport link metrics and connection stage."""

    connected: bool = False
    mode: str = "disconnected"  # "simulator" | "lan_sta" | "usb_ncm" | "softap" | "ble_bootstrap" | "disconnected"
    flow: str = "disconnected"
    stage: str = ConnectionStage.DISCONNECTED.value
    stage_detail: str = ""
    host: str = ""
    ucp_port: int = 9007
    bind_interface: str = ""
    local_ip: str = ""
    ble_address: str = ""
    degraded: bool = False
    reconnect_attempt: int = 0
    rx_silence_ms: int = 0
    rtt_ms: float = 0.0
    video_fps: float = 0.0
    video_kbps: float = 0.0
    video_frames_rx: int = 0
    audio_frames_rx: int = 0
    zenoh_ready: bool = False
    md_ts_active: bool = False
    zenoh_rx_msgs: int = 0
    zenoh_tx_msgs: int = 0
    last_error: dict[str, Any] | None = None
    stage_history: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class RobotTelemetrySnapshot:
    """Unified reactive robot telemetry state updated from device/** Zenoh topics."""

    # Posture & locomotion
    robot_state: int = -1         # -1 = no posture report yet (never assume standing)
    robot_state_ts: float = 0.0   # time.monotonic() of the last real /status/robot/status (0 = never received)
    # time.monotonic() each status/data topic was last received: lets callers tell live values from defaults
    topic_ts: dict[str, float] = field(default_factory=dict)
    posture_name: str = "UNKNOWN"
    velocity_mps: float = 0.0
    yaw_deg: float = 0.0
    pitch_deg: float = 0.0
    roll_deg: float = 0.0
    gimbal_pitch_deg: float = 0.0
    height_cm: int = 11
    obstacle_cm: int = 250
    sport_mode: int = 2
    trip_odom_m: float = 0.0
    session_odom_m: float = 0.0
    pos_x_m: float = 0.0
    pos_y_m: float = 0.0

    # Mission & motion
    mission_id: int = int(MissionId.CAMERA)
    mission_name: str = "CAMERA"
    sub_mode: int = int(CameraSubMode.MANUAL)
    sub_mode_name: str = "MANUAL"
    is_locked: bool = False

    # Battery / PMU
    battery_soc: int = 88
    charging: bool = False
    docked: bool = False
    voltage_mv: int = 16200

    # Camera & recording
    recording: bool = False
    recording_sec: float = 0.0
    record_profile_id: int = 2
    zoom_x: float = 1.0
    eis_enabled: bool = True
    eis_level: int = 1
    horizon_lock: bool = False
    pro_mode: bool = False
    shutter_idx: int = 0
    iso_idx: int = 0
    ev_idx: int = 6
    wb_idx: int = 0
    pre_record_enabled: bool = False
    vt_streaming: bool = True
    vt_bitrate_kbps: int = 4000
    vt_fps: int = 30
    sd_free_gb: float = 48.5
    sd_total_gb: float = 64.0

    # Tracking & PNL
    tracking_active: bool = False
    locked_target_id: int = 0
    tracking_model: str = "person_pet_v2"
    pnl_mode: int = 0  # real robot boots in LOCALIZATION (0)
    targets: list[DetectedTarget] = field(default_factory=list)

    # Games
    active_game: int = 0
    game_state: str = "idle"
    game_score: int = 0

    # Audio, Voice, LED & Wi-Fi
    speaker_volume: int = 75
    intercom_muted: bool = True
    voice_wakeup: bool = True
    voice_lang: str = "en-US"
    led_expression: int = 1
    wifi_mode: str = "AP"
    wifi_ssid: str = ""           # only a real SSID: it is part of the MD-TS robot-key lookup
    wifi_ipv4: str = SOFTAP_HOST
    wifi_rssi: int = -42
    wifi_provision_state: str = "idle"
    serial_number: str = ""
    hardware_version: str = ""
    firmware_version: str = "v1.4.2-x5"
    led_direct_supported: bool = True
    ota_progress_pct: int = 0
    ota_state: str = "idle"

    # Remotes, HMS, Link
    uwb_remotes: list[UwbRemoteState] = field(default_factory=list)
    hms_alerts: list[HmsAlert] = field(default_factory=list)  # currently ACTIVE faults (raise adds, clear removes)
    hms_events: list[dict[str, Any]] = field(default_factory=list)  # recent raw raise/clear events, newest last
    # HMS actions the robot asked the app to perform/confirm (/cmd/hms/action_request), keyed "0xCODE:instance";
    # answered with /cmd/hms/action_response by `system.clear_hms_fault` (acknowledge).
    hms_pending_actions: dict[str, dict[str, Any]] = field(default_factory=dict)
    link: LinkDiagnostics = field(default_factory=LinkDiagnostics)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
