"""Simulator / test infrastructure: robot-side protocol peers and the in-process `MockNebiRobot`.

Nothing in the production stack (transport, client, server) imports this package at import time; the old
locations (`nebi.simulator`, `nebi.ble.MockBlePeripheral`, `nebi.md_ts.MdTsServerAuthority`,
`nebi.transport.ucp.UcpServer`, `nebi.transport.zenoh_ucp.ZenohUcpServerTunnel`) re-export lazily.

Modules:
  - `sim.ble`      `MockBlePeripheral` (robot network-service GATT peer)
  - `sim.md_ts`    `MdTsServerAuthority` (robot identity key + authorizations table)
  - `sim.ucp`      `UcpServer` (robot app-bridge UCP peer)
  - `sim.zenoh`    `ZenohUcpServerTunnel` (robot-side Zenoh-over-UCP tunnel responder)
  - `sim.robot`    `MockNebiRobot` (wires the above together; physics/render/topics/http/telemetry submodules)
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nebi.sim.ble import MockBlePeripheral as MockBlePeripheral
    from nebi.sim.md_ts import MdTsServerAuthority as MdTsServerAuthority
    from nebi.sim.media import SimMediaAsset as SimMediaAsset
    from nebi.sim.robot import MockNebiRobot as MockNebiRobot
    from nebi.sim.ucp import UcpServer as UcpServer
    from nebi.sim.zenoh import ZenohUcpServerTunnel as ZenohUcpServerTunnel

# Lazy: `sim.robot` pulls in PyAV, Pillow, FastAPI and uvicorn.
_EXPORTS = {
    "MockBlePeripheral": "nebi.sim.ble",
    "MdTsServerAuthority": "nebi.sim.md_ts",
    "SimMediaAsset": "nebi.sim.media",
    "MockNebiRobot": "nebi.sim.robot",
    "UcpServer": "nebi.sim.ucp",
    "ZenohUcpServerTunnel": "nebi.sim.zenoh",
}

__all__ = [
    "MdTsServerAuthority",
    "MockBlePeripheral",
    "MockNebiRobot",
    "SimMediaAsset",
    "UcpServer",
    "ZenohUcpServerTunnel",
]


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(import_module(module), name)
