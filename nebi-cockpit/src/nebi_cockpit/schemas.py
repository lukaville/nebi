"""Pydantic request bodies for the cockpit REST API."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class _Body(BaseModel):
    # json.loads accepts NaN/Infinity; never let them reach the robot (motion, zoom, exposure, ...)
    model_config = ConfigDict(allow_inf_nan=False)


class ConnectionSwitchBody(_Body):
    mode: str = "simulator"  # "simulator" | "auto" | "lan_sta" | "usb_ncm" | "softap" | "ble_bootstrap" | "robot"
    flow: str | None = None
    host: str = ""
    ucp_port: int = 9007
    config_port: int = 8010
    media_port: int = 8084
    ota_port: int = 8088
    bind_interface: str = ""
    ble_address: str = ""
    country_code: str = "US"
    join_wifi: bool = False
    wait_for_button_sec: float = 25.0
    auto_reconnect: bool = True


class DiscoveryScanBody(_Body):
    include_ble: bool = True
    include_lan: bool = True
    deep_lan_scan: bool = False
    timeout: float = 1.8


class BleBootstrapBody(_Body):
    ble_address: str = ""
    country_code: str = "US"
    wait_for_button_sec: float = 25.0
    use_simulator_ble: bool = False


class BleProvisionHomeWifiBody(_Body):
    ssid: str
    password: str = ""
    security: int | None = None
    ble_address: str = ""
    country_code: str = "US"
    interface: str = ""
    switch_os_wifi: bool = True
    wait_for_button_sec: float = 35.0
    timeout_sec: int = 30
    use_simulator_ble: bool = False


class SimPairingButtonBody(_Body):
    reset: bool = False
    require_button: bool = True


class SimLinkDropBody(_Body):
    duration_sec: float = 4.2


class JumpBody(_Body):
    height: float = 0.8


class FlipBody(_Body):
    direction: int = 2  # 1=front, 2=back, 3=left, 4=right


class SpeedGearBody(_Body):
    gear: int = 2  # 1=soft, 2=standard, 3=sport


class MissionBody(_Body):
    mission_id: int
    sub_mode: int = 0


class ZoomBody(_Body):
    zoom_x: float = 1.0


class EisBody(_Body):
    enabled: bool = True
    level: int = 1


class ToggleBody(_Body):
    enabled: bool = True


class ProfileBody(_Body):
    profile_id: int


class ProExposureBody(_Body):
    pro_mode: bool = True
    shutter_ns: int = 0
    iso: int = 0
    wb_kelvin: int = 5500
    ev_compensation: float = 0.0


class StreamConfigBody(_Body):
    streaming: bool | None = None
    bitrate_kbps: int | None = None
    fps: int | None = None
    request_idr: bool = False


class TrajectoryPlayBody(_Body):
    trajectory_id: str


class LockTargetBody(_Body):
    target_id: int


class LockPointBody(_Body):
    x: float
    y: float


class FollowModeBody(_Body):
    sub_mode: int = 1
    distance_m: float | None = None
    orbit_speed: float | None = None


class IntValueBody(_Body):
    value: int


class GameStartBody(_Body):
    game_type: int


class GameActionBody(_Body):
    action: str  # "pause" | "resume" | "stop" | "restart"


class GameInputBody(_Body):
    pitch_deg: float = 0.0
    roll_deg: float = 0.0
    yaw_deg: float = 0.0
    jump: bool = False
    action_pressed: bool = False


class PlayFileBody(_Body):
    audio_path: str
    play_action: int = 1


class MicSelectBody(_Body):
    kind: int = 0
    mac: str = ""


class ParamsUpdateBody(_Body):
    values: dict[str, Any]


class LedExpressionBody(_Body):
    effect: str = "happy.csv"
    play_count: int = 1


class LedRgbBody(_Body):
    r: int = 0
    g: int = 210
    b: int = 255


class UwbVibrateBody(_Body):
    slot: int = 0
    level: int = 2


class HmsCodeBody(_Body):
    code: str = "0x01010001"
    instance: int = 0


class WifiConnectBody(_Body):
    ssid: str
    password: str = ""
    security: int = 2


class WifiProvisionBody(_Body):
    ssid: str
    password: str = ""
    security: int = 2
    switch_connection: bool = True
    timeout_sec: int = 25

