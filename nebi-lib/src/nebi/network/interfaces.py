"""Host network interfaces: enumeration (`ifconfig` / `ip`), name -> IPv4 resolution, subnet waits, and
split-socket UDP binding (`IP_BOUND_IF` on macOS, `SO_BINDTODEVICE` on Linux) so the robot link can be pinned to
one interface (`IoTWifi` / USB-NCM) while Home Wi-Fi keeps the default route.

Enumeration shells out (blocking); from async code use the `*_async` variants, which run it in a thread.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
import struct
import subprocess
import sys
from dataclasses import asdict, dataclass
from typing import Any

from nebi.exceptions import NebiDiscoveryError
from nebi.types import SOFTAP_HOST, USB_NCM_HOST

logger = logging.getLogger(__name__)

NEBI_SOFTAP_PREFIX = SOFTAP_HOST.rsplit(".", 1)[0] + "."     # "192.168.7."
NEBI_USB_NCM_PREFIX = USB_NCM_HOST.rsplit(".", 1)[0] + "."   # "192.168.123."


def _ipv4_literal(value: str) -> str | None:
    """`value` stripped if it is an IPv4 address literal, else `None`."""
    val = value.strip()
    try:
        ipaddress.IPv4Address(val)
    except ValueError:
        return None
    return val


def _first_with_prefix(ifaces: list[NetworkInterfaceInfo], prefix: str) -> NetworkInterfaceInfo | None:
    return next((i for i in ifaces if i.ipv4.startswith(prefix)), None)


DARWIN_IP_BOUND_IF = 25  # <netinet/in.h> IP_BOUND_IF on macOS / iOS


@dataclass
class NetworkInterfaceInfo:
    """Host network interface metadata for split-socket (`IoTWifi` / USB-NCM) pinning."""

    name: str
    ipv4: str
    netmask: str = "255.255.255.0"
    kind: str = "other"  # "wifi" | "usb_ncm" | "ethernet" | "loopback" | "other"
    is_up: bool = True
    is_default: bool = False
    subnet_role: str = "lan"  # "nebi_softap" | "nebi_usb_ncm" | "loopback" | "lan"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _classify_subnet_role(ipv4: str) -> str:
    if ipv4.startswith("127."):
        return "loopback"
    if ipv4.startswith(NEBI_SOFTAP_PREFIX):
        return "nebi_softap"
    if ipv4.startswith(NEBI_USB_NCM_PREFIX):
        return "nebi_usb_ncm"
    return "lan"


def _classify_iface_kind(name: str, ipv4: str) -> str:
    low = name.lower()
    if low.startswith("lo") or ipv4.startswith("127."):
        return "loopback"
    if ipv4.startswith(NEBI_USB_NCM_PREFIX) or low.startswith(("usb", "ncm", "rndis")):
        return "usb_ncm"
    if low.startswith(("wl", "wlan", "wifi", "ath")):
        return "wifi"
    if low.startswith("en"):
        return "wifi" if low in ("en0", "en1") else "ethernet"
    return "other"


def _detect_default_interface() -> str | None:
    try:
        if sys.platform == "darwin":
            out = subprocess.check_output(
                ["route", "-n", "get", "default"],
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=1.5,
            )
            m = re.search(r"interface:\s*(\S+)", out)
            if m:
                return m.group(1)
        elif sys.platform.startswith("linux"):
            out = subprocess.check_output(
                ["ip", "route", "show", "default"],
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=1.5,
            )
            m = re.search(r"\bdev\s+(\S+)", out)
            if m:
                return m.group(1)
    except Exception as exc:  # best-effort: no route tool / unexpected output
        logger.debug("Default-route lookup failed: %r", exc)
    return None


def list_network_interfaces(include_loopback: bool = False) -> list[NetworkInterfaceInfo]:
    """Enumerate active IPv4 OS network interfaces and detect USB-NCM / Nebi SoftAP subnets."""
    default_if = _detect_default_interface()
    results: list[NetworkInterfaceInfo] = []

    try:
        if sys.platform == "darwin":
            out = subprocess.check_output(["ifconfig"], stderr=subprocess.DEVNULL, text=True, timeout=2.0)
            cur_name: str | None = None
            cur_up = False
            for line in out.splitlines():
                hdr = re.match(r"^([a-zA-Z0-9_]+):\s+flags=\d+<([^>]*)>", line)
                if hdr:
                    cur_name = hdr.group(1)
                    flags = hdr.group(2).split(",")
                    cur_up = "UP" in flags and "RUNNING" in flags
                    continue
                if cur_name and cur_up:
                    m = re.search(r"\binet\s+(\d+\.\d+\.\d+\.\d+)\s+netmask\s+(0x[0-9a-fA-F]+|\d+\.\d+\.\d+\.\d+)", line)
                    if m:
                        ip = m.group(1)
                        raw_mask = m.group(2)
                        if raw_mask.startswith("0x"):
                            mask_int = int(raw_mask, 16)
                            netmask = socket.inet_ntoa(struct.pack("!I", mask_int))
                        else:
                            netmask = raw_mask
                        if not include_loopback and ip.startswith("127."):
                            continue
                        results.append(
                            NetworkInterfaceInfo(
                                name=cur_name,
                                ipv4=ip,
                                netmask=netmask,
                                kind=_classify_iface_kind(cur_name, ip),
                                is_up=True,
                                is_default=(cur_name == default_if),
                                subnet_role=_classify_subnet_role(ip),
                            )
                        )
        else:
            out = subprocess.check_output(["ip", "-o", "-4", "addr", "show", "up"], stderr=subprocess.DEVNULL, text=True, timeout=2.0)
            for line in out.splitlines():
                m = re.search(r"^\d+:\s+(\S+)\s+inet\s+(\d+\.\d+\.\d+\.\d+)/(\d+)", line)
                if not m:
                    continue
                ifname, ip, prefix = m.group(1), m.group(2), int(m.group(3))
                if not include_loopback and ip.startswith("127."):
                    continue
                net = ipaddress.IPv4Network(f"0.0.0.0/{prefix}")
                results.append(
                    NetworkInterfaceInfo(
                        name=ifname,
                        ipv4=ip,
                        netmask=str(net.netmask),
                        kind=_classify_iface_kind(ifname, ip),
                        is_up=True,
                        is_default=(ifname == default_if),
                        subnet_role=_classify_subnet_role(ip),
                    )
                )
    except Exception as exc:
        logger.debug("Interface enumeration fallback: %s", exc)

    if not results and include_loopback:
        results.append(
            NetworkInterfaceInfo(
                name="lo0" if sys.platform == "darwin" else "lo",
                ipv4="127.0.0.1",
                netmask="255.0.0.0",
                kind="loopback",
                is_up=True,
                is_default=False,
                subnet_role="loopback",
            )
        )

    return results


def resolve_interface_ip(bind_interface: str | None) -> str | None:
    """Resolve an interface name (e.g. `en7`, `wlan1`) or IPv4 string to its local IPv4 address."""
    if not bind_interface:
        return None
    val = bind_interface.strip()
    if not val:
        return None
    if _ipv4_literal(val) is not None:
        return val

    for iface in list_network_interfaces(include_loopback=True):
        if iface.name == val:
            return iface.ipv4

    raise NebiDiscoveryError(
        "INTERFACE_NOT_FOUND",
        f"Network interface '{val}' has no active IPv4 address.",
        stage="discovering",
        recoverable=True,
        hint="Select an active interface in the Connection dialog or leave Interface set to Auto.",
    )


def find_interface_for_subnet(prefix: str) -> NetworkInterfaceInfo | None:
    """Find an active host interface already holding an IP on `prefix` (e.g. `192.168.7.` or `192.168.123.`)."""
    return _first_with_prefix(list_network_interfaces(include_loopback=False), prefix)


async def list_network_interfaces_async(include_loopback: bool = False) -> list[NetworkInterfaceInfo]:
    """`list_network_interfaces` without blocking the event loop (runs `ifconfig`/`ip`/`route` in a thread)."""
    return await asyncio.to_thread(list_network_interfaces, include_loopback)


async def find_interface_for_subnet_async(prefix: str) -> NetworkInterfaceInfo | None:
    return _first_with_prefix(await list_network_interfaces_async(include_loopback=False), prefix)


async def resolve_interface_ip_async(bind_interface: str | None) -> str | None:
    if not bind_interface:
        return None
    literal = _ipv4_literal(bind_interface)
    if literal is not None:
        return literal
    return await asyncio.to_thread(resolve_interface_ip, bind_interface)


def create_bound_udp_socket(
    remote_host: str,
    remote_port: int,
    *,
    bind_interface: str | None = None,
    local_ip: str | None = None,
) -> socket.socket:
    """Create a non-blocking connected UDP socket optionally pinned to `bind_interface` (`IoTWifi` split routing).

    On macOS uses `IP_BOUND_IF` (`25`); on Linux uses `SO_BINDTODEVICE` with `bind((local_ip, 0))` fallback
    so primary Home Wi-Fi / internet routing remains undisturbed.
    """
    resolved_ip = local_ip or (resolve_interface_ip(bind_interface) if bind_interface else None)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setblocking(False)
        # Skip kernel interface index lookup if the caller passed a raw IP string as bind_interface
        if bind_interface and _ipv4_literal(bind_interface) is None:
            if sys.platform == "darwin":
                try:
                    if_idx = socket.if_nametoindex(bind_interface)
                    sock.setsockopt(socket.IPPROTO_IP, DARWIN_IP_BOUND_IF, struct.pack("I", if_idx))
                except OSError as exc:
                    raise NebiDiscoveryError(
                        "INTERFACE_BIND_FAILED",
                        f"Failed to bind UDP socket to macOS interface '{bind_interface}': {exc}",
                        stage="ucp_handshaking",
                        recoverable=True,
                        hint="Verify the interface name in `ifconfig` or switch Interface binding to Auto.",
                    ) from exc
            elif sys.platform.startswith("linux"):
                so_bind = getattr(socket, "SO_BINDTODEVICE", 25)
                try:
                    sock.setsockopt(socket.SOL_SOCKET, so_bind, bind_interface.encode("utf-8") + b"\x00")
                except PermissionError:
                    # Non-root Linux fallback: source-IP bind below routes via subnet table
                    logger.debug("SO_BINDTODEVICE requires CAP_NET_RAW; falling back to source IP %s", resolved_ip)

        if resolved_ip:
            sock.bind((resolved_ip, 0))
        else:
            sock.bind(("0.0.0.0", 0))

        sock.connect((remote_host, int(remote_port)))
        return sock
    except Exception:
        sock.close()
        raise


async def wait_for_interface_subnet(
    prefix: str = "",
    *,
    exclude_prefixes: tuple[str, ...] = (),
    interface: str | None = None,
    timeout: float = 12.0,
) -> NetworkInterfaceInfo | None:
    """Wait until `interface` (or any non-loopback interface) obtains a DHCP IPv4 matching `prefix`."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        for iface in await list_network_interfaces_async(include_loopback=False):
            if interface and iface.name != interface:
                continue
            if exclude_prefixes and iface.ipv4.startswith(exclude_prefixes):
                continue
            if not prefix or iface.ipv4.startswith(prefix):
                return iface
        await asyncio.sleep(0.35)
    return None


