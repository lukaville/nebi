"""`NebiServerManager`: owns the cockpit's single robot link, the simulator and the video hub.

Collaborators (each its own module):
  * `link_state.LinkStageLog` / `LinkOpRunner` - stage bookkeeping and the single-flight, latest-wins
    guard every link-mutating operation runs through (see that module's docstring)
  * `video.VideoStreamHub` - H.265 -> JPEG fan-out, fed by the attached client
  * `wifi_cache.WifiSsidCache` - host SSID for the status payload, refreshed off the loop

The simulator (`nebi.simulator.MockNebiRobot`) is dev/test infrastructure: it is imported lazily, only
when a simulator flow is actually used.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, Any, TypeVar

from nebi.ble import NebiBleClient
from nebi.client import AsyncNebiClient
from nebi.exceptions import NebiError, NebiNotConnectedError
from nebi.md_ts import DEFAULT_IDENTITY_STORE, MdTsIdentityStore
from nebi.types import SOFTAP_HOST, USB_NCM_HOST, ConnectionFlow, ConnectionStage
from nebi_cockpit.errors import http_status_for_nebi_error
from nebi_cockpit.link_state import STAGE_HISTORY_MAX, LinkOpRunner, LinkStageLog
from nebi_cockpit.schemas import BleBootstrapBody, BleProvisionHomeWifiBody, ConnectionSwitchBody, WifiProvisionBody
from nebi_cockpit.video import VideoStreamHub
from nebi_cockpit.wifi_cache import WifiSsidCache

if TYPE_CHECKING:
    from nebi.simulator import MockNebiRobot

logger = logging.getLogger(__name__)

T = TypeVar("T")

__all__ = ["NebiServerManager", "WifiSsidCache", "http_status_for_nebi_error", "resolve_flow"]

_STAGE_HISTORY_MAX = STAGE_HISTORY_MAX  # backwards-compatible alias

# Robot ports a plain flow connects to; any other port means "direct custom-port connection".
DEFAULT_PORTS = {"ucp_port": 9007, "config_port": 8010, "media_port": 8084, "ota_port": 8088}
CONNECT_TIMEOUT_S = 4.0
BLE_CONNECT_TIMEOUT_S = 6.0
DEFAULT_COUNTRY_CODE = "US"
SIM_BLE_PREFIX = "SIM-BLE"
DIRECT_FLOW = "robot+custom-ports"
"""`resolve_flow()` result for a robot at non-default ports (not a `ConnectionFlow`; never sent by clients)."""


def _uses_custom_ports(body: ConnectionSwitchBody) -> bool:
    return any(getattr(body, name) != port for name, port in DEFAULT_PORTS.items())


def resolve_flow(body: ConnectionSwitchBody) -> str:
    """Map a connect request to a flow id.

    `DIRECT_FLOW` = robot at a custom port (standalone MockNebiRobot, port-forwarded robot). The legacy
    `mode="robot"` picks the flow from the host: USB-NCM IP, SoftAP IP (or no host) or LAN."""
    flow = (body.flow or body.mode or "simulator").strip().lower()
    if flow != "robot":
        return flow
    if body.host and _uses_custom_ports(body):
        return DIRECT_FLOW
    if body.host == USB_NCM_HOST:
        return ConnectionFlow.USB_NCM.value
    if body.host == SOFTAP_HOST or not body.host:
        return ConnectionFlow.SOFTAP.value
    return ConnectionFlow.LAN_STA.value


def masked_credentials(creds: dict[str, Any] | None) -> dict[str, Any] | None:
    """SoftAP credentials without the hotspot password, for status pushes, `/api/status` and error bodies.

    The password is returned only by the request that fetched it (`POST /api/ble/bootstrap`, the BLE connect /
    provisioning responses); everything that is broadcast or polled carries just `password_set`.
    """
    if creds is None:
        return None
    out = {k: v for k, v in creds.items() if k != "password"}
    out["password_set"] = bool(creds.get("password"))
    return out


class NebiServerManager:
    """The cockpit's single robot link (`AsyncNebiClient`), multi-flow transitions, `MockNebiRobot` and video hub."""

    def __init__(self, start_simulator: bool = False) -> None:
        self.start_simulator = start_simulator
        self.simulator: MockNebiRobot | None = None
        self.client: AsyncNebiClient | None = None
        self.video_hub = VideoStreamHub(on_keyframe_needed=self.request_keyframe)
        self.wifi_ssid = WifiSsidCache()
        self._log = LinkStageLog()
        self._link_ops = LinkOpRunner(self._log)

        self.active_flow: str = ConnectionFlow.SIMULATOR.value if start_simulator else "disconnected"
        self.last_conn_request: ConnectionSwitchBody = ConnectionSwitchBody(mode="simulator")
        self.last_ble_credentials: dict[str, Any] | None = None

    # ------------------------------------------------------------------ link state (manager-level)

    @property
    def current_stage(self) -> str:
        return self._log.stage

    @property
    def stage_detail(self) -> str:
        return self._log.detail

    @property
    def stage_history(self) -> list[dict[str, Any]]:
        return self._log.history

    @property
    def last_error(self) -> dict[str, Any] | None:
        return self._log.last_error

    @last_error.setter
    def last_error(self, value: dict[str, Any] | None) -> None:
        self._log.last_error = value

    def record_stage(self, stage: ConnectionStage | str, detail: str = "", err: NebiError | None = None) -> None:
        """Stage callback handed to clients / BLE flows."""
        self._log.record(stage, detail, err)

    def fail_link(self, exc: BaseException, hint: str) -> NebiError:
        """Record an ERROR stage for any exception and return it as a structured `NebiError`."""
        err = self._log.fail(exc, hint)
        if "softap_credentials" in err.details:
            self.last_ble_credentials = err.details["softap_credentials"]
        return err

    @property
    def link_busy(self) -> bool:
        return self._link_ops.busy

    async def run_link_op(self, name: str, fn: Callable[[], Coroutine[Any, Any, T]]) -> T:
        """Run a link-mutating operation through the single-flight guard (cancels the one in flight)."""
        return await self._link_ops.run(name, fn)

    # ------------------------------------------------------------------ client attach / detach

    def request_keyframe(self) -> None:
        """Ask the connected robot for an IDR (new video subscriber, decoder resync)."""
        c = self.client
        if c is not None and c.connected:
            c.camera.request_keyframe()

    def _attach(self, client: AsyncNebiClient) -> None:
        client.add_video_listener(self.video_hub.on_h265_access_unit)
        client.camera.request_keyframe()
        self.client = client
        self.active_flow = client.flow

    async def _drop_client(self) -> None:
        if self.client is not None:
            client, self.client = self.client, None
            await client.close()
        self.video_hub.reset_decoder()

    # ------------------------------------------------------------------ lifecycle

    async def ensure_simulator_started(self) -> MockNebiRobot:
        if self.simulator is None:
            from nebi.simulator import MockNebiRobot  # dev/test infrastructure: only when used

            self.simulator = MockNebiRobot(ucp_port=0, http_port=0, video_fps=24)
            await self.simulator.start()
        return self.simulator

    async def startup(self) -> None:
        if self.start_simulator:
            await self.connect_simulator()

    async def shutdown(self) -> None:
        async with self._link_ops.guard:
            await self._link_ops.cancel_current()
            self.wifi_ssid.cancel()
            if self.client is not None:
                await self.client.close()
                self.client = None
            if self.simulator is not None:
                await self.simulator.stop()
                self.simulator = None
            self.video_hub.close()
            self.record_stage(ConnectionStage.DISCONNECTED, "Server shut down")

    async def disconnect(self) -> dict[str, Any]:
        async def _op() -> dict[str, Any]:
            await self._drop_client()
            self.active_flow = "disconnected"
            self.last_error = None
            self.record_stage(ConnectionStage.DISCONNECTED, "Disconnected by user")
            return self.status_dict()

        return await self.run_link_op("disconnect", _op)

    async def connect_simulator(self) -> dict[str, Any]:
        return await self.switch_connection(ConnectionSwitchBody(mode="simulator"))

    # ------------------------------------------------------------------ link operations

    async def switch_connection(self, body: ConnectionSwitchBody) -> dict[str, Any]:
        self.last_conn_request = body
        return await self.run_link_op("connect", lambda: self._switch_connection(body))

    async def _switch_connection(self, body: ConnectionSwitchBody) -> dict[str, Any]:
        flow = resolve_flow(body)
        await self._drop_client()
        self.last_error = None
        try:
            if flow == ConnectionFlow.SIMULATOR.value:
                client = await self._connect_simulator_flow(body)
            elif flow == DIRECT_FLOW:
                client = await self._connect_direct(body)
            else:
                client = await self._connect_named_flow(flow, body)
        except asyncio.CancelledError:
            self.record_stage(ConnectionStage.DISCONNECTED, "Connection attempt cancelled")
            raise
        except Exception as exc:
            raise self.fail_link(exc, "Check robot power/IP/interface settings or switch to Simulator mode.") from exc

        fresh_creds = None
        if client.bootstrapped_ap_credentials is not None:
            fresh_creds = client.bootstrapped_ap_credentials.to_dict()
            self.last_ble_credentials = fresh_creds
        self._attach(client)
        status = self.status_dict()
        if fresh_creds is not None:  # the full hotspot password only goes to the request that fetched it
            status["ble_credentials"] = fresh_creds
        return status

    async def _connect_simulator_flow(self, body: ConnectionSwitchBody) -> AsyncNebiClient:
        sim = await self.ensure_simulator_started()
        self.active_flow = ConnectionFlow.SIMULATOR.value
        return await AsyncNebiClient.connect_flow(
            ConnectionFlow.SIMULATOR,
            simulator=sim,
            auto_reconnect=body.auto_reconnect,
            on_stage=self.record_stage,
        )

    async def _connect_direct(self, body: ConnectionSwitchBody) -> AsyncNebiClient:
        """Custom-port connection (e.g. standalone MockNebiRobot or port-forwarded robot)."""
        self.active_flow = ConnectionFlow.LAN_STA.value
        client = AsyncNebiClient(
            host=body.host,
            ucp_port=body.ucp_port,
            config_port=body.config_port,
            media_port=body.media_port,
            ota_port=body.ota_port,
            bind_interface=body.bind_interface or None,
            auto_reconnect=body.auto_reconnect,
        )
        client.add_stage_listener(self.record_stage)
        try:
            await client.connect(timeout=CONNECT_TIMEOUT_S)
        except BaseException:
            with contextlib.suppress(Exception):  # best-effort cleanup; the connect error is what matters
                await client.close()
            raise
        return client

    async def _connect_named_flow(self, flow: str, body: ConnectionSwitchBody) -> AsyncNebiClient:
        self.active_flow = flow
        # BLE bootstrap against the simulator's MockBlePeripheral when ble_address == SIM-BLE-* (or a local
        # ble_bootstrap): only if the simulator is already running.
        wants_sim = body.ble_address.startswith(SIM_BLE_PREFIX) or (flow == "ble_bootstrap" and body.host == "127.0.0.1")
        use_sim = self.simulator if wants_sim else None
        return await AsyncNebiClient.connect_flow(
            flow,
            host=body.host or None,
            bind_interface=body.bind_interface or None,
            ble_address=body.ble_address or None,
            country_code=body.country_code or DEFAULT_COUNTRY_CODE,
            join_wifi=body.join_wifi,
            wait_for_button_sec=body.wait_for_button_sec,
            timeout=CONNECT_TIMEOUT_S,
            auto_reconnect=body.auto_reconnect,
            simulator=use_sim,
            on_stage=self.record_stage,
        )

    async def provision_home_wifi_via_ble(self, body: BleProvisionHomeWifiBody) -> dict[str, Any]:
        """Run one-shot BLE `MD-TS` pairing + SoftAP -> Home Wi-Fi (`STA`) cutover and attach the `LAN_STA` client."""
        return await self.run_link_op("ble_provision_home_wifi", lambda: self._provision_home_wifi_via_ble(body))

    async def _provision_home_wifi_via_ble(self, body: BleProvisionHomeWifiBody) -> dict[str, Any]:
        await self._drop_client()
        self.last_error = None
        use_sim: MockNebiRobot | None = None
        store: MdTsIdentityStore | None = None
        if body.use_simulator_ble or body.ble_address.startswith(SIM_BLE_PREFIX):
            use_sim = await self.ensure_simulator_started()
            store = MdTsIdentityStore(in_memory=True)
        sec = body.security if body.security is not None else (2 if body.password else 0)
        try:
            summary, lan_client = await AsyncNebiClient.pair_ble_and_provision_home_wifi(
                body.ssid,
                body.password,
                security=sec,
                ble_address=body.ble_address or None,
                country_code=body.country_code or DEFAULT_COUNTRY_CODE,
                wifi_interface=body.interface or None,
                switch_os_wifi=body.switch_os_wifi,
                connect_lan_after=True,
                wait_for_button_sec=body.wait_for_button_sec,
                timeout_sec=body.timeout_sec,
                identity_store=store,
                simulator=use_sim,
                # registered on the returned client by nebi.client too: no extra add_stage_listener here
                on_stage=self.record_stage,
            )
        except asyncio.CancelledError:
            self.record_stage(ConnectionStage.DISCONNECTED, "BLE provisioning cancelled")
            raise
        except Exception as exc:
            raise self.fail_link(exc, "Hold Nebi's button when asked, check the Wi-Fi password, and retry.") from exc
        if "softap_credentials" in summary:
            self.last_ble_credentials = summary["softap_credentials"]
        if lan_client is not None:
            self._attach(lan_client)
        return {"ok": True, **summary, "connection": self.connection_summary()}

    async def ble_bootstrap(self, body: BleBootstrapBody) -> dict[str, Any]:
        """BLE MD-TS pairing that only fetches the robot's SoftAP credentials (link is not changed)."""
        return await self.run_link_op("ble_bootstrap", lambda: self._ble_bootstrap(body))

    async def _ble_bootstrap(self, body: BleBootstrapBody) -> dict[str, Any]:
        mock_p = None
        store = DEFAULT_IDENTITY_STORE
        if body.use_simulator_ble or body.ble_address.startswith(SIM_BLE_PREFIX):
            sim = await self.ensure_simulator_started()
            mock_p = sim.ble_peripheral
            store = MdTsIdentityStore(in_memory=True)
        self.record_stage(ConnectionStage.BLE_CONNECTING, "Connecting to Nebi BLE GATT (0000FFF0)...")
        ble = NebiBleClient(mock_peripheral=mock_p, identity_store=store)
        try:
            await ble.connect(body.ble_address or None, timeout=BLE_CONNECT_TIMEOUT_S)
            self.record_stage(
                ConnectionStage.BLE_FETCHING_WIFI_INFO,
                "Running MD-TS BLE handshake & querying SoftAP credentials (0x50)...",
            )
            creds = await ble.bootstrap_wifi_credentials(
                country_code=body.country_code,
                wait_for_button_sec=body.wait_for_button_sec,
                on_awaiting_button=lambda: self.record_stage(
                    ConnectionStage.BLE_AWAITING_BUTTON_AUTH,
                    "Hold Nebi's physical button for 3-5 seconds to authorize nebi BLE pairing...",
                ),
            )
        except asyncio.CancelledError:
            self.record_stage(ConnectionStage.DISCONNECTED, "BLE bootstrap cancelled")
            raise
        except Exception as exc:
            raise self.fail_link(exc, "Make sure Nebi is powered on and nearby, then retry.") from exc
        finally:
            with contextlib.suppress(Exception):  # best-effort GATT disconnect
                await ble.close()
        self.last_ble_credentials = creds.to_dict()
        return {"ok": True, "credentials": creds.to_dict()}

    async def provision_wifi_handoff(self, body: WifiProvisionBody) -> dict[str, Any]:
        """Robot joins home Wi-Fi over the current link; the client hands off to the LAN IP."""

        async def _op() -> dict[str, Any]:
            client = self.require_client()
            try:
                res = await client.provision_home_wifi_and_handoff(
                    ssid=body.ssid,
                    password=body.password,
                    security=body.security,
                    switch_connection=body.switch_connection,
                    timeout_sec=body.timeout_sec,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise self.fail_link(exc, "Check the Wi-Fi credentials and that the robot is in range.") from exc
            self.active_flow = client.flow
            return {"ok": True, **res, "connection": self.connection_summary()}

        return await self.run_link_op("wifi_provision", _op)

    # ------------------------------------------------------------------ state

    def require_client(self) -> AsyncNebiClient:
        if self.client is None or not self.client.connected:
            stage = self.client.stage.value if self.client is not None else self.current_stage
            err_hint = (
                self.last_error.get("hint", "")
                if self.last_error
                else "Open the Connection menu: Home Wi-Fi, Wake up over Bluetooth and connect to Home Wi-Fi (after a power-on), Nebi's hotspot, or Simulator."
            )
            raise NebiNotConnectedError(
                f"Robot client is not connected (stage: {stage}).",
                stage=stage,
                hint=err_hint,
            )
        return self.client

    def _simulator_ble_summary(self) -> dict[str, Any] | None:
        if self.simulator is None:
            return None
        p = self.simulator.ble_peripheral
        return {
            "address": p.address,
            "name": p.name,
            "authorized": p.authorized,
            "require_button_auth": p.require_button_auth,
            "advertising": p.advertising,
            "ssid": p.ssid,
        }

    def connection_summary(self) -> dict[str, Any]:
        """One merged view. The manager's fields describe the link while there is no client; once a client
        exists, its live link telemetry overrides them (falling back to the manager's error/history)."""
        req = self.last_conn_request
        summary: dict[str, Any] = {
            "connected": False,
            "flow": self.active_flow,
            "stage": self.current_stage,
            "stage_detail": self.stage_detail,
            "host": req.host,
            "ucp_port": req.ucp_port,
            "bind_interface": req.bind_interface,
            "local_ip": "",
            "ble_address": req.ble_address,
            "md_ts_active": False,
            "degraded": False,
            "reconnect_attempt": 0,
            "rx_silence_ms": 0,
            "last_error": self.last_error,
            "stage_history": self.stage_history,
            "ble_credentials": masked_credentials(self.last_ble_credentials),
            "md_ts_identity": DEFAULT_IDENTITY_STORE.summary_dict(),
            "current_wifi_ssid": self.wifi_ssid.get(),
            "simulator_ble": self._simulator_ble_summary(),
            "link_op": self._link_ops.current_name,
        }
        c = self.client
        if c is not None:
            lnk = c.telemetry.link
            summary.update(
                connected=c.connected,
                flow=c.flow,
                stage=lnk.stage,
                stage_detail=lnk.stage_detail,
                host=c.host,
                ucp_port=c.ucp_port,
                bind_interface=c.bind_interface,
                local_ip=c.local_ip,
                ble_address=c.ble_address,
                md_ts_active=c.md_ts_active,
                degraded=lnk.degraded,
                reconnect_attempt=lnk.reconnect_attempt,
                rx_silence_ms=lnk.rx_silence_ms,
                last_error=lnk.last_error or self.last_error,
                stage_history=lnk.stage_history or self.stage_history,
            )
        return summary

    def status_dict(self) -> dict[str, Any]:
        conn = self.connection_summary()
        if self.client is None:
            return {
                "connected": False,
                "connection": conn,
                "telemetry": None,
                "video": {"frames_decoded": self.video_hub.frames_decoded},
            }
        return {
            "connected": self.client.connected,
            "connection": conn,
            "telemetry": self.client.telemetry.to_dict(),
            "video": {
                "frames_decoded": self.video_hub.frames_decoded,
                "width": self.video_hub.latest_width,
                "height": self.video_hub.latest_height,
            },
        }
