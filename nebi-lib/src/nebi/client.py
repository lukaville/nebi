"""`AsyncNebiClient`: the SDK facade over one robot session (transports, domain controllers, telemetry, recovery).

Supported non-cloud control flows (strategies live in `nebi.flows`, see `AsyncNebiClient.connect_flow()`):
  - `ConnectionFlow.LAN_STA` (`lan_sta`): Direct Home Wi-Fi LAN (`<lan_ip>:9007`/`:8010`/`:8084`/`:8088`)
  - `ConnectionFlow.USB_NCM` (`usb_ncm`): USB-C Ethernet-over-USB (`192.168.123.1`) with split-socket binding
  - `ConnectionFlow.SOFTAP` (`softap`): Robot `192.168.7.1` SoftAP with `IP_BOUND_IF` / `SO_BINDTODEVICE` (`IoTWifi`)
  - `ConnectionFlow.BLE_BOOTSTRAP` (`ble_bootstrap`): BLE GATT (`0000FFF0`) 5s button auth -> `0x50`/`0x51` -> SoftAP
  - `ConnectionFlow.AUTO` (`auto`): Smart cascade (`USB-NCM` -> `Home LAN STA` -> `SoftAP` -> `BLE Bootstrap`)
  - `ConnectionFlow.SIMULATOR` (`simulator`): In-process `MockNebiRobot`

Responsibilities kept here: session establishment (HTTP probe -> UCP -> Zenoh tunnel -> MD-TS -> streams), the
link watchdog + auto-reconnect state machine, the Wi-Fi AP -> STA handoff of a live session, listener registries
and link telemetry. Controllers (`motion`, `camera`, ...) and `intercom` are created once and survive every
transport rebuild.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from google.protobuf.message import Message

from nebi._callbacks import ListenerSet, TaskSet
from nebi.ble import BlePeripheralLink, SoftApCredentials
from nebi.domains import (
    AudioController,
    CameraController,
    GamesController,
    MediaController,
    MotionController,
    SystemController,
    TrackingController,
)
from nebi.domains._base import DomainController
from nebi.exceptions import (
    NebiError,
    NebiHandshakeError,
    NebiLinkLostError,
    NebiWifiProvisionError,
)
from nebi.flows import FlowRequest, StageCallback, infer_flow, troubleshooting_hint
from nebi.flows import connect_flow as _run_connect_flow
from nebi.http.clients import ConfigHttpClient, MediaHttpClient, OtaHttpClient
from nebi.intercom import Intercom
from nebi.latch import latch_key_for_host
from nebi.md_ts import DEFAULT_IDENTITY_STORE, MdTsIdentityStore
from nebi.network import discover_robots, resolve_interface_ip
from nebi.onboarding import pair_ble_and_provision_home_wifi as _run_onboarding
from nebi.telemetry import apply_topic_message
from nebi.transport.ucp import BytesCallback, UcpClient, UcpEndpointSession, VideoFrameCallback
from nebi.transport.zenoh_ucp import ZenohTopicBus, ZenohUcpClientTunnel
from nebi.types import SOFTAP_HOST, ConnectionFlow, ConnectionStage, RobotTelemetrySnapshot

if TYPE_CHECKING:
    from nebi.simulator import MockNebiRobot
    from nebi.speaker import Speaker

logger = logging.getLogger(__name__)
HMS_REFRESH_MIN_S = 2.0

TelemetryCallback = Callable[[RobotTelemetrySnapshot], Any]

RECONNECT_DELAYS_S = (0.4, 0.8, 1.4, 2.2, 3.0)  # backoff per attempt (the last value repeats)
RECONNECT_TIMEOUT_S = 2.2
STAGE_HISTORY_LEN = 16
LINK_METRICS_PERIOD_S = 0.25
JOYSTICK_STREAM_HZ = 50.0  # matches the official app's 20 ms timer
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost")


class AsyncNebiClient:
    """Comprehensive async Python client for the Beni robot (`lz110` / `lz100`)."""

    def __init__(
        self,
        host: str = SOFTAP_HOST,
        *,
        ucp_port: int = 9007,
        config_port: int = 8010,
        media_port: int = 8084,
        ota_port: int = 8088,
        direct_zenoh_endpoint: str | None = None,
        mode_label: str = "robot",
        flow: str | None = None,
        bind_interface: str | None = None,
        local_ip: str | None = None,
        ble_address: str = "",
        heartbeat_timeout: float = 3.5,
        degraded_silence_sec: float = 1.2,
        auto_reconnect: bool = True,
        max_reconnect_attempts: int = 5,
        identity_store: MdTsIdentityStore | None = None,
        robot_public_key: bytes | None = None,
        security_mode: str = "auto",
    ) -> None:
        self.host = host
        self.ucp_port = ucp_port
        self.config_port = config_port
        self.media_port = media_port
        self.ota_port = ota_port
        self.direct_zenoh_endpoint = direct_zenoh_endpoint
        self.flow = infer_flow(host, mode_label, flow)
        self.mode_label = mode_label if mode_label != "robot" else self.flow
        self.bind_interface = bind_interface or ""
        self.local_ip = local_ip or (resolve_interface_ip(bind_interface) if bind_interface else None) or ""
        self.ble_address = ble_address
        self.heartbeat_timeout = heartbeat_timeout
        self.degraded_silence_sec = degraded_silence_sec
        self.auto_reconnect = auto_reconnect
        self.max_reconnect_attempts = max_reconnect_attempts
        self.identity_store = identity_store or (
            MdTsIdentityStore(in_memory=True)
            if self.flow == ConnectionFlow.SIMULATOR.value
            else DEFAULT_IDENTITY_STORE
        )
        self.robot_public_key: bytes | None = robot_public_key
        self.security_mode = security_mode
        self.bootstrapped_ap_credentials: SoftApCredentials | None = None

        self.stage: ConnectionStage = ConnectionStage.DISCONNECTED
        self.last_error: NebiError | None = None

        self.telemetry = RobotTelemetrySnapshot()
        self.telemetry.link.host = host
        self.telemetry.link.ucp_port = ucp_port
        self.telemetry.link.flow = self.flow
        self.telemetry.link.bind_interface = self.bind_interface
        self.telemetry.link.local_ip = self.local_ip
        self.telemetry.link.ble_address = self.ble_address
        self.telemetry.wifi_ipv4 = host
        self._hms_refresh_pending = False
        self._hms_refresh_at = 0.0

        self._bg_tasks = TaskSet("client background")
        self._video_listeners: ListenerSet[VideoFrameCallback] = ListenerSet(self._bg_tasks)
        self._audio_listeners: ListenerSet[BytesCallback] = ListenerSet(self._bg_tasks)
        self._telemetry_listeners: ListenerSet[TelemetryCallback] = ListenerSet(self._bg_tasks)
        self._stage_listeners: ListenerSet[StageCallback] = ListenerSet(self._bg_tasks)

        self._connected = False
        self._closing = False
        self._start_joystick_cfg = True
        self._auto_video_cfg = True
        self._link_task: asyncio.Task[None] | None = None
        self._reconnect_task: asyncio.Task[Any] | None = None
        self._transports_closed = False
        self._domains: tuple[DomainController, ...] = ()  # filled below; rebound on every transport rebuild

        # Transports: rebuilt on reconnect / Wi-Fi handoff (see _build_transports).
        self.ucp: UcpClient
        self.zenoh_tunnel: ZenohUcpClientTunnel
        self.bus: ZenohTopicBus
        self.config_http: ConfigHttpClient
        self.media_http: MediaHttpClient
        self.ota_http: OtaHttpClient
        self._build_transports()

        # Persistent: survive transport rebuilds and always talk to the live bus.
        self.intercom = Intercom(lambda: self.bus, self.add_audio_listener)
        self.motion = MotionController(
            self.bus,
            get_ucp_session=self._get_ucp_session,
            get_telemetry=lambda: self.telemetry,
        )
        self.camera = CameraController(self.bus)
        self.tracking = TrackingController(self.bus)
        self.audio = AudioController(self.bus, self._get_ucp_session, intercom=self.intercom)
        self.games = GamesController(self.bus)
        self.media = MediaController(self.bus, self.media_http)
        self.system = SystemController(self.bus, self.config_http, self.ota_http)
        self.system.get_telemetry = lambda: self.telemetry
        self._domains = (self.motion, self.camera, self.tracking, self.audio, self.games, self.media, self.system)

    def _build_transports(self) -> None:
        """(Re)create UCP, the Zenoh tunnel/bus and the HTTP clients, and re-point the controllers at them."""
        local_addr = self.local_ip or None
        self.ucp = UcpClient(
            host=self.host,
            port=self.ucp_port,
            on_video_frame=self._handle_video_frame,
            on_audio_frame=self._handle_audio_frame,
            on_zenoh_frame=self._handle_zenoh_ucp_frame,
            bind_interface=self.bind_interface or None,
            local_ip=local_addr,
            heartbeat_timeout=self.heartbeat_timeout,
            degraded_silence_sec=self.degraded_silence_sec,
            on_link_lost=self._on_ucp_link_lost,
            on_degraded_change=self._on_ucp_degraded_change,
        )
        self.zenoh_tunnel = ZenohUcpClientTunnel(self._get_ucp_session)
        self.bus = ZenohTopicBus(role="app")
        self.bus.subscribe_all(self._handle_topic_message)

        self.config_http = ConfigHttpClient(host=self.host, port=self.config_port, local_address=local_addr)
        self.media_http = MediaHttpClient(host=self.host, port=self.media_port, local_address=local_addr)
        self.ota_http = OtaHttpClient(host=self.host, port=self.ota_port, local_address=local_addr)
        self._transports_closed = False
        self._rebind_domains()

    def _rebind_domains(self) -> None:
        """Controllers (and the joystick latch/height/gear they hold) survive reconnects and Wi-Fi handoffs, so
        references callers keep (`m = robot.motion`) never go stale: only their transports are swapped."""
        if not self._domains:
            return  # first build: the controllers are created right after
        for ctrl in self._domains:
            ctrl.rebind(self.bus)
        self.media.rebind(self.bus, self.media_http)
        self.system.rebind(self.bus, self.config_http, self.ota_http)

    def _get_ucp_session(self) -> UcpEndpointSession | None:
        return self.ucp.session if self.ucp.connected else None

    @property
    def eyes(self) -> SystemController:
        """Eye LEDs (`set_color`, `play_expression`, `release`, `list_expressions`, `eyes_status`); same object as
        `system`, which also carries the LED API."""
        return self.system

    @property
    def speaker(self) -> Speaker:
        """The robot speaker: `say(text)`, `play(audio)`, `stop()`, `status()` (same object as `intercom.speaker`)."""
        return self.intercom.speaker

    async def say(self, text: str = "", *, emotion: str = "neutral", babble: bool = False, engine: str | None = None,
                  preset: str | None = None) -> dict[str, Any]:
        """Speak `text` through the robot speaker with the default TTS engine and wait until it ends.
        Shortcut for `robot.speaker.say(...)`; emoji in `text` pick the emotion ("Ball? 👀" is curious)."""
        return await self.intercom.speaker.say(text, emotion=emotion, babble=babble, engine=engine, preset=preset)

    @property
    def connected(self) -> bool:
        return self._connected and self.ucp.connected and self.bus.is_open

    @property
    def md_ts_active(self) -> bool:
        """True when the Zenoh session is MD-TS encrypted."""
        return bool(self.bus.md_ts_active)

    async def switch_wifi_to_ap(self) -> dict[str, Any]:
        """Send Nebi back to its SoftAP (192.168.7.1) and reflect that in telemetry."""
        res = await self.system.switch_wifi_to_ap()
        self.telemetry.wifi_mode = "AP"
        self.telemetry.wifi_ipv4 = SOFTAP_HOST
        return res

    async def __aenter__(self) -> AsyncNebiClient:
        await self.connect()
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.close()

    def set_bootstrapped_credentials(self, creds: SoftApCredentials, *, record_ssid: bool = True) -> None:
        """Remember the SoftAP credentials BLE bootstrap fetched (hints, details); optionally record the SSID in
        telemetry, where it also keys the MD-TS robot-key lookup."""
        self.bootstrapped_ap_credentials = creds
        if record_ssid:
            self.telemetry.wifi_ssid = creds.ssid

    # --- Listener Registration (each returns an unsubscribe function) ---

    def add_video_listener(self, callback: VideoFrameCallback) -> Callable[[], None]:
        return self._video_listeners.add(callback)

    def add_audio_listener(self, callback: BytesCallback) -> Callable[[], None]:
        return self._audio_listeners.add(callback)

    def add_telemetry_listener(self, callback: TelemetryCallback) -> Callable[[], None]:
        return self._telemetry_listeners.add(callback)

    def add_stage_listener(self, callback: StageCallback) -> Callable[[], None]:
        return self._stage_listeners.add(callback)

    def _set_stage(
        self,
        stage: ConnectionStage,
        detail: str = "",
        error: NebiError | None = None,
    ) -> None:
        self.stage = stage
        if error is not None:
            self.last_error = error
            self.telemetry.link.last_error = error.to_dict()
        elif stage == ConnectionStage.CONNECTED:
            self.last_error = None
            self.telemetry.link.last_error = None

        lnk = self.telemetry.link
        lnk.stage = stage.value
        lnk.stage_detail = detail
        lnk.flow = self.flow
        lnk.bind_interface = self.bind_interface
        lnk.local_ip = self.local_ip
        lnk.ble_address = self.ble_address
        lnk.stage_history.append({"stage": stage.value, "detail": detail, "ts": round(time.time(), 3)})
        if len(lnk.stage_history) > STAGE_HISTORY_LEN:
            del lnk.stage_history[:-STAGE_HISTORY_LEN]

        self._stage_listeners.emit(stage, detail, error)
        self._notify_telemetry()

    def _spawn(self, coro: Any) -> asyncio.Task[Any]:
        """create_task that keeps a reference (no GC mid-flight) and logs failures instead of losing them."""
        return self._bg_tasks.spawn(coro)

    # --- Connection Flows (strategies in nebi.flows, onboarding in nebi.onboarding) ---

    @classmethod
    async def connect_flow(
        cls,
        flow: str | ConnectionFlow = ConnectionFlow.AUTO,
        *,
        host: str | None = None,
        bind_interface: str | None = None,
        ble_address: str | None = None,
        country_code: str = "US",
        join_wifi: bool = False,
        wait_for_button_sec: float = 20.0,
        timeout: float = 4.0,
        auto_reconnect: bool = True,
        identity_store: MdTsIdentityStore | None = None,
        security_mode: str = "auto",
        simulator: MockNebiRobot | None = None,
        mock_ble: BlePeripheralLink | None = None,
        on_stage: StageCallback | None = None,
    ) -> AsyncNebiClient:
        """Create and connect an `AsyncNebiClient` using any supported local flow (excluding Cloud)."""
        req = FlowRequest(
            host=host,
            bind_interface=bind_interface,
            ble_address=ble_address,
            country_code=country_code,
            join_wifi=join_wifi,
            wait_for_button_sec=wait_for_button_sec,
            timeout=timeout,
            auto_reconnect=auto_reconnect,
            identity_store=identity_store,
            security_mode=security_mode,
            simulator=simulator,
            mock_ble=mock_ble,
            on_stage=on_stage,
        )
        return await _run_connect_flow(cls, flow, req)

    @classmethod
    async def pair_ble_and_provision_home_wifi(
        cls,
        ssid: str,
        password: str = "",
        *,
        security: int = 2,
        ble_address: str | None = None,
        country_code: str = "US",
        wifi_interface: str | None = None,
        switch_os_wifi: bool = True,
        connect_lan_after: bool = True,
        wait_for_button_sec: float = 35.0,
        timeout_sec: int = 30,
        identity_store: MdTsIdentityStore | None = None,
        security_mode: str = "auto",
        simulator: MockNebiRobot | None = None,
        mock_ble: BlePeripheralLink | None = None,
        on_stage: StageCallback | None = None,
    ) -> tuple[dict[str, Any], AsyncNebiClient | None]:
        """One-command end-to-end onboarding:
          1. Connect to Nebi BLE (`0000FFF0`), complete `MD-TS` X25519 button pairing, and fetch SoftAP credentials.
          2. Temporarily join Nebi's `192.168.7.1` SoftAP (if no secondary adapter is already on `192.168.7.x`).
          3. Open an encrypted `MD-TS` Zenoh session to `192.168.7.1:9007` and commit Home Wi-Fi (`ssid`/`password`).
          4. Always restore the host's OS Wi-Fi back to Home Wi-Fi in `finally:`.
          5. Discover Nebi's new Home LAN IP (`STA_CONNECTED`) and optionally return a connected `LAN_STA` client.
        """
        return await _run_onboarding(
            cls,
            ssid,
            password,
            security=security,
            ble_address=ble_address,
            country_code=country_code,
            wifi_interface=wifi_interface,
            switch_os_wifi=switch_os_wifi,
            connect_lan_after=connect_lan_after,
            wait_for_button_sec=wait_for_button_sec,
            timeout_sec=timeout_sec,
            identity_store=identity_store,
            security_mode=security_mode,
            simulator=simulator,
            mock_ble=mock_ble,
            on_stage=on_stage,
        )

    # --- Connection Lifecycle, Watchdog & Auto-Reconnect ---

    async def connect(
        self,
        *,
        timeout: float = 4.0,
        start_joystick_stream: bool = True,
        auto_start_video: bool = True,
    ) -> None:
        self._closing = False
        self._start_joystick_cfg = start_joystick_stream
        self._auto_video_cfg = auto_start_video
        if self._transports_closed:  # close() then connect(): HTTP clients etc. were closed, rebuild them
            self._build_transports()

        try:
            await self._establish_session(timeout=timeout, is_reconnect=False)
        except NebiError as err:
            await self._cancel_reconnect()
            self._set_stage(ConnectionStage.ERROR, err.message, err)
            raise
        except Exception as exc:
            await self._cancel_reconnect()
            wrapped = NebiHandshakeError(
                "CONNECT_FAILED",
                f"Failed connecting to {self.host}:{self.ucp_port}: {exc}",
                stage=self.stage.value,
                recoverable=True,
                hint=self._flow_troubleshooting_hint(),
            )
            self._set_stage(ConnectionStage.ERROR, wrapped.message, wrapped)
            raise wrapped from exc

    async def _cancel_reconnect(self) -> None:
        t = self._reconnect_task
        if t is not None and not t.done() and t is not asyncio.current_task():
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):  # cancelled on purpose; its outcome no longer matters
                pass
        self._reconnect_task = None

    def _flow_troubleshooting_hint(self) -> str:
        return troubleshooting_hint(self.flow, self.host, self.bootstrapped_ap_credentials)

    async def _close_link(self) -> None:
        """Close bus, tunnel and UCP (in that order)."""
        await self.bus.close()
        await self.zenoh_tunnel.close()
        await self.ucp.close()

    async def _establish_session(self, *, timeout: float, is_reconnect: bool) -> None:
        """Full handshake. A reconnect reports no intermediate stages (it stays RECONNECTING until done)."""
        report = not is_reconnect
        await self._probe_config_http(timeout, report)
        await self._open_zenoh_link(timeout, report, is_reconnect)
        await self._negotiate_security(timeout, report)

        self._connected = True
        lnk = self.telemetry.link
        lnk.connected = True
        lnk.mode = self.mode_label
        lnk.zenoh_ready = True
        lnk.md_ts_active = self.bus.md_ts_active
        lnk.degraded = False
        lnk.reconnect_attempt = 0

        await self._start_streams(report)
        if self._link_task is None or self._link_task.done():
            self._link_task = asyncio.create_task(self._link_metrics_loop())
        await self._read_device_info()

        if self.flow == ConnectionFlow.LAN_STA.value:
            self.telemetry.wifi_mode = "STA"
            self.telemetry.wifi_ipv4 = self.host

        self._set_stage(ConnectionStage.CONNECTED, f"Connected via {self.flow} ({self.host}:{self.ucp_port})")
        self._schedule_hms_refresh(delay=0.3)

    def _schedule_hms_refresh(self, delay: float = 0.5) -> None:
        """Debounced `/status/hms/query_active`: re-sync the active fault set after connecting and after each HMS
        event (the robot never publishes the snapshot unprompted). At most one query every HMS_REFRESH_MIN_S."""
        if self._hms_refresh_pending or self._closing:
            return
        self._hms_refresh_pending = True

        async def _run() -> None:
            try:
                wait = max(delay, self._hms_refresh_at + HMS_REFRESH_MIN_S - time.monotonic())
                await asyncio.sleep(wait)
                if self.connected:
                    self._hms_refresh_at = time.monotonic()
                    await self.system.query_active_faults(timeout=2.0)
            except Exception:  # optional re-sync: events alone still keep the set current
                logger.debug("HMS active-fault query failed", exc_info=True)
            finally:
                self._hms_refresh_pending = False

        try:
            self._spawn(_run())
        except RuntimeError:  # no running loop
            self._hms_refresh_pending = False

    async def _probe_config_http(self, timeout: float, report: bool) -> None:
        """Stage 1: non-blocking HTTP :8010 readiness probe (also fills firmware version / SN when it answers)."""
        if report:
            self._set_stage(ConnectionStage.PROBING_HTTP, f"Probing config service http://{self.host}:{self.config_port}...")
        try:
            ver_probe = await asyncio.wait_for(self.config_http.version(), timeout=min(0.9, timeout * 0.35))
        except Exception as exc:  # :8010 is often mTLS-gated or still booting: UCP :9007 is what matters
            logger.debug("config :8010 probe skipped: %s", exc)
            return
        if ver_probe.get("firmware_version"):
            self.telemetry.firmware_version = str(ver_probe["firmware_version"])
        if ver_probe.get("sn"):
            self.telemetry.serial_number = str(ver_probe["sn"])

    async def _open_zenoh_link(self, timeout: float, report: bool, is_reconnect: bool) -> None:
        """Stages 2-3: UCP UDP :9007 + Zenoh-over-UCP tunnel (`0xA4` -> `0xA7` `zenoh.open` -> `0xA5` ready barrier)."""
        max_tunnel_tries = 1 if (self.direct_zenoh_endpoint or is_reconnect) else 2
        for attempt_idx in range(max_tunnel_tries):
            if report:
                self._set_stage(ConnectionStage.UCP_HANDSHAKING, f"Opening UCP v1 UDP session ({self.host}:{self.ucp_port})...")
            try:
                await self.ucp.connect(timeout=timeout, reset_first=(attempt_idx > 0))
            except TimeoutError as exc:
                hint = self._flow_troubleshooting_hint()
                if self.flow == ConnectionFlow.LAN_STA.value:
                    hint = (
                        f"Nebi boots in SoftAP + BLE mode ({SOFTAP_HOST}) after every power-cycle rather than {self.host}. "
                        "Open the Connection menu and choose 'Wake up over Bluetooth and connect to Home Wi-Fi' (no button hold needed once paired) "
                        "or 'Hotspot via Bluetooth'."
                    )
                raise NebiHandshakeError(
                    "UCP_HANDSHAKE_TIMEOUT",
                    f"UCP UDP handshake (`ucp:sync` -> `ucp:ack`) timed out on {self.host}:{self.ucp_port}.",
                    stage=ConnectionStage.UCP_HANDSHAKING.value,
                    recoverable=True,
                    hint=hint,
                    details={"host": self.host, "ucp_port": self.ucp_port, "bind_interface": self.bind_interface},
                ) from exc

            if report:
                self._set_stage(
                    ConnectionStage.ZENOH_TUNNELING,
                    "Negotiating Zenoh-over-UCP tunnel (0xA4 -> Zenoh 0xA7 -> 0xA5) & pub/sub...",
                )
            try:
                if self.direct_zenoh_endpoint:
                    await asyncio.wait_for(self.bus.open_client(self.direct_zenoh_endpoint), timeout=timeout)
                else:
                    endpoint = await self.zenoh_tunnel.start_link()
                    await asyncio.wait_for(self.bus.open_client(endpoint), timeout=max(2.5, timeout))
                    await self.zenoh_tunnel.complete_ready_barrier(timeout=max(2.0, timeout))
                return
            except Exception as exc:
                await self._close_link()
                if attempt_idx + 1 < max_tunnel_tries:
                    logger.info(
                        "Zenoh-over-UCP tunnel attempt %d failed (%s); resetting UCP session with ucp:leav and retrying...",
                        attempt_idx + 1,
                        exc,
                    )
                    await asyncio.sleep(0.12)
                    continue
                raise NebiHandshakeError(
                    "ZENOH_TUNNEL_TIMEOUT",
                    f"Zenoh-over-UCP tunnel handshake failed on {self.host}:{self.ucp_port}: {exc}",
                    stage=ConnectionStage.ZENOH_TUNNELING.value,
                    recoverable=True,
                    hint="Robot accepted UDP :9007 but its Zenoh bridge did not complete; retry connection.",
                ) from exc

    async def _negotiate_security(self, timeout: float, report: bool) -> None:
        """MD-TS X25519 + AES-256-GCM over the Zenoh session; learns/persists the robot key when it succeeds."""
        try:
            robot_pub = self.robot_public_key or self.identity_store.lookup_robot_public_key(
                ble_address=self.ble_address,
                ssid=self.telemetry.wifi_ssid,
                host=self.host,
                allow_default=(self.flow != ConnectionFlow.SIMULATOR.value),
            )
            if report and (self.security_mode == "md-ts" or robot_pub is not None):
                self._set_stage(ConnectionStage.SECURITY_HANDSHAKING, "Negotiating MD-TS X25519 + AES-256-GCM Zenoh session...")
            md_ts_ok = await self.bus.establish_md_ts_session(
                identity_store=self.identity_store,
                robot_public_key=robot_pub,
                security_mode=self.security_mode,
                timeout=min(2.5, max(1.0, timeout)),
            )
            if md_ts_ok and robot_pub:
                self.robot_public_key = robot_pub
                if self.host not in _LOOPBACK_HOSTS:
                    self.identity_store.save_robot_public_key(
                        robot_pub,
                        ble_address=self.ble_address,
                        ssid=self.telemetry.wifi_ssid,
                        host=self.host,
                    )
        except NebiError:
            await self._close_link()
            raise
        except Exception as exc:
            await self._close_link()
            raise NebiHandshakeError(
                "ZENOH_SECURITY_FAILED",
                f"Zenoh MD-TS security handshake failed on {self.host}:{self.ucp_port}: {exc}",
                stage=ConnectionStage.SECURITY_HANDSHAKING.value,
                recoverable=True,
            ) from exc

    async def _start_streams(self, report: bool) -> None:
        """Stage 4: start live H.265 video FIRST (the robot only forwards 31-byte `0x1051` Channel 0 + SACK joystick
        frames to `/cmd/rc/joystick` while live view is enabled), then the 50 Hz
        joystick loop with a clean edge-toggle baseline and the persisted bit-59 latch for this robot."""
        if report:
            self._set_stage(ConnectionStage.STREAM_STARTING, "Starting H.265 FPV stream & 50 Hz control loop...")
        if self._auto_video_cfg:
            try:
                await self.camera.start_live_stream(timeout=1.5)
                self.camera.request_keyframe()
            except Exception as exc:
                logger.debug("Initial start_live_stream skipped: %s", exc)

        self.motion.latch_key = latch_key_for_host(self.host)
        self.motion.reset_session_state()
        if self._start_joystick_cfg:
            await self.motion.start_joystick_stream(hz=JOYSTICK_STREAM_HZ)

    async def _read_device_info(self) -> None:
        """Best-effort hardware/firmware version and serial number (older firmware may not answer)."""
        try:
            ver = await self.system.get_version(timeout=1.0)
            if ver.get("hardware_version"):
                self.telemetry.hardware_version = ver["hardware_version"]
            if ver.get("firmware_version"):
                self.telemetry.firmware_version = ver["firmware_version"]
            self.telemetry.led_direct_supported = bool(ver.get("led_direct_supported", True))
            sn = await self.system.get_serial_number(timeout=1.0)
            if sn:
                self.telemetry.serial_number = sn
        except Exception as exc:
            logger.debug("device info query skipped: %s", exc)

    def _on_ucp_degraded_change(self, is_degraded: bool) -> None:
        if not self._connected or self._closing:
            return
        self.telemetry.link.degraded = is_degraded
        if is_degraded and self.stage == ConnectionStage.CONNECTED:
            self._set_stage(
                ConnectionStage.DEGRADED,
                f"High UCP latency or packet loss ({self.ucp.rx_silence_ms} ms silence, {self.ucp.session.rtt_ms:.0f} ms RTT)",
            )
        elif not is_degraded and self.stage == ConnectionStage.DEGRADED:
            self._set_stage(ConnectionStage.CONNECTED, f"Link healthy ({self.host}:{self.ucp_port})")

    def _on_ucp_link_lost(self, reason: str) -> None:
        # SAFETY CRITICAL: zero any latched joystick velocity on link loss (also mid-handshake), so nothing resumes.
        self.motion.neutral()
        if self._closing or not self._connected:
            # Mid-handshake: connect() is still running and will fail/time out on its own; spawning a reconnect
            # here would race it and could orphan a joystick stream nobody can stop.
            return
        self._connected = False
        self.telemetry.link.connected = False

        err = NebiLinkLostError(
            "LINK_HEARTBEAT_TIMEOUT",
            reason,
            stage=ConnectionStage.RECONNECTING.value if self.auto_reconnect else ConnectionStage.ERROR.value,
            recoverable=True,
            hint=self._flow_troubleshooting_hint(),
        )
        if self.auto_reconnect and (self._reconnect_task is None or self._reconnect_task.done()):
            self._reconnect_task = self._spawn(self._auto_reconnect_loop(err))
        else:
            self._set_stage(ConnectionStage.ERROR, reason, err)

    async def _teardown_session_only(self) -> None:
        await self.system.stop()
        self.motion.neutral()  # last frame out is neutral, and nothing stale resumes on the next session
        await self.motion.stop_joystick_stream()
        await self._close_link()
        await self.config_http.close()
        await self.media_http.close()
        await self.ota_http.close()
        self._transports_closed = True
        # posture/lift telemetry from the old session must not be trusted by the next one
        self.telemetry.robot_state_ts = 0.0

    async def _auto_reconnect_loop(self, initial_err: NebiLinkLostError) -> None:
        for attempt in range(1, self.max_reconnect_attempts + 1):
            if self._closing:
                return
            self.telemetry.link.reconnect_attempt = attempt
            self._set_stage(
                ConnectionStage.RECONNECTING,
                f"Link lost — auto-reconnecting ({attempt}/{self.max_reconnect_attempts}) to {self.host}:{self.ucp_port}...",
                initial_err if attempt == 1 else None,
            )
            await self._teardown_session_only()
            await asyncio.sleep(RECONNECT_DELAYS_S[min(attempt - 1, len(RECONNECT_DELAYS_S) - 1)])
            if self._closing:
                return
            self._build_transports()
            try:
                await self._establish_session(timeout=RECONNECT_TIMEOUT_S, is_reconnect=True)
            except Exception as exc:
                logger.debug("Reconnect attempt %d failed: %s", attempt, exc)
                continue
            logger.info("Reconnected to Nebi at %s:%d on attempt %d", self.host, self.ucp_port, attempt)
            self._restore_intercom_if_active()
            return

        final_err = NebiLinkLostError(
            "RECONNECT_EXHAUSTED",
            f"Lost link to {self.host}:{self.ucp_port} and failed {self.max_reconnect_attempts} reconnect attempts.",
            stage=ConnectionStage.ERROR.value,
            recoverable=True,
            hint=self._flow_troubleshooting_hint(),
        )
        self._set_stage(ConnectionStage.ERROR, final_err.message, final_err)

    def _restore_intercom_if_active(self) -> None:
        if self.intercom.active:
            self._spawn(self._restore_intercom())

    async def _restore_intercom(self) -> None:
        try:
            await self.intercom.restore()
        except Exception as exc:
            logger.warning("Intercom restore after reconnect failed: %s", exc)

    async def provision_home_wifi_and_handoff(
        self,
        ssid: str,
        password: str = "",
        *,
        security: int = 2,
        switch_connection: bool = True,
        timeout_sec: int = 25,
    ) -> dict[str, Any]:
        """Provision Nebi onto Home Wi-Fi (`AP -> STA`) and seamlessly hand off `AsyncNebiClient` to the new LAN IP."""
        prev_auto = self.auto_reconnect
        self.auto_reconnect = False  # Suppress AP-teardown false-alarm reconnect during intentional STA cutover
        self._set_stage(ConnectionStage.WIFI_PROVISIONING, f"Provisioning Nebi onto Home Wi-Fi '{ssid}'...")
        self.telemetry.wifi_provision_state = "provisioning"
        try:
            res = await self.system.provision_home_wifi(
                ssid=ssid,
                password=password,
                security=security,
                wait_for_result=True,
                timeout_sec=timeout_sec,
            )
            lan_ip = str(res.get("ipv4") or "")

            # If AP tore down mid-cutover before returning IP, scan Home LAN ARP + /24 subnet for newly joined robot
            if not lan_ip and res.get("status") == "cutover_in_progress":
                lan_ip = await self._find_robot_on_lan()
                if lan_ip:
                    res["ipv4"] = lan_ip
                    res["status"] = "succeeded"

            self.telemetry.wifi_mode = "STA"
            self.telemetry.wifi_ssid = ssid
            if lan_ip:
                self.telemetry.wifi_ipv4 = lan_ip
            self.telemetry.wifi_provision_state = str(res.get("status", "succeeded"))

            if switch_connection and lan_ip and (lan_ip != self.host or self.bind_interface):
                await self._handoff_to_lan(lan_ip)
                res["handed_off"] = True
            else:
                if self.connected:
                    self._set_stage(ConnectionStage.CONNECTED, f"Home Wi-Fi '{ssid}' active ({lan_ip or self.host})")
                res["handed_off"] = False

            return res
        except NebiWifiProvisionError as err:
            self.telemetry.wifi_provision_state = f"failed:{err.code}"
            # Robot watchdog rolls back to SoftAP on failure — keep current AP session healthy if still alive
            if self.connected:
                self._set_stage(ConnectionStage.CONNECTED, f"Provisioning rolled back safely ({err.code})", err)
            else:
                self._set_stage(ConnectionStage.ERROR, err.message, err)
            raise
        except BaseException as exc:
            # any other failure (handoff connect, discovery, cancellation) must not leave a stale PROVISIONING stage
            self.telemetry.wifi_provision_state = f"failed:{type(exc).__name__}"
            stage_err = exc if isinstance(exc, NebiError) else NebiWifiProvisionError(
                "WIFI_HANDOFF_FAILED", f"Wi-Fi provisioning/handoff failed: {exc}", stage=ConnectionStage.ERROR.value,
                recoverable=True)
            if self.connected:
                self._set_stage(ConnectionStage.CONNECTED, f"Provisioning did not complete ({type(exc).__name__})", stage_err)
            else:
                self._set_stage(ConnectionStage.ERROR, stage_err.message, stage_err)
            raise
        finally:
            self.auto_reconnect = prev_auto

    async def _find_robot_on_lan(self, rounds: int = 8) -> str:
        """Poll the Home LAN for the robot after an STA cutover; never probes our own live UCP session."""
        for _ in range(rounds):
            await asyncio.sleep(1.5)
            found = await discover_robots(
                include_ble=False,
                include_lan=True,
                deep_lan_scan=True,
                timeout=1.5,
                skip_ucp_hosts={self.host},  # probing could reset our own live session
            )
            sta = next((r for r in found if r.flow == ConnectionFlow.LAN_STA.value), None)
            if sta is not None:
                return sta.host
        return ""

    async def _handoff_to_lan(self, lan_ip: str) -> None:
        """Tear down the AP session and re-establish this same client (and its controllers) on `lan_ip`."""
        await self._teardown_session_only()
        self.host = lan_ip
        self.flow = ConnectionFlow.LAN_STA.value
        self.mode_label = ConnectionFlow.LAN_STA.value
        self.bind_interface = ""
        self.local_ip = ""
        self._build_transports()
        await self._establish_session(timeout=4.0, is_reconnect=False)
        self._restore_intercom_if_active()

    async def close(self) -> None:
        self._closing = True
        self._connected = False
        await self._cancel_reconnect()
        self.intercom.detach()

        if self._link_task is not None:
            self._link_task.cancel()
            try:
                await self._link_task
            except asyncio.CancelledError:
                pass
            self._link_task = None

        await self._teardown_session_only()

        self.telemetry.link.connected = False
        self.telemetry.link.mode = "disconnected"
        self.telemetry.link.zenoh_ready = False
        self.telemetry.link.degraded = False
        self._set_stage(ConnectionStage.DISCONNECTED, "Disconnected")

    async def _link_metrics_loop(self) -> None:
        try:
            while not self._closing:
                await asyncio.sleep(LINK_METRICS_PERIOD_S)
                self._sync_link_metrics()
                self._notify_telemetry()
        except asyncio.CancelledError:
            return

    def _sync_link_metrics(self) -> None:
        lnk = self.telemetry.link
        lnk.connected = self.connected
        lnk.mode = self.mode_label if self.connected else self.stage.value
        lnk.flow = self.flow
        lnk.stage = self.stage.value
        lnk.host = self.host
        lnk.ucp_port = self.ucp_port
        lnk.bind_interface = self.bind_interface
        lnk.local_ip = self.local_ip
        lnk.ble_address = self.ble_address
        lnk.degraded = self.ucp.degraded
        lnk.rx_silence_ms = self.ucp.rx_silence_ms
        sess = self.ucp.session
        lnk.rtt_ms = round(sess.rtt_ms, 2)
        lnk.video_fps = round(sess.video_fps, 1)
        lnk.video_kbps = round(sess.video_kbps, 1)
        lnk.video_frames_rx = sess.video_frames_rx
        lnk.audio_frames_rx = sess.audio_frames_rx
        lnk.zenoh_ready = self.bus.is_open
        lnk.md_ts_active = self.bus.md_ts_active
        lnk.zenoh_rx_msgs = self.bus.rx_count
        lnk.zenoh_tx_msgs = self.bus.tx_count
        self.telemetry.updated_at = time.time()

    # --- Internal Transport & Topic Handlers ---

    def _handle_zenoh_ucp_frame(self, raw: bytes) -> None:
        self.zenoh_tunnel.on_ucp_zenoh_frame(raw)

    def _handle_video_frame(self, annexb_au: bytes, is_keyframe: bool, ts_us: int) -> None:
        self._video_listeners.emit(annexb_au, is_keyframe, ts_us)

    def _handle_audio_frame(self, opus_frame: bytes) -> None:
        self._audio_listeners.emit(opus_frame)

    def _notify_telemetry(self) -> None:
        self._telemetry_listeners.emit(self.telemetry)

    def _handle_topic_message(self, topic: str, msg: Message, _seq: int) -> None:
        d = getattr(msg, "data", None)
        if d is None:
            return
        apply_topic_message(self.telemetry, topic, d)
        if topic == "/status/hms/event_broadcast":
            self._schedule_hms_refresh()
        self._sync_link_metrics()
        self._notify_telemetry()
