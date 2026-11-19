"""One-command onboarding: BLE pairing -> SoftAP -> commit Home Wi-Fi -> restore host Wi-Fi -> LAN client.

Backs `AsyncNebiClient.pair_ble_and_provision_home_wifi()`. Kept apart from the client because it orchestrates
several short-lived clients and switches the host's OS Wi-Fi, which is not session logic.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from nebi._callbacks import TaskSet, call_guarded
from nebi.ble import BlePeripheralLink, SoftApCredentials
from nebi.exceptions import NebiDiscoveryError, NebiError
from nebi.flows import (
    StageCallback,
    ble_fetch_softap,
    bootstrapped_into_simulator,
    discover_lan_host,
    redacted_credentials,
)
from nebi.md_ts import MdTsIdentityStore
from nebi.network import (
    find_interface_for_subnet_async,
    get_current_wifi_ssid_async,
    join_os_wifi,
    wait_for_interface_subnet,
)
from nebi.types import SOFTAP_HOST, SOFTAP_SUBNET, ConnectionFlow, ConnectionStage

if TYPE_CHECKING:
    from nebi.client import AsyncNebiClient
    from nebi.simulator import MockNebiRobot

logger = logging.getLogger(__name__)

_STAGE_TASKS = TaskSet("onboarding stage callback")
_LAN_DISCOVERY_ROUNDS = 10
# After the robot joins the home Wi-Fi its app link can take a while to come up. Keep retrying the LAN connection
# this long (re-discovering the IP between attempts): a single short attempt let the robot give up and roll back
# to its hotspot.
LAN_HANDOFF_WINDOW_S = 30.0
LAN_HANDOFF_ATTEMPT_S = 5.0


async def pair_ble_and_provision_home_wifi(
    client_cls: type[AsyncNebiClient],
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
    """See `AsyncNebiClient.pair_ble_and_provision_home_wifi()`."""

    def _emit(st: ConnectionStage, detail: str, err: NebiError | None = None) -> None:
        if on_stage is not None:
            call_guarded(on_stage, (st, detail, err), _STAGE_TASKS)

    def _attach(client: AsyncNebiClient) -> AsyncNebiClient:
        if on_stage is not None:
            client.add_stage_listener(on_stage)
        return client

    ble_mock = mock_ble or (simulator.ble_peripheral if simulator else None)
    previous_os_ssid = await get_current_wifi_ssid_async(wifi_interface) if ble_mock is None else None
    existing_ap_if = await find_interface_for_subnet_async(SOFTAP_SUBNET) if ble_mock is None else None

    # Step 1: BLE MD-TS pairing & SoftAP credential fetch
    creds, store, robot_pub = await ble_fetch_softap(
        ble_mock=ble_mock,
        identity_store=identity_store,
        security_mode=security_mode,
        ble_address=ble_address,
        connect_timeout=8.0,
        country_code=country_code,
        wait_for_button_sec=wait_for_button_sec,
        emit=_emit,
    )

    if bootstrapped_into_simulator(simulator, ble_mock, creds):
        assert simulator is not None  # narrowed by bootstrapped_into_simulator()
        return await _provision_via_simulator(
            client_cls,
            simulator,
            creds=creds,
            store=store,
            robot_pub=robot_pub,
            attach=_attach,
            ssid=ssid, password=password, security=security, security_mode=security_mode,
            connect_lan_after=connect_lan_after, timeout_sec=timeout_sec,
        )

    # Real hardware: SoftAP -> encrypted Zenoh provisioning -> restore host Home Wi-Fi -> LAN handoff
    switched_host_wifi = False
    prov_res: dict[str, Any] = {}
    try:
        ap_iface_name = existing_ap_if.name if existing_ap_if is not None else None
        if ap_iface_name is None:
            if not switch_os_wifi:
                raise NebiDiscoveryError(
                    "SOFTAP_NOT_JOINED",
                    f"Host is not connected to Nebi's SoftAP '{creds.ssid}' ({SOFTAP_HOST}).",
                    stage=ConnectionStage.JOINING_SOFTAP.value,
                    recoverable=True,
                    hint=f"Enable switch_os_wifi=True or join '{creds.ssid}' (password in the connection details) on a secondary Wi-Fi adapter.",
                    details={"softap_credentials": redacted_credentials(creds)},
                )
            _emit(
                ConnectionStage.JOINING_SOFTAP,
                f"Temporarily joining Nebi SoftAP '{creds.ssid}' to transmit Home Wi-Fi credentials...",
            )
            await join_os_wifi(creds.ssid, creds.password, interface=wifi_interface)
            switched_host_wifi = True
            ap_if = await wait_for_interface_subnet(SOFTAP_SUBNET, interface=wifi_interface, timeout=12.0)
            if ap_if is not None:
                ap_iface_name = ap_if.name

        ap_client = _attach(
            client_cls(
                host=creds.ap_ip or SOFTAP_HOST,
                flow=ConnectionFlow.BLE_BOOTSTRAP.value,
                mode_label=ConnectionFlow.BLE_BOOTSTRAP.value,
                bind_interface=ap_iface_name or wifi_interface,
                ble_address=creds.ble_address,
                auto_reconnect=False,
                identity_store=store,
                robot_public_key=robot_pub,
                security_mode=security_mode,
            )
        )
        ap_client.set_bootstrapped_credentials(creds, record_ssid=False)

        await ap_client.connect(timeout=6.0, start_joystick_stream=False, auto_start_video=False)
        try:
            _emit(ConnectionStage.WIFI_PROVISIONING, f"Committing Home Wi-Fi '{ssid}' to Nebi over encrypted MD-TS Zenoh...")
            prov_res = await ap_client.system.provision_home_wifi(
                ssid=ssid,
                password=password,
                security=security,
                wait_for_result=True,
                timeout_sec=timeout_sec,
            )
        finally:
            await ap_client.close()
    finally:
        if switched_host_wifi:
            await _restore_host_wifi(ssid, password, previous_os_ssid, wifi_interface, _emit)

    prov_res["ble_credentials"] = creds.to_dict()
    prov_res["softap_credentials"] = creds.to_dict()
    lan_ip = str(prov_res.get("ipv4") or "")

    if not lan_ip:  # discover Nebi's newly assigned Home Wi-Fi LAN IP
        _emit(ConnectionStage.DISCOVERING, f"Scanning Home Wi-Fi '{ssid}' LAN for Nebi's new DHCP address...")
        for _ in range(_LAN_DISCOVERY_ROUNDS):
            found = await discover_lan_host(1.8)
            if found:
                lan_ip = found
                prov_res["ipv4"] = lan_ip
                prov_res["status"] = "succeeded"
                break
            await asyncio.sleep(1.5)

    prov_res["lan_ip"] = lan_ip
    if connect_lan_after and lan_ip:
        lan_client = await _connect_lan_with_retries(
            lambda host: _attach(
                client_cls(
                    host=host,
                    flow=ConnectionFlow.LAN_STA.value,
                    mode_label=ConnectionFlow.LAN_STA.value,
                    ble_address=creds.ble_address,
                    identity_store=store,
                    robot_public_key=robot_pub,
                    security_mode=security_mode,
                )
            ),
            lan_ip,
            _emit,
        )
        prov_res["lan_ip"] = prov_res["ipv4"] = lan_client.host
        prov_res["handed_off"] = True
        prov_res["md_ts_active"] = bool(lan_client.bus.md_ts_active)
        return prov_res, lan_client

    prov_res["handed_off"] = False
    prov_res["md_ts_active"] = bool(creds.md_ts_established)
    return prov_res, None


async def _connect_lan_with_retries(
    make_client: Callable[[str], AsyncNebiClient],
    lan_ip: str,
    emit: Callable[[ConnectionStage, str], None],
    *,
    window_s: float = LAN_HANDOFF_WINDOW_S,
    attempt_s: float = LAN_HANDOFF_ATTEMPT_S,
) -> AsyncNebiClient:
    """Connect to the robot on the home LAN, retrying for `window_s` (fresh client each time, IP re-discovered)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + window_s
    host, attempt = lan_ip, 0
    while True:
        attempt += 1
        client = make_client(host)
        try:
            await client.connect(timeout=attempt_s)
            return client
        except NebiError as exc:
            await client.close()
            if loop.time() + 1.0 >= deadline:
                raise
            emit(ConnectionStage.DISCOVERING, f"Nebi not answering on {host} yet ({exc.code}); retrying (attempt {attempt + 1})...")
            await asyncio.sleep(1.0)
            host = await discover_lan_host(1.8) or host


