"""Connection strategies behind `AsyncNebiClient.connect_flow()`.

Each `ConnectionFlow` is a strategy registered with `@register_flow(name)`: an async function that receives the
client class and a `FlowRequest` and returns a connected client (or raises a `NebiError`). New flows plug in by
registering another strategy; unknown flow names fall back to the Home LAN (`lan_sta`) strategy.

  - `simulator`:     in-process `MockNebiRobot`
  - `auto`:          discover, then try `usb_ncm` -> `lan_sta` -> `softap` -> `ble_bootstrap` in that order
  - `ble_bootstrap`: BLE GATT pairing -> SoftAP credentials -> (optionally join SoftAP via OS Wi-Fi) -> connect
  - `usb_ncm`:       `192.168.123.1` over a USB-C cable (the robot shows up as a network adapter). Fails fast with
                     `USB_NETWORK_NOT_FOUND` when no 192.168.123.x interface exists and no host is given
  - `softap`:        `192.168.7.1` bound to the interface already on the SoftAP subnet (split-socket)
  - `lan_sta`:       Home Wi-Fi LAN IP (given, or found by a deep LAN scan)
"""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from nebi._callbacks import TaskSet, call_guarded
from nebi.ble import BlePeripheralLink, NebiBleClient, SoftApCredentials
from nebi.exceptions import NebiDiscoveryError, NebiError
from nebi.md_ts import DEFAULT_IDENTITY_STORE, MdTsIdentityStore
from nebi.network import (
    discover_robots,
    find_interface_for_subnet_async,
    join_os_wifi,
    wait_for_interface_subnet,
)
from nebi.types import (
    SOFTAP_HOST,
    SOFTAP_SUBNET,
    USB_NCM_HOST,
    USB_NCM_SUBNET,
    ConnectionFlow,
    ConnectionStage,
)

if TYPE_CHECKING:
    from nebi.client import AsyncNebiClient
    from nebi.simulator import MockNebiRobot

logger = logging.getLogger(__name__)

StageCallback = Callable[[ConnectionStage, str, "NebiError | None"], Any]

# AUTO tries direct IP links before BLE (which needs a button press and an OS Wi-Fi switch).
AUTO_FLOW_PRIORITY: dict[str, int] = {
    ConnectionFlow.USB_NCM.value: 0,
    ConnectionFlow.LAN_STA.value: 1,
    ConnectionFlow.SOFTAP.value: 2,
    ConnectionFlow.BLE_BOOTSTRAP.value: 3,
}

# Stage callbacks for flows that run before a client object exists (async callbacks are kept alive here).
_PRE_CLIENT_TASKS = TaskSet("stage callback")


def infer_flow(host: str, mode_label: str, flow: str | None) -> str:
    """The flow a client constructed without an explicit `flow` is using, from its host."""
    if flow:
        return flow
    if mode_label == "simulator":
        return ConnectionFlow.SIMULATOR.value
    if host == USB_NCM_HOST:
        return ConnectionFlow.USB_NCM.value
    if host == SOFTAP_HOST:
        return ConnectionFlow.SOFTAP.value
    return ConnectionFlow.LAN_STA.value


def normalize_flow(flow: str | ConnectionFlow, host: str | None) -> str:
    flow_val = flow.value if isinstance(flow, ConnectionFlow) else str(flow).strip().lower()
    if flow_val == "robot":
        flow_val = infer_flow(host or SOFTAP_HOST, "robot", None)
    return flow_val


AFTER_BOOT_HINT = (
    "After every power-on Nebi does not rejoin Wi-Fi by itself, it only listens on Bluetooth: re-send the Wi-Fi "
    "over BLE (cockpit 'Wake up over Bluetooth and connect to Home Wi-Fi', or `nebi setup-wifi`), then connect on the LAN."
)


