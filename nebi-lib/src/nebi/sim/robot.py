"""`MockNebiRobot`: in-process simulated Nebi (`lz110`) speaking the real robot-side protocol stack over localhost.

  - UCP v1 UDP server (`sim.ucp.UcpServer`, default `:19007`) streaming fragmented Annex-B H.265 video on Ch 1
  - Zenoh-over-UCP tunnel responder (`sim.zenoh`) + `eclipse-zenoh` peer session (`device/**` <-> `app/**`)
  - `app/**` command handlers (`sim.topics`) and periodic telemetry (`sim.telemetry`)
  - Local HTTP daemon serving Config (`:8010`), Media Album (`:8084`) and OTA (`:8088`) routes (`sim.http`)
  - 25 Hz kinematics (`sim.physics`) + Pillow FPV renderer (`sim.render`) + PyAV `libx265` HEVC encoder
  - BLE GATT peripheral (`sim.ble`) and MD-TS authority (`sim.md_ts`) shared by BLE and Zenoh
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import socket
import time
from fractions import Fraction
from typing import Any

import av
import uvicorn
from google.protobuf.message import Message
from PIL import Image

from nebi.catalog.registry import CATALOG
from nebi.sim.ble import MockBlePeripheral
from nebi.sim.http import build_http_app
from nebi.sim.intercom import SimIntercom
from nebi.sim.md_ts import MdTsServerAuthority
from nebi.sim.media import SimMediaAsset, encode_jpeg, encode_mp4
from nebi.sim.physics import step_physics
from nebi.sim.render import render_fpv_frame
from nebi.sim.telemetry import (
    publish_fast_telemetry,
    publish_hms_action_request,
    publish_hms_event,
    publish_hms_snapshot,
    publish_slow_telemetry,
)
from nebi.sim.topics import dispatch_app_topic
from nebi.sim.ucp import UcpServer
from nebi.sim.zenoh import ZenohUcpServerTunnel
from nebi.transport.ucp import UcpEndpointSession
from nebi.transport.zenoh_bus import ZenohTopicBus
from nebi.types import (
    JOY_GEAR_MASK,
    JOY_GEAR_SHIFT,
    SOFTAP_HOST,
    CameraSubMode,
    JoystickState,
    MissionId,
    PostureCmd,
    RobotPostureState,
    SpeedGear,
    StuntDirection,
)

logger = logging.getLogger(__name__)

__all__ = ["MockNebiRobot", "SimMediaAsset"]



class MockNebiRobot:
    """In-process simulated Nebi (`lz110`) robot speaking real UCP + Zenoh Protobuf + H.265 + HTTP."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        ucp_port: int = 19007,
        http_port: int = 18080,
        *,
        video_width: int = 640,
        video_height: int = 360,
        video_fps: int = 24,
        md_ts_enabled: bool = True,
        md_ts_zenoh_required: bool = False,
    ) -> None:
        self.host = host
        self.ucp_port = ucp_port
        self.http_port = http_port
        self.video_width = video_width
        self.video_height = video_height
        self.target_fps = video_fps
        self.md_ts_enabled = md_ts_enabled
        self.md_ts_zenoh_required = md_ts_zenoh_required
        self.md_ts_authority = MdTsServerAuthority(
            require_button_auth=False,
            auto_authorize_unknown=True,
        )

        # Simulated physical & subsystem state
        self.pos_x: float = 0.0
        self.pos_y: float = 0.0
        self.yaw_rad: float = 0.0
        self.vx_mps: float = 0.0
        self.vy_mps: float = 0.0
        self.yaw_rate_rps: float = 0.0
        self.body_height_cm: float = 11.0
        self.pitch_deg: float = 0.0
        self.roll_deg: float = 0.0
        self.gimbal_pitch_deg: float = 0.0
        self.jump_z_m: float = 0.0
        self.jump_vz: float = 0.0
        self.stunt_timer: float = 0.0
        self.stunt_dir: int = 0
        self.turn_around_rad_rem: float = 0.0
        self.trip_odom_m: float = 12.4
        self.session_odom_m: float = 0.0

        self.posture_state: RobotPostureState = RobotPostureState.BALANCING
        self.mission_id: int = int(MissionId.CAMERA)
        self.sub_mode: int = int(CameraSubMode.MANUAL)
        self.speed_gear: int = int(SpeedGear.STANDARD)

        # Battery & dock
        self.dock_x: float = 3.8
        self.dock_y: float = -1.6
        self.battery_soc: float = 91.0
        self.charging: bool = False
        self.docked: bool = False

        # Camera & VT
        self.vt_streaming: bool = True
        self.vt_bitrate_kbps: int = 3500
        self.force_idr: bool = True
        self.zoom_x: float = 1.0
        self.eis_enabled: bool = True
        self.eis_level: int = 1
        self.horizon_lock: bool = False
        self.record_profile_id: int = 2
        self.pro_mode: bool = False
        self.ev_comp: float = 0.0
        self.wb_kelvin: int = 5500
        self.pre_record_enabled: bool = False
        self.recording: bool = False
        self.rec_started_at: float = 0.0
        self.rec_frames: list[Image.Image] = []
        self.rec_highlights: list[float] = []

        # Tracking & world actors
        self.tracking_enabled: bool = False
        self.locked_target_id: int = 0
        self.target_type_filter: int = 0  # 0=all, 1=person, 2=pet
        self.follow_distance_m: float = 2.2
        self.orbit_speed: float = 0.55
        self.pnl_mode: int = 0  # the real positioning service boots in LOCALIZATION
        self.latest_boxes: list[dict[str, Any]] = []

        # Games, Audio, LED, UWB, HMS, Wi-Fi, Config, OTA
        self.active_game: int = 0
        self.game_state: int = 0  # 0=idle, 1=running, 2=paused
        self.game_score: float = 0.0
        self.led_effect: str = "happy.csv"
        self.led_rgb: tuple[int, int, int] = (0, 210, 255)
        self.led_frames: int = 0  # /cmd/led/ctrl_oneway frames received
        self.led_last_frame_t: float = 0.0  # monotonic time of the last frame (the real robot takes the eyes back ~1 s after)
        self.voice_wakeup: bool = True
        self.intercom = SimIntercom(lambda: self.ucp_server.session)
        self.active_mic_kind: int = 0
        self.active_mic_mac: str = ""
        self.wifi_ssid: str = "Nebi-SIM001"
        self.wifi_mode: int = 0  # 0=AP, 1=STA
        self.wifi_ipv4: str = SOFTAP_HOST
        self.pending_wifi_ssid: str = ""
        self.pending_wifi_psk: str = ""
        self.prov_uuid: bytes = b""
        self.prov_state: int = 0  # 0=NONE, 1=RUNNING, 2=SUCCEEDED, 3=FAILED
        self.prov_fail_reason: int = 0
        self.ble_peripheral = MockBlePeripheral(
            address="SIM-BLE-LZ110-01",
            name="Nebi-SIM001",
            ssid="Nebi-SIM001",
            password="nebi2026sim",
            ap_ip=host,
            require_button_auth=False,
            authorized=True,
            md_ts_enabled=self.md_ts_enabled,
            md_ts_authority=self.md_ts_authority,
        )
        self.hms_active_codes: list[tuple[int, int]] = []  # (fault_code_int, instance)
        self.hms_event_offset: int = 0
        self.hms_pending_commands: dict[int, tuple[int, int]] = {}  # action_request command_id -> fault
        self.ota_packages: list[dict[str, Any]] = []
        self.ota_stage: int = 0
        self.ota_progress: int = 0

        self.config_store: dict[str, Any] = {p.key: p.default for p in CATALOG.params}

        self.joystick = JoystickState()
        self.last_joystick_ts: float = 0.0
        self._joy_edge_initialized: bool = False
        self._prev_stand_fold_bit: bool = False
        self._stand_fold_press_ts: float = 0.0
        self._prev_estop_bit: bool = False
        self._estop_rebaseline: bool = False  # set on a new UCP peer: re-latch the estop baseline on its first frame
        self.media_files: dict[str, SimMediaAsset] = {}
        self.media_auth_failures = 0  # /media/* requests rejected by the bearer-token check
        self.media_counter: int = 0
        self.last_rgb_frame: Image.Image | None = None

        # Transport servers
        self.bus = ZenohTopicBus(
            role="device",
            md_ts_authority=self.md_ts_authority if self.md_ts_enabled else None,
            md_ts_required=self.md_ts_zenoh_required,
        )
        self.bus.subscribe_all(self._on_app_topic)
        self._zenoh_port: int = 0
        self.ucp_server = UcpServer(
            host=host,
            port=ucp_port,
            on_audio_frame=self._on_intercom_audio,
            on_zenoh_frame=self._on_ucp_zenoh_frame,
            on_joystick_frame=self._on_ucp_joystick_frame,
            on_peer_connected=self._on_ucp_peer_connected,
        )
        self.zenoh_tunnel: ZenohUcpServerTunnel | None = None
        self._http_server: uvicorn.Server | None = None
        self._http_task: asyncio.Task[None] | None = None
        self._sim_task: asyncio.Task[None] | None = None
        self._running = False

        self._hevc_enc: Any = None
        self._pts: int = 0

    @property
    def running(self) -> bool:
        return self._running

    # --- Intercom state (read by tests / UI) ---

    @property
    def intercom_active(self) -> bool:
        return self.intercom.active

    @property
    def intercom_rx_frames(self) -> int:
        return self.intercom.rx_frames

    @property
    def intercom_rx_rejected(self) -> int:
        return self.intercom.rx_rejected

    @property
    def intercom_tx_frames(self) -> int:
        return self.intercom.tx_frames

    @property
    def intercom_rx_sessions(self) -> int:
        return self.intercom.rx_sessions

    @property
    def intercom_last_rx_seq(self) -> int:
        return self.intercom.last_rx_seq

    @property
    def intercom_rx_level(self) -> float:
        return self.intercom.rx_level

    def set_intercom_active(self, active: bool) -> None:
        self.intercom.set_active(active)

    def client_kwargs(self) -> dict[str, Any]:
        """Keyword arguments to connect `AsyncNebiClient` directly to this simulator."""
        return {
            "host": self.host,
            "ucp_port": self.ucp_server.bound_port,
            "config_port": self.http_port,
            "media_port": self.http_port,
            "ota_port": self.http_port,
            "mode_label": "simulator",
        }

    # --- Lifecycle ---

    async def start(self) -> None:
        if self._running:
            return
        if self.http_port == 0:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind((self.host, 0))
                self.http_port = int(s.getsockname()[1])

        self._init_encoder()
        self._seed_demo_media()

        self._zenoh_port = await self.bus.open_peer_listener(self.host, 0)
        self.zenoh_tunnel = ZenohUcpServerTunnel(
            lambda: self.ucp_server.session,
            self._zenoh_port,
            on_tunnel_reset=self.bus.reset_md_ts_session,
        )
        await self.ucp_server.start()
        self.ucp_port = self.ucp_server.bound_port

        app = build_http_app(self)
        cfg = uvicorn.Config(app, host=self.host, port=self.http_port, log_level="warning", lifespan="off")
        self._http_server = uvicorn.Server(cfg)
        self._http_task = asyncio.create_task(self._http_server.serve(), name="sim-http")

        # Wait briefly for HTTP socket readiness
        for _ in range(40):
            if self._http_server.started:
                break
            await asyncio.sleep(0.025)

        self._running = True
        self._sim_task = asyncio.create_task(self._simulation_loop(), name="sim-loop")

    async def stop(self) -> None:
        self._running = False
        self.set_intercom_active(False)
        if self._sim_task is not None:
            self._sim_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._sim_task
            self._sim_task = None

        if self._http_server is not None:
            self._http_server.should_exit = True
        if self._http_task is not None:
            try:
                await asyncio.wait_for(self._http_task, timeout=2.0)
            except Exception as exc:  # slow/failed uvicorn shutdown: cancel it rather than hang stop()
                logger.debug("Simulator HTTP server did not stop cleanly: %r", exc)
                self._http_task.cancel()
            self._http_task = None

        if self.zenoh_tunnel is not None:
            await self.zenoh_tunnel.close()
        await self.ucp_server.stop()
        await self.bus.close()

    def _init_encoder(self) -> None:
        enc: Any = av.CodecContext.create("libx265", "w")  # a video codec context (PyAV stubs differ by version)
        enc.width = self.video_width
        enc.height = self.video_height
        enc.pix_fmt = "yuv420p"
        enc.time_base = Fraction(1, max(1, self.target_fps))
        enc.options = {
            "preset": "ultrafast",
            "tune": "zerolatency",
            "x265-params": "keyint=15:min-keyint=1:bframes=0:repeat-headers=1:log-level=error",
        }
        enc.open()
        self._hevc_enc = enc

    def _seed_demo_media(self) -> None:
        if self.media_files:
            return
        img = self.render_fpv_frame(0.0)
        self.last_rgb_frame = img
        jpg_bytes = encode_jpeg(img, quality=88)
        self.media_counter += 1
        fid = str(self.media_counter)
        stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(time.time() - 120.0))
        self.media_files[fid] = SimMediaAsset(
            file_id=fid,
            session_id=f"IMG_{stamp}",
            filename=f"IMG_{stamp}.jpg",
            file_type="photo",
            mime_type="image/jpeg",
            width=self.video_width,
            height=self.video_height,
            duration_s=0.0,
            created_at=time.time() - 120.0,
            size_bytes=len(jpg_bytes),
            content=jpg_bytes,
        )

    # --- UCP Callbacks ---

    def _on_ucp_peer_connected(self) -> None:
        self.force_idr = True
        # Real LZ110: the robot keeps the previous bit-59 value across client sessions (a new
        # client's first frame CAN produce an edge). Only the estop baseline is re-latched here.
        if self._joy_edge_initialized:
            self._estop_rebaseline = True

    def _on_ucp_joystick_frame(self, word: int) -> None:
        """Emulate how the robot turns a UCP joystick word into motion (edge detection, latches, gear mapping)."""
        st = JoystickState.unpack(word)
        # Wire speed_gear (`0=Soft, 1=Standard, 2=Sport`) -> `sport_mode` (`1=Soft, 2=Standard, 3=Sport`)
        wire_gear = (word >> JOY_GEAR_SHIFT) & JOY_GEAR_MASK
        if wire_gear in (0, 1, 2):
            self.speed_gear = wire_gear + 1
        self.apply_joystick_state(st, from_ucp_wire=True)

    def apply_joystick_state(self, st: JoystickState, *, from_ucp_wire: bool) -> None:
        now = time.monotonic()
        self.joystick = st
        self.last_joystick_ts = now

        stand_rising = False
        stand_falling = False
        if self._estop_rebaseline:
            self._estop_rebaseline = False
            self._prev_estop_bit = st.estop
        if not self._joy_edge_initialized:
            self._joy_edge_initialized = True
            self._prev_stand_fold_bit = st.stand_up_fold
            self._prev_estop_bit = st.estop
            estop_triggered = (not from_ucp_wire) and st.estop
        else:
            estop_triggered = (st.estop != self._prev_estop_bit) if from_ucp_wire else st.estop
            if st.stand_up_fold and not self._prev_stand_fold_bit:
                stand_rising = True
                self._stand_fold_press_ts = now
            elif (not st.stand_up_fold) and self._prev_stand_fold_bit:
                stand_falling = True
            self._prev_stand_fold_bit = st.stand_up_fold
            self._prev_estop_bit = st.estop

        if estop_triggered:
            self.vx_mps = 0.0
            self.vy_mps = 0.0
            self.yaw_rate_rps = 0.0
            self.posture_state = RobotPostureState.FOLDED
        elif stand_rising:
            # Real LZ110 (verified live): bit-59 0->1 edge disables the chassis -> fold sequence.
            if self.posture_state not in (RobotPostureState.FOLDED, RobotPostureState.CHARGING):
                self.vx_mps = 0.0
                self.vy_mps = 0.0
                self.yaw_rate_rps = 0.0
                self.posture_state = RobotPostureState.FOLDED
        elif stand_falling:
            # Real LZ110: bit-59 1->0 edge enables the chassis -> stand up (also cancels a fold).
            if self.posture_state in (RobotPostureState.FOLDED, RobotPostureState.FOLDING, RobotPostureState.CHARGING):
                self.posture_state = RobotPostureState.BALANCING
                self.docked = False
                self.charging = False
        elif not from_ucp_wire and st.posture == PostureCmd.STAND:
            self.posture_state = RobotPostureState.BALANCING
            self.docked = False
            self.charging = False
        elif not from_ucp_wire and st.posture == PostureCmd.FOLD:
            self.posture_state = RobotPostureState.FOLDED

        if not from_ucp_wire and st.speed_gear != SpeedGear.DEFAULT:
            self.speed_gear = int(st.speed_gear)
        if st.jump_trigger and self.jump_z_m <= 0.01 and self.posture_state == RobotPostureState.BALANCING:
            self.jump_vz = 2.2 + 1.8 * 0.8
            self.posture_state = RobotPostureState.JUMPING
        if st.stunt != StuntDirection.NONE and self.stunt_timer <= 0.0:
            self.stunt_dir = int(st.stunt)
            self.stunt_timer = 0.75
            self.posture_state = RobotPostureState.FLIPPING
        if st.turn_around and self.turn_around_rad_rem <= 0.01:
            self.turn_around_rad_rem = math.pi

    def _on_ucp_zenoh_frame(self, raw: bytes) -> None:
        if self.zenoh_tunnel is not None:
            self.zenoh_tunnel.on_ucp_zenoh_frame(raw)

    def _on_intercom_audio(self, opus_frame: bytes) -> None:
        # The real robot never receives app audio on UCP Channel 2 (downlink is Zenoh
        # `/data/audio/playback_data`); legacy clients that still send here are ignored.
        _ = len(opus_frame)

    # --- Fault Injection Helper (for Web UI & Tests) ---

    def inject_hms_fault(self, code_hex: str = "0x01010001", instance: int = 0) -> None:
        code_int = int(code_hex, 16)
        pair = (code_int, int(instance))
        if pair not in self.hms_active_codes:
            self.hms_active_codes.append(pair)
        publish_hms_event(self, code_int, int(instance), kind=1)
        self.hms_pending_commands[self.hms_event_offset + 1] = pair
        publish_hms_action_request(self, code_int, int(instance))

    def clear_sim_hms_fault(self, code: int, instance: int | None = None) -> None:
        """Clear matching active faults (any instance when `instance` is None) and broadcast the clear events."""
        gone = [p for p in self.hms_active_codes if p[0] == code and (instance is None or p[1] == instance)]
        self.hms_active_codes = [p for p in self.hms_active_codes if p not in gone]
        for c, inst in gone:
            publish_hms_event(self, c, inst, kind=2)

    def publish_hms_snapshot(self, seq_num: int | None = None, request_id: int = 0) -> None:
        publish_hms_snapshot(self, seq_num=seq_num, request_id=request_id)

    def press_pairing_button(self) -> None:
        """Simulate holding Nebi's physical button for 5 seconds to authorize BLE pairing."""
        self.ble_peripheral.press_pairing_button()

    def reset_ble_auth(self, require_button: bool = True) -> None:
        """Require a fresh 5-second physical button press on the next BLE bootstrap."""
        self.ble_peripheral.reset_authorization(require_button=require_button)
        self.bus.reset_md_ts_session()

    def simulate_link_drop(self, duration_sec: float = 4.2) -> None:
        """Drop UDP datagrams for `duration_sec` to exercise UCP heartbeat watchdog & auto-reconnect."""
        self.ucp_server.simulate_blackout(duration_sec)

    # --- Zenoh Command Handlers (`app/**` -> `device/**`, see `sim.topics`) ---

    def _on_app_topic(self, topic: str, msg: Message, seq: int) -> None:
        dispatch_app_topic(self, topic, msg, seq)

    # --- Media Capture & Recording Helpers ---

    def capture_photo(self) -> SimMediaAsset:
        img = self.last_rgb_frame or self.render_fpv_frame(time.monotonic())
        jpg = encode_jpeg(img, quality=90)
        self.media_counter += 1
        fid = str(self.media_counter)
        stamp = f"{time.strftime('%Y%m%d_%H%M%S')}_{self.media_counter:03d}"
        asset = SimMediaAsset(
            file_id=fid,
            session_id=f"IMG_{stamp}",
            filename=f"IMG_{stamp}.jpg",
            file_type="photo",
            mime_type="image/jpeg",
            width=img.width,
            height=img.height,
            duration_s=0.0,
            created_at=time.time(),
            size_bytes=len(jpg),
            content=jpg,
        )
        self.media_files[fid] = asset
        self.bus.publish("/status/media/catalog_changed", revision=self.media_counter, storage_kind=1, change_reason=1)
        return asset

    def finalize_video_recording(self) -> SimMediaAsset | None:
        frames = list(self.rec_frames)
        self.rec_frames.clear()
        if not frames:
            frames = [self.last_rgb_frame or self.render_fpv_frame(time.monotonic())]
        dur = max(0.5, time.monotonic() - self.rec_started_at)

        fps = max(10, min(30, round(len(frames) / dur) or 15))
        mp4_bytes = encode_mp4(frames, fps)

        # Firmware layout: VID_<stamp>.mp4 plus a `.thm` JPEG sidecar indexed as file_type "thumbnail".
        stamp = f"{time.strftime('%Y%m%d_%H%M%S')}_{self.media_counter + 1:03d}"
        thm = encode_jpeg(frames[0], quality=80)
        self.media_counter += 1
        thm_id = str(self.media_counter)
        self.media_files[thm_id] = SimMediaAsset(
            file_id=thm_id,
            session_id=f"VID_{stamp}",
            filename=f"VID_{stamp}.thm",
            file_type="thumbnail",
            mime_type="image/jpeg",
            width=frames[0].width,
            height=frames[0].height,
            duration_s=0.0,
            created_at=time.time(),
            size_bytes=len(thm),
            content=thm,
        )
        self.media_counter += 1
        fid = str(self.media_counter)
        asset = SimMediaAsset(
            file_id=fid,
            session_id=f"VID_{stamp}",
            filename=f"VID_{stamp}.mp4",
            file_type="video",
            mime_type="video/mp4",
            width=frames[0].width,
            height=frames[0].height,
            duration_s=dur,
            created_at=time.time(),
            size_bytes=len(mp4_bytes),
            content=mp4_bytes,
            highlights=list(self.rec_highlights),
            fps=fps,
        )
        self.media_files[fid] = asset
        self.bus.publish("/status/media/catalog_changed", revision=self.media_counter, storage_kind=1, change_reason=2)
        return asset

    # --- 25 Hz Physics + Perception + H.265 Video Streaming Loop ---

    async def _simulation_loop(self) -> None:
        t0 = time.monotonic()
        last_t = t0
        tick = 0
        try:
            while self._running:
                loop_start = time.monotonic()
                dt = max(0.005, min(0.1, loop_start - last_t))
                last_t = loop_start
                sim_t = loop_start - t0
                tick += 1

                step_physics(self, sim_t, dt)
                rgb = self.render_fpv_frame(sim_t)
                self.last_rgb_frame = rgb

                if self.recording and tick % 2 == 0 and len(self.rec_frames) < 450:
                    self.rec_frames.append(rgb.copy())

                # Encode & stream Annex-B H.265 over UCP Channel 1
                ucp: UcpEndpointSession | None = self.ucp_server.session
                if ucp is not None and self.vt_streaming and self._hevc_enc is not None:
                    vf = av.VideoFrame.from_image(rgb)
                    vf.pts = self._pts
                    self._pts += 1
                    if self.force_idr:
                        vf.pict_type = 1  # PictureType.I
                        self.force_idr = False
                    pkts = self._hevc_enc.encode(vf)
                    if pkts:
                        au_bytes = b"".join(bytes(p) for p in pkts)
                        ucp.send_video_frame(au_bytes)

                # Publish high-rate telemetry (12.5 Hz) and medium-rate telemetry (2.5 Hz)
                if tick % 2 == 0:
                    publish_fast_telemetry(self)
                if tick % 10 == 0:
                    publish_slow_telemetry(self)

                elapsed = time.monotonic() - loop_start
                sleep_s = max(0.004, (1.0 / max(10, self.target_fps)) - elapsed)
                await asyncio.sleep(sleep_s)
        except asyncio.CancelledError:
            return

    def render_fpv_frame(self, sim_t: float) -> Image.Image:
        """Render the FPV frame at `sim_t` and remember the visible actors' detection boxes."""
        img, self.latest_boxes = render_fpv_frame(self, sim_t)
        return img