async def _provision_via_simulator(
    client_cls: type[AsyncNebiClient],
    simulator: MockNebiRobot,
    *,
    creds: SoftApCredentials,
    store: MdTsIdentityStore,
    robot_pub: bytes | None,
    attach: Callable[[AsyncNebiClient], AsyncNebiClient],
    ssid: str,
    password: str,
    security: int,
    security_mode: str,
    connect_lan_after: bool,
    timeout_sec: int,
) -> tuple[dict[str, Any], AsyncNebiClient | None]:
    """Simulator path: in-process UCP + Zenoh, no OS Wi-Fi switching."""
    sim_client: AsyncNebiClient = attach(
        client_cls(
            **simulator.client_kwargs(),
            flow=ConnectionFlow.BLE_BOOTSTRAP.value,
            ble_address=creds.ble_address,
            identity_store=store,
            robot_public_key=robot_pub,
            security_mode=security_mode,
        )
    )
    sim_client.set_bootstrapped_credentials(creds, record_ssid=False)
    await sim_client.connect(timeout=4.0)
    try:
        prov = await sim_client.provision_home_wifi_and_handoff(
            ssid=ssid,
            password=password,
            security=security,
            switch_connection=connect_lan_after,
            timeout_sec=timeout_sec,
        )
        prov["ble_credentials"] = creds.to_dict()
        prov["softap_credentials"] = creds.to_dict()
        prov["lan_ip"] = str(prov.get("ipv4") or sim_client.host)
        prov["md_ts_active"] = bool(sim_client.bus.md_ts_active)
        if connect_lan_after:
            return prov, sim_client
        await sim_client.close()
        return prov, None
    except Exception:
        await sim_client.close()
        raise


async def _restore_host_wifi(
    ssid: str, password: str, previous_os_ssid: str | None, wifi_interface: str | None, emit: Callable[..., None]
) -> None:
    """Always put the host back on Home Wi-Fi after the temporary SoftAP join (best effort, logged)."""
    restore_ssid = ssid or previous_os_ssid or ""
    restore_pw = password if restore_ssid == ssid else ""
    if not restore_ssid:
        return
    emit(ConnectionStage.WIFI_PROVISIONING, f"Restoring host Wi-Fi interface back to '{restore_ssid}'...")
    try:
        await join_os_wifi(restore_ssid, restore_pw, interface=wifi_interface)
        await wait_for_interface_subnet(
            "",
            exclude_prefixes=(SOFTAP_SUBNET, "169.254."),
            interface=wifi_interface,
            timeout=15.0,
        )
    except Exception as exc:
        logger.warning("Failed to auto-restore host Wi-Fi to %s: %s", restore_ssid, exc)