def troubleshooting_hint(flow: str, host: str, creds: SoftApCredentials | None) -> str:
    """User-facing hint for a failed/lost connection on `flow`."""
    if flow == ConnectionFlow.USB_NCM.value:
        return (f"Check that Nebi is connected with a USB-C data cable and appears as a network adapter ({USB_NCM_HOST}). "
                "Some robots/firmware only charge over USB-C: then connect over Wi-Fi instead.")
    if flow in (ConnectionFlow.SOFTAP.value, ConnectionFlow.BLE_BOOTSTRAP.value):
        if creds is not None:
            return (
                f"BLE retrieved SoftAP '{creds.ssid}' (password in the connection details). "
                f"Connect a Wi-Fi adapter to '{creds.ssid}' or bind that interface so {SOFTAP_HOST} is reachable."
            )
        return f"Ensure this machine has a Wi-Fi interface joined to Nebi's SoftAP ({SOFTAP_HOST}) or use BLE Bootstrap."
    if flow == ConnectionFlow.LAN_STA.value:
        return (f"Verify Nebi is on your Wi-Fi at {host} (same subnet, ports 9007 UDP & 8010 TCP). "
                + AFTER_BOOT_HINT)
    return "Verify Nebi is powered on and reachable, or switch to Simulator mode."


def redacted_credentials(creds: SoftApCredentials) -> dict[str, Any]:
    """SoftAP credentials for error details (password removed)."""
    return {k: v for k, v in creds.to_dict().items() if k != "password"}


@dataclass
class FlowRequest:
    """Everything a connection strategy may need (the keyword arguments of `connect_flow()`)."""

    host: str | None = None
    bind_interface: str | None = None
    ble_address: str | None = None
    country_code: str = "US"
    join_wifi: bool = False
    wait_for_button_sec: float = 20.0
    timeout: float = 4.0
    auto_reconnect: bool = True
    identity_store: MdTsIdentityStore | None = None
    security_mode: str = "auto"
    simulator: MockNebiRobot | None = None
    mock_ble: BlePeripheralLink | None = None
    on_stage: StageCallback | None = None

    @property
    def ble_peripheral(self) -> BlePeripheralLink | None:
        """Mock BLE peripheral to use (explicit, else the simulator's), or None for real BLE."""
        return self.mock_ble or (self.simulator.ble_peripheral if self.simulator else None)

    def emit(self, stage: ConnectionStage, detail: str, err: NebiError | None = None) -> None:
        """Report a stage before a client (and its stage listeners) exists."""
        if self.on_stage is not None:
            call_guarded(self.on_stage, (stage, detail, err), _PRE_CLIENT_TASKS)

    def build(self, client_cls: type[AsyncNebiClient], **kwargs: Any) -> AsyncNebiClient:
        """Construct a client with this request's common options and the stage listener attached."""
        opts: dict[str, Any] = {
            "auto_reconnect": self.auto_reconnect,
            "identity_store": self.identity_store,
            "security_mode": self.security_mode,
        }
        opts.update(kwargs)
        client = client_cls(**opts)
        if self.on_stage is not None:
            client.add_stage_listener(self.on_stage)
        return client


FlowStrategy = Callable[["type[AsyncNebiClient]", FlowRequest], Awaitable["AsyncNebiClient"]]
_FLOW_STRATEGIES: dict[str, FlowStrategy] = {}


def register_flow(name: str | ConnectionFlow) -> Callable[[FlowStrategy], FlowStrategy]:
    """Register a connection strategy for `name` (replaces an existing one)."""
    key = name.value if isinstance(name, ConnectionFlow) else name

    def _register(fn: FlowStrategy) -> FlowStrategy:
        _FLOW_STRATEGIES[key] = fn
        return fn

    return _register


def registered_flows() -> list[str]:
    return list(_FLOW_STRATEGIES)


