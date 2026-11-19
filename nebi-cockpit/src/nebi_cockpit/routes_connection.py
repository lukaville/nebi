"""Status, discovery, BLE bootstrap, connection switching and simulator QA routes."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter

from nebi.md_ts import DEFAULT_IDENTITY_STORE
from nebi.network import discover_robots, list_network_interfaces
from nebi_cockpit.deps import ManagerDep, SimulatorDep
from nebi_cockpit.schemas import (
    BleBootstrapBody,
    BleProvisionHomeWifiBody,
    ConnectionSwitchBody,
    DiscoveryScanBody,
    SimLinkDropBody,
    SimPairingButtonBody,
)

router = APIRouter(prefix="/api")


async def _interfaces() -> list[dict[str, Any]]:
    # `ifconfig` subprocess: keep it off the event loop
    found = await asyncio.to_thread(list_network_interfaces, include_loopback=False)
    return [i.to_dict() for i in found]


@router.get("/health", tags=["status"])
async def api_health(manager: ManagerDep) -> dict[str, Any]:
    conn = manager.connection_summary()
    return {
        "ok": True,
        "connected": bool(manager.client and manager.client.connected),
        "stage": conn["stage"],
        "flow": conn["flow"],
    }


@router.get("/status", tags=["status"])
async def api_status(manager: ManagerDep) -> dict[str, Any]:
    return manager.status_dict()


@router.get("/network/interfaces", tags=["discovery"])
async def api_network_interfaces() -> dict[str, Any]:
    return {"interfaces": await _interfaces()}


@router.post("/discovery/scan", tags=["discovery"])
async def api_discovery_scan(body: DiscoveryScanBody, manager: ManagerDep) -> dict[str, Any]:
    found = await discover_robots(
        include_ble=body.include_ble,
        include_lan=body.include_lan,
        deep_lan_scan=body.deep_lan_scan,
        simulator=manager.simulator,
        timeout=body.timeout,
    )
    return {
        "robots": [x.to_dict() for x in found],
        "interfaces": await _interfaces(),
        "current_wifi_ssid": await manager.wifi_ssid.refresh_now(),
        "md_ts_identity": DEFAULT_IDENTITY_STORE.summary_dict(),
    }


@router.post("/ble/bootstrap", tags=["connection"])
async def api_ble_bootstrap(body: BleBootstrapBody, manager: ManagerDep) -> dict[str, Any]:
    return await manager.ble_bootstrap(body)


@router.post("/ble/provision_home_wifi", tags=["connection"])
async def api_ble_provision_home_wifi(body: BleProvisionHomeWifiBody, manager: ManagerDep) -> dict[str, Any]:
    return await manager.provision_home_wifi_via_ble(body)


@router.post("/connection", tags=["connection"])
async def api_switch_connection(body: ConnectionSwitchBody, manager: ManagerDep) -> dict[str, Any]:
    return await manager.switch_connection(body)


@router.post("/connection/disconnect", tags=["connection"])
async def api_disconnect_connection(manager: ManagerDep) -> dict[str, Any]:
    return await manager.disconnect()


@router.post("/connection/retry", tags=["connection"])
async def api_retry_connection(manager: ManagerDep) -> dict[str, Any]:
    return await manager.switch_connection(manager.last_conn_request)


# --- Simulator QA / hardware-flow emulation ---


@router.post("/sim/pairing_button", tags=["simulator"])
async def api_sim_pairing_button(body: SimPairingButtonBody, manager: ManagerDep) -> dict[str, Any]:
    sim = await manager.ensure_simulator_started()
    if body.reset:
        sim.reset_ble_auth(require_button=body.require_button)
    else:
        sim.press_pairing_button()
    return {
        "ok": True,
        "authorized": sim.ble_peripheral.authorized,
        "require_button_auth": sim.ble_peripheral.require_button_auth,
    }


@router.post("/sim/link_drop", tags=["simulator"])
async def api_sim_link_drop(body: SimLinkDropBody, sim: SimulatorDep) -> dict[str, Any]:
    sim.simulate_link_drop(body.duration_sec)
    return {"ok": True, "duration_sec": body.duration_sec}
