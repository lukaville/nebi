"""System diagnostics, Config `:8010` parameters, UWB remotes, HMS faults and local OTA.

`SystemController` is also the public home of the eye-LED (`LedController`, domains/led.py) and robot Wi-Fi
(`WifiController`, domains/wifi.py) APIs, which live in their own modules and are mixed in here so
`robot.system.set_led_rgbw()` / `robot.system.provision_home_wifi()` keep working.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nebi.catalog.registry import CATALOG, ParamSpec
from nebi.domains._base import decode_fixed_str, int_fields
from nebi.domains.led import LedController
from nebi.domains.wifi import WifiController
from nebi.exceptions import NebiError

if TYPE_CHECKING:
    from nebi.http.clients import ConfigHttpClient, OtaHttpClient
    from nebi.transport.zenoh_ucp import ZenohTopicBus
    from nebi.types import RobotTelemetrySnapshot

logger = logging.getLogger(__name__)


class SystemController(LedController, WifiController):
    """High-level async controller for Nebi system management, parameters, Wi-Fi, LEDs, UWB, HMS, and OTA."""

    def __init__(
        self,
        bus: ZenohTopicBus,
        config_http: ConfigHttpClient,
        ota_http: OtaHttpClient,
    ) -> None:
        super().__init__(bus)
        self._config_http = config_http
        self._ota_http = ota_http
        # Set by AsyncNebiClient: lets clear_hms_fault find the robot's pending HMS action requests.
        self.get_telemetry: Callable[[], RobotTelemetrySnapshot] | None = None

    def rebind(
        self,
        bus: ZenohTopicBus,
        config_http: ConfigHttpClient | None = None,
        ota_http: OtaHttpClient | None = None,
    ) -> None:
        super().rebind(bus)
        if config_http is not None:
            self._config_http = config_http
        if ota_http is not None:
            self._ota_http = ota_http

    @property
    def tunable_parameter_specs(self) -> list[ParamSpec]:
        return CATALOG.params

    # --- Ping, Version, Serial Number, Time Sync ---

    async def ping(self, payload: bytes = b"nebi", timeout: float = 2.0) -> dict[str, Any]:
        t0 = time.perf_counter()
        rsp = await self._bus.request(
            "/cmd/common/ping_req",
            "/cmd/common/ping_rsp",
            timeout=timeout,
            ping_data=payload,
        )
        rtt_ms = (time.perf_counter() - t0) * 1000.0
        return {
            "ret_code": int(rsp.data.ret_code),
            "echo": bytes(rsp.data.ping_data).decode("utf-8", errors="replace").rstrip("\x00"),
            "rtt_ms": round(rtt_ms, 2),
        }

    @staticmethod
    def is_direct_led_supported(hardware_version: str) -> bool:
        """Direct eye-LED frames (`/cmd/led/ctrl_oneway`) work on every known board, LZ110 P2 included."""
        del hardware_version
        return True

    @staticmethod
    def _decode_firmware_version(raw: bytes, major: int = 0, minor: int = 0) -> str:
        clean = bytes(raw).rstrip(b"\x00")
        if len(clean) == 4 and any(b < 0x20 or b > 0x7E for b in clean):
            return f"V{clean[0]}.{clean[1]:02d}.{clean[2]:02d}.{clean[3]:02d}"
        if len(raw) >= 4 and clean and any(b < 0x20 or b > 0x7E for b in clean):
            b0, b1, b2, b3 = raw[0], raw[1], raw[2], raw[3]
            return f"V{b0}.{b1:02d}.{b2:02d}.{b3:02d}"
        txt = clean.decode("utf-8", errors="replace").strip()
        if not txt and (major or minor):
            return f"v{major}.{minor}"
        return txt

    async def get_version(self, timeout: float = 2.0) -> dict[str, Any]:
        rsp = await self._bus.request("/cmd/common/get_version_req", "/cmd/common/get_version_rsp", timeout=timeout)
        hw = bytes(rsp.data.hardware_version).decode("utf-8", errors="replace").rstrip("\x00").strip()
        major = int(rsp.data.major_version)
        minor = int(rsp.data.minor_version)
        fw = self._decode_firmware_version(bytes(rsp.data.firmware_version), major=major, minor=minor)
        return {
            "ret_code": int(rsp.data.ret_code),
            "major": major,
            "minor": minor,
            "hardware_version": hw,
            "firmware_version": fw,
            "led_direct_supported": self.is_direct_led_supported(hw),
        }

    async def get_hardware_snapshot(self, timeout: float = 2.0) -> dict[str, Any]:
        """Query `/cmd/capability/hardware_snapshot_req` -> `/cmd/capability/hardware_snapshot_rsp`."""
        rsp = await self._bus.request(
            "/cmd/capability/hardware_snapshot_req",
            "/cmd/capability/hardware_snapshot_rsp",
            timeout=timeout,
        )
        return {
            "ret_code": int(rsp.data.ret_code),
            "has_camera_profiles": rsp.data.HasField("camera_record_profiles"),
            "has_manual_iso": rsp.data.HasField("camera_manual_iso"),
        }

    async def get_serial_number(self, sn_type: int = 0, timeout: float = 2.0) -> str:
        rsp = await self._bus.request(
            "/cmd/robot/sn_req",
            "/cmd/robot/sn_rsp",
            timeout=timeout,
            sn_type=int(sn_type),
        )
        return decode_fixed_str(rsp.data.sn, rsp.data.lens)

    async def sync_time(self, timestamp_ms: int | None = None, tz_offset_min: int = 0, timeout: float = 2.0) -> dict[str, Any]:
        ts = int(timestamp_ms if timestamp_ms is not None else time.time() * 1000)
        return await self._request_ints(
            "/cmd/time/correction_req",
            "/cmd/time/correction_rsp",
            ("ret_code",),
            timeout=timeout,
            timestamp=ts,
            offset=int(tz_offset_min) & 0xFFFFFFFF,
        )

    # --- Parameters (Config HTTP :8010 + Zenoh `/cmd/parameter/*` fallback when :8010 requires mTLS) ---

    async def get_parameters(self, keys: list[str] | None = None) -> dict[str, Any]:
        try:
            return await self._config_http.query(keys=keys)
        except Exception as exc:
            # Real LZ110 hardware gates :8010 behind mTLS; fall back to Zenoh `/cmd/parameter/query_req` + catalog defaults
            logger.debug("config :8010 query failed (%s); using Zenoh parameter queries", exc)
            vals: dict[str, Any] = {}
            defaults: list[str] = []  # keys whose value is the catalog default, not read from the robot
            wanted = set(keys) if keys else None
            for spec in CATALOG.params:
                jkey = spec.key
                if wanted is not None and jkey not in wanted:
                    continue
                vals[jkey] = spec.default
                live = False
                if spec.param_id is not None and self._bus.is_open:
                    try:
                        rsp = await self._bus.request(
                            "/cmd/parameter/query_req",
                            "/cmd/parameter/query_rsp",
                            timeout=0.5,
                            mode_type=1,
                            param_id=spec.param_id,
                        )
                        if int(rsp.data.ret_code) == 0:
                            vals[jkey] = round(float(rsp.data.param_value), 4)
                            live = True
                    except Exception as exc:  # unanswered param: report the catalog default (listed in "defaults")
                        logger.debug("parameter %s query failed: %s", jkey, exc)
                if not live:
                    defaults.append(jkey)
            return {"ok": len(defaults) < len(vals) or not vals, "values": vals, "source": "zenoh", "defaults": defaults}

    async def set_parameters(self, values: dict[str, Any]) -> dict[str, Any]:
        http_res: dict[str, Any] | None = None
        try:
            http_res = await self._config_http.commit(values)
        except Exception as exc:  # mTLS-gated on real hardware: the Zenoh path below is authoritative then
            logger.debug("config :8010 commit failed: %s", exc)
            http_res = None

        # Mirror / apply function parameters over live Zenoh `/cmd/parameter/update_req`
        applied: list[str] = []
        for key, val in values.items():
            spec = CATALOG.param(key)
            if spec is not None and spec.param_id is not None and self._bus.is_open:
                try:
                    rsp = await self._bus.request(
                        "/cmd/parameter/update_req",
                        "/cmd/parameter/update_rsp",
                        timeout=1.0,
                        mode_type=1,
                        param_id=spec.param_id,
                        param_value=float(val),
                    )
                    if int(rsp.data.ret_code) == 0:
                        applied.append(key)
                except Exception as exc:  # reported in "failed"
                    logger.debug("parameter %s update failed: %s", key, exc)
        if http_res is not None:
            return http_res
        failed = [k for k in values if k not in applied]
        return {"ok": bool(applied) and not failed, "committed": applied, "failed": failed,
                "values": {k: values[k] for k in applied}, "source": "zenoh"}

    # --- UWB Remotes & Haptics ---

    async def vibrate_remote(self, slot: int = 0, level: int = 2, timeout: float = 2.0) -> dict[str, Any]:
        rsp = await self._bus.request(
            "/cmd/rc/vibration_set_req",
            "/cmd/rc/vibration_set_rsp",
            timeout=timeout,
            level=int(level),
            receiver_type=1,
            receiver_index=int(slot),
        )
        return int_fields(rsp.data, "level")

    async def set_remote_button_mode(
        self,
        camera_btn_mode: int = 0,
        game_btn_mode: int = 0,
        timeout: float = 2.0,
    ) -> dict[str, Any]:
        return await self._request_ints(
            "/cmd/rc/function_key_mode",
            "/cmd/rc/function_key_mode_ack",
            ("ret_code",),
            timeout=timeout,
            camera_btn_mode=int(camera_btn_mode),
            game_btn_mode=int(game_btn_mode),
            update_flag=3,
        )

    # --- HMS Fault Recovery ---

    async def clear_hms_fault(
        self,
        fault_code: int | str,
        instance_id: int = 0,
        timeout: float = 2.0,
        *,
        pending: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Acknowledge an HMS fault the robot asked the app about.

        Direction matters: `/cmd/hms/action_request` flows robot -> app, and the app
        answers on `/cmd/hms/action_response` (headerless). So "clear" = answer the robot's pending action request
        for this fault (`pending`, from `telemetry.hms_pending_actions`) with result_code 0. Faults the robot never
        asked about cannot be cleared from the app: they recover by themselves when the condition goes away
        (`HMS_NO_PENDING_ACTION`). Not yet confirmed on hardware."""
        code_int = int(fault_code, 16) if isinstance(fault_code, str) else int(fault_code)
        code = f"0x{code_int & 0xFFFFFFFF:08X}"
        if pending is None and self.get_telemetry is not None:
            actions = self.get_telemetry().hms_pending_actions
            pending = actions.get(f"{code}:{int(instance_id)}") or next(
                (a for a in actions.values() if a.get("code") == code), None
            )
        if pending is None:
            raise NebiError(
                "HMS_NO_PENDING_ACTION",
                f"The robot has not asked the app to confirm fault {code} (instance {int(instance_id)}), so there is "
                "nothing to acknowledge.",
                stage="connected",
                recoverable=True,
                hint="This fault clears by itself once its cause is fixed (see its recovery hint); restart the robot "
                "if it persists.",
                details={"code": code, "instance": int(instance_id)},
            )
        seq = self._bus.publish(
            "/cmd/hms/action_response",
            command_id=int(pending["command_id"]),
            action_id=int(pending["action_id"]),
            result_code=0,
            dispatcher_instance_id=int(pending.get("dispatcher_instance_id", 0)),
            delivery_store_id=bytes.fromhex(str(pending.get("delivery_store_id", ""))),
        )
        return {"acknowledged": True, "code": code, "instance": int(instance_id),
                "command_id": int(pending["command_id"]), "result_code": 0, "seq": seq}

    async def query_active_faults(self, timeout: float = 2.0) -> dict[str, Any]:
        """Ask the robot's health monitor for the authoritative active fault set (`/status/hms/query_active` -> `active_snapshot`).

        The client's telemetry decoder applies the snapshot to `telemetry.hms_alerts` as it arrives; this returns the
        raw codes. Raises `NebiRpcTimeoutError` when the robot does not answer."""
        req_id = int(time.time() * 1000) & 0xFFFFFFFFFFFF
        rsp = await self._bus.request(
            "/status/hms/query_active",
            "/status/hms/active_snapshot",
            timeout=timeout,
            filter_kind=0,
            module_id=0,
            request_id=req_id,
        )
        d = rsp.data
        return {
            "count": int(d.count),
            "fault_codes": [f"0x{int(c) & 0xFFFFFFFF:08X}" for c in d.fault_codes],
            "instance_ids": [int(i) for i in d.instance_ids],
            "ret_code": int(d.ret_code),
        }

    # --- Local OTA Firmware Upgrade ---

    async def start_ota_upgrade(
        self,
        filename: str,
        content: bytes,
        timeout: float = 5.0,
    ) -> dict[str, Any]:
        """Upload firmware archive via the robot's firmware-upgrade HTTP service and trigger `/cmd/ota/upgrade_control_req`."""
        upload_res = await self._ota_http.upload_package(filename, content)
        rsp = await self._bus.request(
            "/cmd/ota/upgrade_control_req",
            "/cmd/ota/upgrade_control_rsp",
            timeout=timeout,
            control_cmd=1,
        )
        return {
            "upload": upload_res,
            "ret_code": int(rsp.data.ret_code),
            "stage": int(rsp.data.current_upgrade_stage),
        }

    async def start_ota_from_file(self, path: str | Path, timeout: float = 5.0) -> dict[str, Any]:
        p = Path(path)
        return await self.start_ota_upgrade(p.name, await asyncio.to_thread(p.read_bytes), timeout=timeout)

    async def factory_reset(self, timeout: float = 3.0) -> dict[str, Any]:
        return await self._request_ints(
            "/cmd/system/factory_reset_req", "/cmd/system/factory_reset_rsp", ("ret_code",), timeout=timeout
        )