async def connect_flow(
    client_cls: type[AsyncNebiClient], flow: str | ConnectionFlow, req: FlowRequest
) -> AsyncNebiClient:
    """Run the strategy for `flow` (unknown names use `lan_sta`)."""
    flow_val = normalize_flow(flow, req.host)
    strategy = _FLOW_STRATEGIES.get(flow_val, _FLOW_STRATEGIES[ConnectionFlow.LAN_STA.value])
    return await strategy(client_cls, req)


async def connect_or_close(client: AsyncNebiClient, timeout: float) -> AsyncNebiClient:
    """Connect, or close the half-built client (HTTP clients, sockets, tasks) and re-raise."""
    try:
        await client.connect(timeout=timeout)
    except BaseException:
        try:
            await client.close()
        except Exception as close_exc:  # the connect error is the one worth reporting
            logger.debug("closing half-built client failed: %s", close_exc)
        raise
    return client


async def ble_fetch_softap(
    *,
    ble_mock: BlePeripheralLink | None,
    identity_store: MdTsIdentityStore | None,
    security_mode: str,
    ble_address: str | None,
    connect_timeout: float,
    country_code: str,
    wait_for_button_sec: float,
    emit: Callable[..., None],
) -> tuple[SoftApCredentials, MdTsIdentityStore, bytes | None]:
    """BLE GATT connect + MD-TS pairing + SoftAP credential fetch (shared by the BLE flows and onboarding)."""
    store = identity_store or (MdTsIdentityStore(in_memory=True) if ble_mock is not None else DEFAULT_IDENTITY_STORE)
    emit(ConnectionStage.BLE_CONNECTING, "Connecting to Nebi BLE GATT service (0000FFF0)...")
    ble = NebiBleClient(mock_peripheral=ble_mock, identity_store=store, security_mode=security_mode)
    try:
        await ble.connect(ble_address or None, timeout=connect_timeout)
        emit(ConnectionStage.BLE_FETCHING_WIFI_INFO, "Running MD-TS BLE X25519 handshake & querying SoftAP credentials (0x50)...")
        creds = await ble.bootstrap_wifi_credentials(
            country_code=country_code,
            wait_for_button_sec=wait_for_button_sec,
            on_awaiting_button=lambda: emit(
                ConnectionStage.BLE_AWAITING_BUTTON_AUTH,
                "Hold Nebi's physical button for 3-5 seconds until it chimes to authorize nebi...",
            ),
        )
    finally:
        await ble.close()
    robot_pub = bytes.fromhex(creds.robot_public_key_hex) if creds.robot_public_key_hex else None
    return creds, store, robot_pub


def bootstrapped_into_simulator(simulator: MockNebiRobot | None, ble_mock: BlePeripheralLink | None, creds: SoftApCredentials) -> bool:
    """True when BLE bootstrap ran against the simulator's peripheral: connect to its bound UCP/HTTP ports."""
    return simulator is not None and ble_mock is simulator.ble_peripheral and creds.ap_ip == simulator.host


async def _subnet_interface(bind_interface: str | None, subnet: str) -> str | None:
    """The explicit interface, else the host interface already holding an address on `subnet`."""
    if bind_interface:
        return bind_interface
    iface = await find_interface_for_subnet_async(subnet)
    return iface.name if iface else None


# --- Strategies -------------------------------------------------------------------------------------------------


@register_flow(ConnectionFlow.SIMULATOR)
async def _simulator_flow(client_cls: type[AsyncNebiClient], req: FlowRequest) -> AsyncNebiClient:
    if req.simulator is None:
        raise NebiDiscoveryError(
            "SIMULATOR_NOT_RUNNING",
            "Simulator flow requested, but no MockNebiRobot instance was provided.",
            stage=ConnectionStage.DISCOVERING.value,
        )
    client = req.build(client_cls, **req.simulator.client_kwargs(), flow=ConnectionFlow.SIMULATOR.value)
    return await connect_or_close(client, req.timeout)


