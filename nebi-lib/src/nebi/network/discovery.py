"""Robot discovery across all non-cloud paths: simulator, USB-C NCM, SoftAP, Home LAN (ARP + optional /24 sweep)
and BLE advertisements."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
import subprocess
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any, Protocol

import httpx

from nebi.network.interfaces import (
    NEBI_SOFTAP_PREFIX,
    NEBI_USB_NCM_PREFIX,
    NetworkInterfaceInfo,
    _first_with_prefix,
    create_bound_udp_socket,
    list_network_interfaces_async,
    resolve_interface_ip_async,
)
from nebi.transport.ucp_codec import UCP_ACK, UCP_SYNC
from nebi.types import SOFTAP_HOST, USB_NCM_HOST

if TYPE_CHECKING:
    from nebi.ble import BlePeripheralLink

logger = logging.getLogger(__name__)


class SimulatorLike(Protocol):
    """What discovery needs from an in-process simulator (`nebi.sim.MockNebiRobot`)."""

    @property
    def running(self) -> bool: ...

    @property
    def ble_peripheral(self) -> BlePeripheralLink: ...

    def client_kwargs(self) -> dict[str, Any]: ...


@dataclass
class DiscoveredRobot:
    """A reachable or advertising Nebi robot discovered over USB, LAN, SoftAP, BLE, or Simulator."""

    flow: str  # "simulator" | "usb_ncm" | "lan_sta" | "softap" | "ble_bootstrap"
    name: str
    host: str
    ucp_port: int = 9007
    config_port: int = 8010
    media_port: int = 8084
    ota_port: int = 8088
    interface: str | None = None
    local_ip: str | None = None
    ble_address: str = ""
    rssi: int | None = None
    serial_number: str = ""
    firmware_version: str = ""
    reachable_ip: bool = True
    requires_ble_bootstrap: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


async def probe_nebi_endpoint(
    host: str,
    *,
    ucp_port: int = 9007,
    config_port: int = 8010,
    bind_interface: str | None = None,
    local_ip: str | None = None,
    timeout: float = 0.65,
    ucp_probe: bool = True,
) -> dict[str, Any] | None:
    """Probe `:8010/config/v1/local/version`, then (if `ucp_probe`) UCP `:9007` `ucp:sync` -> `ucp:ack`.

    Safety of the UCP probe: the robot's UCP endpoint is single-client and a `ucp:sync` from a new source port takes
    over the session, so probing a robot that already has a live session (cockpit, phone app) can disturb that
    session. We therefore send only `ucp:sync` and never `ucp:leav`: LEAV forces `conn_state = 0` and resets
    the live session's sequence numbers, whereas SYNC to a connected robot just answers ACK without a reset.
    A warm session left behind by the probe expires after the 3.5 s
    watchdog, and nebi's own connect sends LEAV before SYNC anyway. Retail firmware puts `:8010` behind mTLS,
    so the HTTP step alone cannot find real robots; pass `ucp_probe=False` (or `skip_ucp_hosts` to
    `discover_robots`) for hosts that may have a live session.
    """
    resolved_ip = local_ip or await resolve_interface_ip_async(bind_interface)
    info: dict[str, Any] = {"host": host, "ucp_port": ucp_port, "config_port": config_port}

    # 1. Probe HTTP :8010 version/health
    transport = httpx.AsyncHTTPTransport(local_address=resolved_ip) if resolved_ip else None
    try:
        async with httpx.AsyncClient(
            base_url=f"http://{host}:{config_port}",
            timeout=timeout,
            transport=transport,
        ) as client:
            resp = await client.get("/config/v1/local/version")
            if resp.status_code == 200:
                data = resp.json()
                info["firmware_version"] = str(data.get("firmware_version") or data.get("version") or "")
                info["serial_number"] = str(data.get("sn") or data.get("serial_number") or "")
                info["http_ok"] = True
                return info
    except Exception as exc:  # best-effort probe: any failure means "not found here"
        logger.debug("HTTP probe of %s:%d failed: %r", host, config_port, exc)

    if not ucp_probe:
        return None

    # 2. Fallback UDP :9007 `ucp:sync` -> `ucp:ack` probe (no `ucp:leav`, see docstring)
    loop = asyncio.get_running_loop()
    ack_fut: asyncio.Future[bool] = loop.create_future()

    class _ProbeProto(asyncio.DatagramProtocol):
        def datagram_received(self, data: bytes, _addr: Any) -> None:
            if (data == UCP_ACK or data.startswith(UCP_ACK)) and not ack_fut.done():
                ack_fut.set_result(True)

    sock: socket.socket | None = None
    udp_transport: asyncio.DatagramTransport | None = None
    try:
        sock = create_bound_udp_socket(host, ucp_port, bind_interface=bind_interface, local_ip=resolved_ip)
        udp_transport, _ = await loop.create_datagram_endpoint(_ProbeProto, sock=sock)
        sock = None  # owned by transport now
        udp_transport.sendto(UCP_SYNC)
        ok = await asyncio.wait_for(ack_fut, timeout=timeout)
        if ok:
            info["ucp_ok"] = True
            return info
    except Exception as exc:  # best-effort probe (incl. interface-bind errors): "not found here"
        logger.debug("UCP probe of %s:%d failed: %r", host, ucp_port, exc)
    finally:
        if udp_transport is not None:
            udp_transport.close()
        if sock is not None:
            sock.close()

    return None


def _list_arp_neighbor_ips() -> list[str]:
    """Read recently active IPv4 LAN neighbors from the OS ARP cache (`arp -an`)."""
    ips: list[str] = []
    try:
        out = subprocess.check_output(["arp", "-an"], stderr=subprocess.DEVNULL, text=True, timeout=1.5)
        for m in re.finditer(r"\((\d+\.\d+\.\d+\.\d+)\)\s+at\s+([0-9a-fA-F:]{11,17})", out):
            ip = m.group(1)
            mac = m.group(2).lower()
            if mac in ("ff:ff:ff:ff:ff:ff", "00:00:00:00:00:00"):
                continue
            if ip.startswith(("127.", "169.254.", "224.", "239.")) or ip.endswith(".255"):
                continue
            if ip not in (SOFTAP_HOST, USB_NCM_HOST) and ip not in ips:
                ips.append(ip)
    except Exception as exc:  # best-effort: no ARP tool / unexpected output
        logger.debug("ARP neighbour lookup failed: %r", exc)
    return ips[:24]


async def _list_arp_neighbor_ips_async() -> list[str]:
    return await asyncio.to_thread(_list_arp_neighbor_ips)


async def discover_robots(
    *,
    include_ble: bool = True,
    include_lan: bool = True,
    deep_lan_scan: bool = False,
    simulator: SimulatorLike | None = None,
    mock_ble: BlePeripheralLink | None = None,
    timeout: float = 1.8,
    skip_ucp_hosts: frozenset[str] | set[str] = frozenset(),
) -> list[DiscoveredRobot]:
    """Scan all local non-cloud transports in parallel (`Simulator`, `USB-NCM`, `LAN STA`, `SoftAP`, `BLE`).

    `skip_ucp_hosts`: hosts with a possibly live UCP session (e.g. the robot the caller is connected to); they
    are probed over HTTP only so discovery never takes over their single-client UCP link.
    """
    from nebi.ble import NebiBleClient  # lazy: only needed for the BLE scan

    discovered: list[DiscoveredRobot] = []

    # 1. Active in-process Simulator
    if simulator is not None and simulator.running:
        kw = simulator.client_kwargs()
        discovered.append(
            DiscoveredRobot(
                flow="simulator",
                name="Nebi Simulator (MockNebiRobot)",
                host=kw["host"],
                ucp_port=kw["ucp_port"],
                config_port=kw["config_port"],
                media_port=kw["media_port"],
                ota_port=kw["ota_port"],
                serial_number="LZ110SIM2026X5",
                firmware_version="v1.4.2-sim",
                reachable_ip=True,
            )
        )

    probe_tasks: list[asyncio.Task[DiscoveredRobot | None]] = []

    async def _probe_candidate(flow: str, label: str, host: str, iface: NetworkInterfaceInfo | None) -> DiscoveredRobot | None:
        res = await probe_nebi_endpoint(
            host,
            bind_interface=iface.name if iface else None,
            local_ip=iface.ipv4 if iface else None,
            timeout=min(0.65, timeout),
            ucp_probe=host not in skip_ucp_hosts,
        )
        if res is None:
            return None
        return DiscoveredRobot(
            flow=flow,
            name=label,
            host=host,
            interface=iface.name if iface else None,
            local_ip=iface.ipv4 if iface else None,
            serial_number=res.get("serial_number", ""),
            firmware_version=res.get("firmware_version", ""),
            reachable_ip=True,
        )

    # 2. USB-C NCM (`192.168.123.1`)
    all_ifaces = await list_network_interfaces_async(include_loopback=False)
    usb_if = _first_with_prefix(all_ifaces, NEBI_USB_NCM_PREFIX)
    probe_tasks.append(asyncio.create_task(_probe_candidate("usb_ncm", "Nebi USB-C NCM", USB_NCM_HOST, usb_if)))

    # 3. Direct SoftAP (`192.168.7.1`)
    ap_if = _first_with_prefix(all_ifaces, NEBI_SOFTAP_PREFIX)
    probe_tasks.append(asyncio.create_task(_probe_candidate("softap", f"Nebi SoftAP ({SOFTAP_HOST})", SOFTAP_HOST, ap_if)))

    # 4. Home Wi-Fi LAN (`STA_CONNECTED`) via ARP neighbor table (and optional /24 active TCP :8010 sweep)
    lan_sweep_task: asyncio.Task[list[DiscoveredRobot]] | None = None
    if include_lan:
        for lan_ip in await _list_arp_neighbor_ips_async():
            probe_tasks.append(
                asyncio.create_task(_probe_candidate("lan_sta", f"Nebi Home LAN ({lan_ip})", lan_ip, None))
            )
        if deep_lan_scan:
            lan_sweep_task = asyncio.create_task(
                scan_lan_subnet_for_robots(timeout_per_host=min(0.35, timeout), skip_ucp_hosts=skip_ucp_hosts)
            )

    # 5. BLE Advertisements (`0000FFF0`)
    ble_task: asyncio.Task[list[Any]] | None = None
    if include_ble:
        ble_mock = mock_ble or (simulator.ble_peripheral if simulator else None)
        ble_task = asyncio.create_task(NebiBleClient.scan(timeout=min(1.5, timeout), mock_peripheral=ble_mock))

    seen_hosts: set[str] = {d.host for d in discovered}
    if probe_tasks:
        for item in await asyncio.gather(*probe_tasks, return_exceptions=True):
            if isinstance(item, DiscoveredRobot) and item.host not in seen_hosts:
                seen_hosts.add(item.host)
                discovered.append(item)

    if lan_sweep_task is not None:
        for item in await lan_sweep_task:
            if item.host not in seen_hosts:
                seen_hosts.add(item.host)
                discovered.append(item)

    if ble_task is not None:
        ble_ads = await ble_task
        for ad in ble_ads:
            discovered.append(
                DiscoveredRobot(
                    flow="ble_bootstrap",
                    name=f"{ad.name} (BLE {'Sim' if ad.is_simulated else 'GATT'})",
                    host="127.0.0.1" if ad.is_simulated else SOFTAP_HOST,
                    ble_address=ad.address,
                    rssi=ad.rssi,
                    reachable_ip=bool(ap_if is not None or ad.is_simulated),
                    requires_ble_bootstrap=True,
                )
            )

    return discovered


async def scan_lan_subnet_for_robots(
    *,
    subnet_cidr: str | None = None,
    config_port: int = 8010,
    timeout_per_host: float = 0.35,
    concurrency: int = 64,
    skip_ucp_hosts: frozenset[str] | set[str] = frozenset(),
) -> list[DiscoveredRobot]:
    """Fast async `/24` Home LAN sweep finding Nebi's `:8010` / `:9007` even when cold in the OS ARP cache."""
    all_ifaces = await list_network_interfaces_async(include_loopback=True)
    local_ips = {i.ipv4 for i in all_ifaces}
    if subnet_cidr:
        net = ipaddress.IPv4Network(subnet_cidr, strict=False)
    else:
        lan_ifaces = [i for i in all_ifaces if i.subnet_role == "lan"]
        lan_if = next((i for i in lan_ifaces if i.is_default), None) or next(iter(lan_ifaces), None)
        if lan_if is None:
            return []
        octets = lan_if.ipv4.split(".")
        net = ipaddress.IPv4Network(f"{octets[0]}.{octets[1]}.{octets[2]}.0/24", strict=False)

    sem = asyncio.Semaphore(max(8, concurrency))
    candidates = [
        str(ip)
        for ip in net.hosts()
        if str(ip) not in local_ips and not str(ip).startswith((NEBI_SOFTAP_PREFIX, NEBI_USB_NCM_PREFIX))
    ]

    async def _check_ip(ip_str: str) -> DiscoveredRobot | None:
        async with sem:
            try:
                _r, writer = await asyncio.wait_for(
                    asyncio.open_connection(ip_str, config_port),
                    timeout=timeout_per_host,
                )
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception as exc:  # we only needed the SYN/ACK
                    logger.debug("LAN sweep close of %s failed: %r", ip_str, exc)
            except Exception:  # closed port / timeout: not a candidate
                return None

            res = await probe_nebi_endpoint(
                ip_str, config_port=config_port, timeout=0.6, ucp_probe=ip_str not in skip_ucp_hosts
            )
            if res is None:
                return None
            return DiscoveredRobot(
                flow="lan_sta",
                name=f"Nebi Home LAN ({ip_str})",
                host=ip_str,
                serial_number=res.get("serial_number", ""),
                firmware_version=res.get("firmware_version", ""),
                reachable_ip=True,
            )

    results = await asyncio.gather(*[_check_ip(ip) for ip in candidates], return_exceptions=True)
    return [r for r in results if isinstance(r, DiscoveredRobot)]


