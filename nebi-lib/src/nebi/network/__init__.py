"""Host networking for the robot link.

  - `interfaces`  interface enumeration, name -> IP resolution, split-socket UDP binding (`IoTWifi` / USB-NCM)
  - `discovery`   multi-path robot discovery (simulator, USB-C NCM, SoftAP, Home LAN, BLE)
  - `wifi`        host OS Wi-Fi: current SSID (cached) and opt-in joining

Supported non-cloud paths: USB-C NCM (`192.168.123.1`), Home Wi-Fi station (LAN IP), direct SoftAP
(`192.168.7.1`, optionally pinned to a second interface), BLE GATT bootstrap, and the built-in simulator.
"""

from __future__ import annotations

from nebi.network.discovery import (
    DiscoveredRobot,
    SimulatorLike,
    discover_robots,
    probe_nebi_endpoint,
    scan_lan_subnet_for_robots,
)
from nebi.network.interfaces import (
    DARWIN_IP_BOUND_IF,
    NEBI_SOFTAP_PREFIX,
    NEBI_USB_NCM_PREFIX,
    NetworkInterfaceInfo,
    create_bound_udp_socket,
    find_interface_for_subnet,
    find_interface_for_subnet_async,
    list_network_interfaces,
    list_network_interfaces_async,
    resolve_interface_ip,
    resolve_interface_ip_async,
    wait_for_interface_subnet,
)
from nebi.network.wifi import (
    get_current_wifi_ssid,
    get_current_wifi_ssid_async,
    join_os_wifi,
)

__all__ = [
    "DARWIN_IP_BOUND_IF",
    "NEBI_SOFTAP_PREFIX",
    "NEBI_USB_NCM_PREFIX",
    "DiscoveredRobot",
    "NetworkInterfaceInfo",
    "SimulatorLike",
    "create_bound_udp_socket",
    "discover_robots",
    "find_interface_for_subnet",
    "find_interface_for_subnet_async",
    "get_current_wifi_ssid",
    "get_current_wifi_ssid_async",
    "join_os_wifi",
    "list_network_interfaces",
    "list_network_interfaces_async",
    "probe_nebi_endpoint",
    "resolve_interface_ip",
    "resolve_interface_ip_async",
    "scan_lan_subnet_for_robots",
    "wait_for_interface_subnet",
]