@register_flow(ConnectionFlow.AUTO)
async def _auto_flow(client_cls: type[AsyncNebiClient], req: FlowRequest) -> AsyncNebiClient:
    req.emit(ConnectionStage.DISCOVERING, "Scanning Home LAN, SoftAP, and BLE...")
    found = await discover_robots(
        include_ble=True,
        include_lan=True,
        simulator=None,
        mock_ble=req.ble_peripheral,
        timeout=min(2.2, req.timeout),
    )
    found.sort(key=lambda r: AUTO_FLOW_PRIORITY.get(r.flow, 9))
    last_err: NebiError | None = None
    for cand in found:
        if cand.flow != ConnectionFlow.BLE_BOOTSTRAP.value and cand.reachable_ip:
            client = req.build(
                client_cls,
                host=cand.host,
                ucp_port=cand.ucp_port,
                config_port=cand.config_port,
                media_port=cand.media_port,
                ota_port=cand.ota_port,
                flow=cand.flow,
                mode_label=cand.flow,
                bind_interface=req.bind_interface or cand.interface,
                local_ip=cand.local_ip,
            )
            try:
                return await connect_or_close(client, req.timeout)
            except NebiError as err:  # try the next candidate instead of giving up on the first
                last_err = err
                logger.info("AUTO: %s via %s failed (%s); trying next candidate", cand.host, cand.flow, err.code)
                continue
        if cand.flow == ConnectionFlow.BLE_BOOTSTRAP.value:
            ble_req = dataclasses.replace(req, ble_address=cand.ble_address)
            return await _FLOW_STRATEGIES[ConnectionFlow.BLE_BOOTSTRAP.value](client_cls, ble_req)

    if last_err is not None:
        raise last_err
    raise NebiDiscoveryError(
        "NO_ROBOT_FOUND",
        f"No Nebi robot found on the Home Wi-Fi LAN, its SoftAP ({SOFTAP_HOST}), or BLE.",
        stage=ConnectionStage.DISCOVERING.value,
        recoverable=True,
        hint=AFTER_BOOT_HINT,
    )


@register_flow(ConnectionFlow.BLE_BOOTSTRAP)
async def _ble_bootstrap_flow(client_cls: type[AsyncNebiClient], req: FlowRequest) -> AsyncNebiClient:
    ble_mock = req.ble_peripheral
    creds, store, robot_pub = await ble_fetch_softap(
        ble_mock=ble_mock,
        identity_store=req.identity_store,
        security_mode=req.security_mode,
        ble_address=req.ble_address,
        connect_timeout=min(6.0, req.timeout + 2.0),
        country_code=req.country_code,
        wait_for_button_sec=req.wait_for_button_sec,
        emit=req.emit,
    )
    common = {
        "flow": ConnectionFlow.BLE_BOOTSTRAP.value,
        "ble_address": creds.ble_address,
        "identity_store": store,
        "robot_public_key": robot_pub,
    }

    if bootstrapped_into_simulator(req.simulator, ble_mock, creds):
        assert req.simulator is not None  # narrowed by bootstrapped_into_simulator()
        client = req.build(client_cls, **req.simulator.client_kwargs(), **common)
        client.set_bootstrapped_credentials(creds)
        return await connect_or_close(client, req.timeout)

    target_host = req.host or creds.ap_ip or SOFTAP_HOST
    ap_iface = await _subnet_interface(req.bind_interface, SOFTAP_SUBNET)
    if req.join_wifi and ap_iface is None:
        req.emit(ConnectionStage.JOINING_SOFTAP, f"Joining SoftAP '{creds.ssid}' via OS Wi-Fi...")
        await join_os_wifi(creds.ssid, creds.password, interface=req.bind_interface)
        if detected := await wait_for_interface_subnet(SOFTAP_SUBNET, interface=req.bind_interface, timeout=10.0):
            ap_iface = detected.name

    client = req.build(
        client_cls, host=target_host, mode_label=ConnectionFlow.BLE_BOOTSTRAP.value, bind_interface=ap_iface, **common
    )
    client.set_bootstrapped_credentials(creds)
    try:
        await connect_or_close(client, req.timeout)
    except NebiError as exc:
        exc.details["softap_credentials"] = redacted_credentials(creds)
        if not exc.hint:
            exc.hint = (
                f"BLE fetched SSID '{creds.ssid}' (password in the connection details). "
                f"Join '{creds.ssid}' on a Wi-Fi interface (or enable Auto-Join OS Wi-Fi) so {target_host} is reachable."
            )
        raise
    return client


