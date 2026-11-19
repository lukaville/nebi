"""Robot Wi-Fi: scan, station/AP switching and the robot's transactional Home Wi-Fi provisioning (`/cmd/nm/*`)."""

from __future__ import annotations

import asyncio
import logging
import uuid as _uuid
from typing import Any

from nebi.domains._base import DomainController, decode_fixed_str, decode_ipv4_field, int_fields
from nebi.exceptions import NebiWifiProvisionError
from nebi.types import SOFTAP_HOST, WifiProvisionFailReason

logger = logging.getLogger(__name__)

_PROVISION_STATE_SUCCEEDED = 2  # wifi_provision_result state: 0=NONE, 1=RUNNING, 2=SUCCEEDED, 3=FAILED
_PROVISION_STATE_FAILED = 3


class WifiController(DomainController):
    """Robot-side Wi-Fi management. The client's `provision_home_wifi_and_handoff()` builds on this."""

    _decode_ipv4_field = staticmethod(decode_ipv4_field)  # pre-split API alias

    async def scan_wifi(self, force: bool = True, max_count: int = 20, timeout: float = 4.0) -> list[dict[str, Any]]:
        rsp = await self._bus.request(
            "/cmd/nm/wifi_scan_req",
            "/cmd/nm/wifi_scan_rsp",
            timeout=timeout,
            force_scan=1 if force else 0,
            max_count=int(max_count),
        )
        networks: list[dict[str, Any]] = []
        for ap in rsp.data.aps:
            networks.append({"ssid": decode_fixed_str(ap.ssid, ap.ssid_len), **int_fields(ap, "channel", "rssi", "security")})
        return networks

    async def connect_wifi(  # noqa: PLR0917 - public positional signature kept for compatibility
        self,
        ssid: str,
        password: str = "",
        security: int = 2,
        timeout_sec: int = 15,
        provision_uuid: bytes | None = None,
        timeout: float = 4.0,
    ) -> dict[str, Any]:
        ssid_b = ssid.encode("utf-8")
        psk_b = password.encode("utf-8")
        kwargs: dict[str, Any] = {
            "ssid_len": len(ssid_b),
            "ssid": ssid_b,
            "psk_len": len(psk_b),
            "psk": psk_b,
            "security": int(security if password else 0),
            "timeout_sec": int(timeout_sec),
            "uuid": bytes(provision_uuid) if provision_uuid is not None else b"\x00" * 16,
            "resv": b"\x00" * 4,
        }
        rsp = await self._bus.request(
            "/cmd/nm/wifi_connect_req",
            "/cmd/nm/wifi_connect_rsp",
            timeout=timeout,
            **kwargs,
        )
        return {"result": int(rsp.data.result), "ssid": ssid}

    async def switch_wifi_to_station(self, watchdog_sec: int = 30, timeout: float = 3.0) -> dict[str, Any]:
        rsp = await self._bus.request(
            "/cmd/nm/wifi_switch_to_station_req",
            "/cmd/nm/wifi_switch_to_station_rsp",
            timeout=timeout,
            trigger_source=1,
            watchdog_sec=int(watchdog_sec),
        )
        return int_fields(rsp.data, "result", "current_mode")

    async def get_switch_to_station_result(self, timeout: float = 2.0) -> dict[str, Any]:
        return await self._request_ints(
            "/cmd/nm/wifi_switch_to_station_result_req",
            "/cmd/nm/wifi_switch_to_station_result_rsp",
            ("valid", "pending", "result", "current_mode"),
            timeout=timeout,
        )

    async def switch_wifi_to_ap(self) -> dict[str, Any]:
        """Switch Nebi from Home Wi-Fi `STA_CONNECTED` back to `192.168.7.1` SoftAP (`/cmd/nm/wifi_switch_to_ap_req`)."""
        seq = self._bus.publish("/cmd/nm/wifi_switch_to_ap_req", trigger_source=1)
        return {"ok": True, "seq": seq, "target_ap_ip": SOFTAP_HOST}

    async def get_wifi_provision_result(self, provision_uuid: bytes, timeout: float = 2.0) -> dict[str, Any]:
        rsp = await self._bus.request(
            "/cmd/nm/wifi_provision_result_req",
            "/cmd/nm/wifi_provision_result_rsp",
            timeout=timeout,
            uuid=bytes(provision_uuid),
        )
        d = rsp.data
        ssid = decode_fixed_str(d.ssid, d.ssid_len)
        fail_code = int(d.fail_reason)
        try:
            fail_name = WifiProvisionFailReason(fail_code).name
        except ValueError:
            fail_name = f"REASON_{fail_code}"
        return {
            "result": int(d.result),
            "uuid_match": int(d.uuid_match),
            "state": int(d.state),  # 0=NONE, 1=RUNNING, 2=SUCCEEDED, 3=FAILED
            "fail_reason": fail_code,
            "fail_reason_name": fail_name,
            "ssid": ssid,
            "ipv4": decode_ipv4_field(bytes(d.ipv4)),
            "channel": int(d.channel),
            "rssi": int(d.rssi),
        }

    async def provision_home_wifi(
        self,
        ssid: str,
        password: str = "",
        *,
        security: int = 2,
        wait_for_result: bool = True,
        timeout_sec: int = 25,
    ) -> dict[str, Any]:
        """Execute the robot network service's 3-stage transactional Home Wi-Fi provisioning (`AP -> STA`).

        Stages:
          1. `/cmd/nm/wifi_connect_req` (with 16-byte `uuid` -> stored `PENDING` in OP-TEE without dropping AP)
          2. `/cmd/nm/wifi_provision_start_req` (`trigger_source=1`, `uuid`)
          3. `/cmd/nm/wifi_provision_commit_req` (`trigger_source=1`, `uuid` -> starts `nm_switch_watchdog` & `wlan0` STA)
          4. Poll `/cmd/nm/wifi_provision_result_req` / listen `/status/nm/net_online_notify` (note: on real single-chip
             AIC8800 robots where `wlan1` SoftAP stops during cutover, RPC loss is expected and treated as `cutover_in_progress`).
        """
        tx_uuid = _uuid.uuid4().bytes
        online_fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()

        def _on_net_online(_topic: str, msg: Any, _seq: int) -> None:
            d = getattr(msg, "data", None)
            if d is None or online_fut.done():
                return
            if any(d.uuid) and bytes(d.uuid) != tx_uuid:
                return
            online_fut.set_result(
                {
                    "state": _PROVISION_STATE_SUCCEEDED,
                    "ssid": decode_fixed_str(d.ssid, d.ssid_len),
                    "ipv4": decode_ipv4_field(bytes(d.ipv4)),
                    "channel": int(d.channel),
                    "rssi": int(d.rssi),
                    "uuid_hex": tx_uuid.hex(),
                }
            )

        unsub = self._bus.subscribe_topic("/status/nm/net_online_notify", _on_net_online)
        try:
            try:
                conn_rsp = await self.connect_wifi(
                    ssid=ssid,
                    password=password,
                    security=security,
                    timeout_sec=timeout_sec,
                    provision_uuid=tx_uuid,
                    timeout=8.0,
                )
            except TimeoutError as exc:
                raise NebiWifiProvisionError(
                    "WIFI_PROVISION_RPC_TIMEOUT",
                    f"Timed out staging Wi-Fi credentials for '{ssid}' (/cmd/nm/wifi_connect_req -> /cmd/nm/wifi_connect_rsp).",
                    stage="wifi_provisioning",
                    recoverable=True,
                    hint="Ensure Nebi's SoftAP link is stable and MD-TS Zenoh encryption is active, then retry.",
                ) from exc
            if conn_rsp["result"] != 0:
                raise NebiWifiProvisionError(
                    "WIFI_PROVISION_STAGE_REJECTED",
                    f"Robot rejected Wi-Fi credentials staging for '{ssid}' (result={conn_rsp['result']}).",
                    stage="wifi_provisioning",
                    recoverable=True,
                    hint="Check that SSID is 1-32 bytes and WPA2 password is 8-63 characters (or empty for Open networks).",
                )

            try:
                start_rsp = await self._bus.request(
                    "/cmd/nm/wifi_provision_start_req",
                    "/cmd/nm/wifi_provision_start_rsp",
                    timeout=6.0,
                    trigger_source=1,
                    resv=b"\x00" * 7,
                    uuid=tx_uuid,
                    mqtt_endpoint_len=0,
                    mqtt_endpoint=b"",
                    mqtt_resv=b"\x00" * 7,
                )
            except TimeoutError as exc:
                raise NebiWifiProvisionError(
                    "WIFI_PROVISION_START_TIMEOUT",
                    "Timed out starting Wi-Fi provisioning transaction (/cmd/nm/wifi_provision_start_req).",
                    stage="wifi_provisioning",
                    recoverable=True,
                ) from exc
            if int(start_rsp.data.result) != 0:
                raise NebiWifiProvisionError(
                    "WIFI_PROVISION_START_FAILED",
                    f"wifi_provision_start_req failed (result={int(start_rsp.data.result)}).",
                    stage="wifi_provisioning",
                    recoverable=True,
                )

            try:
                commit_rsp = await self._bus.request(
                    "/cmd/nm/wifi_provision_commit_req",
                    "/cmd/nm/wifi_provision_commit_rsp",
                    timeout=6.0,
                    trigger_source=1,
                    resv=b"\x00" * 7,
                    uuid=tx_uuid,
                )
            except TimeoutError:
                # Expected on single-radio AIC8800 firmware: commit may drop the wlan1 SoftAP right as it responds
                commit_rsp = None
            if commit_rsp is not None and int(commit_rsp.data.result) != 0:
                raise NebiWifiProvisionError(
                    "WIFI_PROVISION_COMMIT_FAILED",
                    f"wifi_provision_commit_req failed (result={int(commit_rsp.data.result)}).",
                    stage="wifi_provisioning",
                    recoverable=True,
                )

            if not wait_for_result:
                return {
                    "ok": True,
                    "status": "committed",
                    "ssid": ssid,
                    "uuid_hex": tx_uuid.hex(),
                }

            deadline = asyncio.get_running_loop().time() + min(12.0, float(timeout_sec))
            while asyncio.get_running_loop().time() < deadline:
                if online_fut.done():
                    info = online_fut.result()
                    return {"ok": True, "status": "succeeded", **info}
                try:
                    res = await self.get_wifi_provision_result(tx_uuid, timeout=1.0)
                except Exception as exc:
                    # Expected on physical hardware right as the wlan1 AP tears down for wlan0 STA association
                    logger.debug("provision result poll lost (cutover): %s", exc)
                    return {
                        "ok": True,
                        "status": "cutover_in_progress",
                        "ssid": ssid,
                        "uuid_hex": tx_uuid.hex(),
                        "ipv4": "",
                    }

                if res["state"] == _PROVISION_STATE_SUCCEEDED:
                    return {
                        "ok": True,
                        "status": "succeeded",
                        "ssid": res["ssid"] or ssid,
                        "ipv4": res["ipv4"],
                        "channel": res["channel"],
                        "rssi": res["rssi"],
                        "uuid_hex": tx_uuid.hex(),
                    }
                if res["state"] == _PROVISION_STATE_FAILED:  # watchdog rolled back to AP
                    reason = res["fail_reason_name"]
                    code = "WIFI_PROVISION_WRONG_KEY" if reason == "WRONG_KEY" else "WIFI_PROVISION_FAILED"
                    hint_map = {
                        "WRONG_KEY": f"Incorrect Wi-Fi password for '{ssid}'. Nebi's watchdog automatically restored its SoftAP (192.168.7.1) — re-enter the password and try again.",
                        "NETWORK_NOT_FOUND": f"SSID '{ssid}' was not found by Nebi's radio. Move closer to your router or check 2.4GHz/5GHz band settings.",
                        "DHCP_TIMEOUT": f"Connected to '{ssid}' but router DHCP did not assign an IPv4 address. Robot rolled back to SoftAP.",
                    }
                    raise NebiWifiProvisionError(
                        code,
                        f"Home Wi-Fi provisioning to '{ssid}' failed ({reason}); watchdog rolled robot back to SoftAP.",
                        stage="wifi_provisioning",
                        recoverable=True,
                        hint=hint_map.get(reason, "Nebi rolled back to SoftAP (192.168.7.1) safely; verify router settings and retry."),
                        details=res,
                    )
                await asyncio.sleep(0.25)

            return {
                "ok": True,
                "status": "cutover_in_progress",
                "ssid": ssid,
                "uuid_hex": tx_uuid.hex(),
                "ipv4": "",
            }
        finally:
            unsub()
