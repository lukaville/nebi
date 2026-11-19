"""`nebi`: Async Python SDK and robot simulator compatible with the Beni robot (LZ110/LZ100); the web cockpit is
`nebi-cockpit`, the CLI `nebi-cli`.

Independent, unofficial project: not affiliated with or endorsed by the robot's manufacturer. It physically moves
the robot; use it only with a robot you own, under supervision. Apache-2.0, provided AS IS (see LICENSE / NOTICE).

Public names are resolved lazily (PEP 562): `import nebi` stays cheap, so lightweight entry points such as the
`nebi` agent CLI do not pull in BLE, crypto, Zenoh or PyAV until a name that needs them is actually used.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    __version__: str  # resolved lazily from the installed distribution metadata (pyproject.toml is the single source)

_LAZY_EXPORTS: dict[str, str] = {
    "AsyncNebiClient": "nebi.client",
    "AudioOperation": "nebi.intercom",
    "NebiExtension": "nebi.extensions",
    "SpeechAudio": "nebi.tts",
    "TtsEngine": "nebi.tts",
    "register_tts_engine": "nebi.tts",
    "NEBI_BLE_CHAR_NOTIFY_UUID": "nebi.ble",
    "NEBI_BLE_CHAR_READ_UUID": "nebi.ble",
    "NEBI_BLE_CHAR_WRITE_UUID": "nebi.ble",
    "NEBI_BLE_SERVICE_UUID": "nebi.ble",
    "NebiBleClient": "nebi.ble",
    "NebiBleError": "nebi.exceptions",
    "NebiCatalog": "nebi.catalog.registry",
    "NebiDiscoveryError": "nebi.exceptions",
    "NebiError": "nebi.exceptions",
    "NebiHandshakeError": "nebi.exceptions",
    "NebiIdentityError": "nebi.exceptions",
    "NebiLinkLostError": "nebi.exceptions",
    "NebiNotConnectedError": "nebi.exceptions",
    "NebiRpcTimeoutError": "nebi.exceptions",
    "NebiWifiProvisionError": "nebi.exceptions",
    "BleFrame": "nebi.ble",
    "BlePeripheralLink": "nebi.ble",
    "DomainController": "nebi.domains._base",
    "register_flow": "nebi.flows",
    "topic_decoder": "nebi.telemetry",
    "BleRobotAdvertisement": "nebi.ble",
    "BleStreamParser": "nebi.ble",
    "CATALOG": "nebi.catalog.registry",
    "CameraCmdType": "nebi.types",
    "CameraProfileSpec": "nebi.catalog.registry",
    "CameraSubMode": "nebi.types",
    "ConnectionFlow": "nebi.types",
    "ConnectionStage": "nebi.types",
    "DEFAULT_IDENTITY_STORE": "nebi.md_ts",
    "DetectedTarget": "nebi.types",
    "DiscoveredRobot": "nebi.network",
    "GameAction": "nebi.types",
    "GameType": "nebi.types",
    "HmsAlert": "nebi.types",
    "INTERCOM_FRAME_SAMPLES": "nebi.intercom",
    "INTERCOM_SAMPLE_RATE": "nebi.intercom",
    "Intercom": "nebi.intercom",
    "JoystickState": "nebi.types",
    "LinkDiagnostics": "nebi.types",
    "MdTsClientIdentity": "nebi.md_ts",
    "MdTsIdentityStore": "nebi.md_ts",
    "MdTsServerAuthority": "nebi.sim.md_ts",
    "MdTsSession": "nebi.md_ts",
    "MissionId": "nebi.types",
    "MockBlePeripheral": "nebi.sim.ble",
    "NetworkInterfaceInfo": "nebi.network",
    "OpusDecoder": "nebi.intercom",
    "OpusEncoder": "nebi.intercom",
    "PetSubMode": "nebi.types",
    "PostureCmd": "nebi.types",
    "RobotPostureState": "nebi.types",
    "RobotTelemetrySnapshot": "nebi.types",
    "SoftApCredentials": "nebi.ble",
    "SpeedGear": "nebi.types",
    "StuntDirection": "nebi.types",
    "TopicSpec": "nebi.catalog.registry",
    "UwbRemoteState": "nebi.types",
    "WifiProvisionFailReason": "nebi.types",
    "crc8_autosar": "nebi.ble",
    "create_bound_udp_socket": "nebi.network",
    "discover_robots": "nebi.network",
    "encode_ble_frame": "nebi.ble",
    "get_current_wifi_ssid": "nebi.network",
    "list_network_interfaces": "nebi.network",
    "probe_nebi_endpoint": "nebi.network",
    "scan_lan_subnet_for_robots": "nebi.network",
}

__all__ = [
    "CATALOG",
    "DEFAULT_IDENTITY_STORE",
    "INTERCOM_FRAME_SAMPLES",
    "INTERCOM_SAMPLE_RATE",
    "NEBI_BLE_CHAR_NOTIFY_UUID",
    "NEBI_BLE_CHAR_READ_UUID",
    "NEBI_BLE_CHAR_WRITE_UUID",
    "NEBI_BLE_SERVICE_UUID",
    "AsyncNebiClient",
    "AudioOperation",
    "BleFrame",
    "BlePeripheralLink",
    "BleRobotAdvertisement",
    "BleStreamParser",
    "CameraCmdType",
    "CameraProfileSpec",
    "CameraSubMode",
    "ConnectionFlow",
    "ConnectionStage",
    "DetectedTarget",
    "DiscoveredRobot",
    "DomainController",
    "GameAction",
    "GameType",
    "HmsAlert",
    "Intercom",
    "JoystickState",
    "LinkDiagnostics",
    "MdTsClientIdentity",
    "MdTsIdentityStore",
    "MdTsServerAuthority",
    "MdTsSession",
    "MissionId",
    "MockBlePeripheral",
    "NebiBleClient",
    "NebiBleError",
    "NebiCatalog",
    "NebiDiscoveryError",
    "NebiError",
    "NebiExtension",
    "NebiHandshakeError",
    "NebiIdentityError",
    "NebiLinkLostError",
    "NebiNotConnectedError",
    "NebiRpcTimeoutError",
    "NebiWifiProvisionError",
    "NetworkInterfaceInfo",
    "OpusDecoder",
    "OpusEncoder",
    "PetSubMode",
    "PostureCmd",
    "RobotPostureState",
    "RobotTelemetrySnapshot",
    "SoftApCredentials",
    "SpeechAudio",
    "SpeedGear",
    "StuntDirection",
    "TopicSpec",
    "TtsEngine",
    "UwbRemoteState",
    "WifiProvisionFailReason",
    "__version__",
    "crc8_autosar",
    "create_bound_udp_socket",
    "discover_robots",
    "encode_ble_frame",
    "get_current_wifi_ssid",
    "list_network_interfaces",
    "probe_nebi_endpoint",
    "register_flow",
    "register_tts_engine",
    "scan_lan_subnet_for_robots",
    "topic_decoder",
]

if TYPE_CHECKING:  # static analysers see the real symbols
    from nebi.ble import (
        NEBI_BLE_CHAR_NOTIFY_UUID,
        NEBI_BLE_CHAR_READ_UUID,
        NEBI_BLE_CHAR_WRITE_UUID,
        NEBI_BLE_SERVICE_UUID,
        BleFrame,
        BlePeripheralLink,
        BleRobotAdvertisement,
        BleStreamParser,
        NebiBleClient,
        SoftApCredentials,
        crc8_autosar,
        encode_ble_frame,
    )
    from nebi.catalog.registry import (
        CATALOG,
        CameraProfileSpec,
        NebiCatalog,
        TopicSpec,
    )
    from nebi.client import (
        AsyncNebiClient,
    )
    from nebi.domains._base import DomainController
    from nebi.exceptions import (
        NebiBleError,
        NebiDiscoveryError,
        NebiError,
        NebiHandshakeError,
        NebiIdentityError,
        NebiLinkLostError,
        NebiNotConnectedError,
        NebiRpcTimeoutError,
        NebiWifiProvisionError,
    )
    from nebi.extensions import NebiExtension
    from nebi.flows import register_flow
    from nebi.intercom import (
        INTERCOM_FRAME_SAMPLES,
        INTERCOM_SAMPLE_RATE,
        AudioOperation,
        Intercom,
        OpusDecoder,
        OpusEncoder,
    )
    from nebi.md_ts import (
        DEFAULT_IDENTITY_STORE,
        MdTsClientIdentity,
        MdTsIdentityStore,
        MdTsSession,
    )
    from nebi.network import (
        DiscoveredRobot,
        NetworkInterfaceInfo,
        create_bound_udp_socket,
        discover_robots,
        get_current_wifi_ssid,
        list_network_interfaces,
        probe_nebi_endpoint,
        scan_lan_subnet_for_robots,
    )
    from nebi.sim.ble import MockBlePeripheral
    from nebi.sim.md_ts import MdTsServerAuthority
    from nebi.telemetry import topic_decoder
    from nebi.tts import SpeechAudio, TtsEngine, register_tts_engine
    from nebi.types import (
        CameraCmdType,
        CameraSubMode,
        ConnectionFlow,
        ConnectionStage,
        DetectedTarget,
        GameAction,
        GameType,
        HmsAlert,
        JoystickState,
        LinkDiagnostics,
        MissionId,
        PetSubMode,
        PostureCmd,
        RobotPostureState,
        RobotTelemetrySnapshot,
        SpeedGear,
        StuntDirection,
        UwbRemoteState,
        WifiProvisionFailReason,
    )


def _distribution_version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("nebi-lib")
    except PackageNotFoundError:  # running from a source tree that was never installed
        return "0+unknown"


def __getattr__(name: str) -> Any:
    if name == "__version__":
        globals()[name] = value = _distribution_version()
        return value
    module = _LAZY_EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module 'nebi' has no attribute {name!r}")
    value = getattr(importlib.import_module(module), name)
    globals()[name] = value  # cache: later lookups bypass __getattr__
    return value


def __dir__() -> list[str]:
    return __all__