async def _direct_ip_flow(
    client_cls: type[AsyncNebiClient], req: FlowRequest, flow: ConnectionFlow, host: str, bind_interface: str | None
) -> AsyncNebiClient:
    client = req.build(client_cls, host=host, flow=flow.value, mode_label=flow.value, bind_interface=bind_interface)
    return await connect_or_close(client, req.timeout)


@register_flow(ConnectionFlow.USB_NCM)
async def _usb_ncm_flow(client_cls: type[AsyncNebiClient], req: FlowRequest) -> AsyncNebiClient:
    iface = await _subnet_interface(req.bind_interface, USB_NCM_SUBNET)
    if iface is None and (not req.host or req.host == USB_NCM_HOST):
        raise NebiDiscoveryError(
            "USB_NETWORK_NOT_FOUND",
            f"No USB network adapter to Nebi found (expected a 192.168.123.x interface, robot at {USB_NCM_HOST}).",
            stage=ConnectionStage.DISCOVERING.value,
            recoverable=True,
            hint="Plug Nebi in with a USB-C data cable (not charge-only), wait a few seconds for the adapter to "
            "appear, then retry. Some robots/firmware only charge over USB-C: then use Wi-Fi (`lan_sta`, `softap`, "
            "or Bluetooth wake-up after a power-on).",
        )
    return await _direct_ip_flow(client_cls, req, ConnectionFlow.USB_NCM, req.host or USB_NCM_HOST, iface)


@register_flow(ConnectionFlow.SOFTAP)
async def _softap_flow(client_cls: type[AsyncNebiClient], req: FlowRequest) -> AsyncNebiClient:
    iface = await _subnet_interface(req.bind_interface, SOFTAP_SUBNET)
    return await _direct_ip_flow(client_cls, req, ConnectionFlow.SOFTAP, req.host or SOFTAP_HOST, iface)


async def discover_lan_host(timeout: float) -> str | None:
    """Deep-scan the Home LAN and return the first robot in station mode."""
    found = await discover_robots(include_ble=False, include_lan=True, deep_lan_scan=True, timeout=timeout)
    return next((r.host for r in found if r.flow == ConnectionFlow.LAN_STA.value), None)


@register_flow(ConnectionFlow.LAN_STA)
async def _lan_sta_flow(client_cls: type[AsyncNebiClient], req: FlowRequest) -> AsyncNebiClient:
    target_host = req.host or ""
    if not target_host:
        req.emit(ConnectionStage.DISCOVERING, "Scanning Home Wi-Fi LAN for Nebi...")
        target_host = await discover_lan_host(min(2.0, req.timeout)) or ""
        if not target_host:
            raise NebiDiscoveryError(
                "LAN_ROBOT_NOT_FOUND",
                "Could not auto-discover Nebi on the Home Wi-Fi LAN.",
                stage=ConnectionStage.DISCOVERING.value,
                recoverable=True,
                hint="Enter Nebi's LAN IPv4 address (e.g. 192.0.2.10). " + AFTER_BOOT_HINT,
            )
    return await _direct_ip_flow(client_cls, req, ConnectionFlow.LAN_STA, target_host, req.bind_interface)


__all__ = [
    "AUTO_FLOW_PRIORITY",
    "FlowRequest",
    "FlowStrategy",
    "StageCallback",
    "connect_flow",
    "connect_or_close",
    "infer_flow",
    "register_flow",
    "registered_flows",
    "troubleshooting_hint",
]
