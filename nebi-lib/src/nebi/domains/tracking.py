"""AI vision target selection, click-to-track, follow sub-modes, model switching, and PNL localization."""

from __future__ import annotations

import struct
from typing import Any

from nebi.domains._base import DomainController
from nebi.domains._mission import request_mission
from nebi.types import CameraSubMode, MissionId

_CTRL_CMD = ("/cmd/tracking/ctrl_cmd", "/cmd/tracking/ctrl_cmd_ack")
_CTRL_ACK_FIELDS = ("result", "selected_id", "start_tracking")


class TrackingController(DomainController):
    """High-level async controller for Nebi's AI target tracking & follow autonomy."""

    async def _ctrl_cmd(
        self, *, selected_id: int, start_tracking: int, x: float, y: float, request_mode: int, timeout: float
    ) -> dict[str, Any]:
        return await self._request_ints(
            *_CTRL_CMD,
            _CTRL_ACK_FIELDS,
            timeout=timeout,
            selected_id=selected_id,
            start_tracking=start_tracking,
            selected_pos_x=x,
            selected_pos_y=y,
            request_mode=request_mode,
        )

    async def lock_target(self, target_id: int, timeout: float = 2.0) -> dict[str, Any]:
        """Lock onto a detected `DetectedTarget.target_id` from `/data/tracking/target_data`."""
        return await self._ctrl_cmd(
            selected_id=int(target_id), start_tracking=1, x=0.0, y=0.0, request_mode=1, timeout=timeout
        )

    async def lock_point(self, norm_x: float, norm_y: float, timeout: float = 2.0) -> dict[str, Any]:
        """Click-to-track normalized `[0.0 .. 1.0]` point in FPV video viewport."""
        x = float(max(0.0, min(1.0, norm_x)))
        y = float(max(0.0, min(1.0, norm_y)))
        return await self._ctrl_cmd(selected_id=0, start_tracking=1, x=x, y=y, request_mode=2, timeout=timeout)

    async def unlock_target(self, timeout: float = 2.0) -> dict[str, Any]:
        """Stop active target tracking."""
        return await self._ctrl_cmd(selected_id=0, start_tracking=0, x=0.0, y=0.0, request_mode=0, timeout=timeout)

    async def set_follow_mode(
        self,
        sub_mode: CameraSubMode | int = CameraSubMode.FOLLOW_TRAIL,
        distance_m: float | None = None,
        orbit_speed: float | None = None,
        *,
        parallel: bool = False,
        angle_deg: int = 0,
        orbit_direction: int = 0,
        routing: int = 0,
        timeout: float = 2.0,
    ) -> dict[str, Any]:
        """Switch follow/spotlight sub-mode and optional 9-byte Follow `mode_params`.

        Per official SDK spec for Follow (`mission_id=1, sub_mode=2`):
        `mode_params[0]=0 (trailing) | 1 (parallel)`, `[1..4]=f32 distance_m`, `[5..6]=i16 angle_deg`,
        `[7]=orbit_direction`, `[8]=routing`.
        Note: `/cmd/tracking/param_update_req` `param_id=1` is reference human height (0.1–3.0 m) and `param_id=2`
        is the UWB follow switch (0.0/1.0) — use `set_reference_height()` / `set_uwb_follow()` for those.
        `orbit_speed` is accepted for backward compatibility and ignored (not a tracking parameter).
        """
        del orbit_speed
        mode_params = b""
        if distance_m is not None or parallel or angle_deg or orbit_direction or routing:
            style = 1 if (parallel or int(sub_mode) == int(CameraSubMode.FOLLOW_PARALLEL)) else 0
            dist = float(distance_m) if distance_m is not None else 2.0
            mode_params = struct.pack(
                "<BfhBB",
                style,
                dist,
                int(angle_deg),
                int(orbit_direction) & 0xFF,
                int(routing) & 0xFF,
            )
        return await request_mission(
            self._bus, int(MissionId.CAMERA), int(sub_mode), mode_params=mode_params, timeout=timeout
        )

    async def set_reference_height(self, height_m: float, timeout: float = 2.0) -> dict[str, Any]:
        """Set reference human height for visual tracking (`param_id=1`, clamped to 0.1–3.0 m)."""
        return await self.update_tracking_param(param_id=1, value=max(0.1, min(3.0, float(height_m))), timeout=timeout)

    async def set_uwb_follow(self, enabled: bool, timeout: float = 2.0) -> dict[str, Any]:
        """Enable or disable UWB remote-controller positioning in Follow (`param_id=2`, 0.0 or 1.0)."""
        return await self.update_tracking_param(param_id=2, value=1.0 if enabled else 0.0, timeout=timeout)

    async def switch_target_type(self, target_type: int, timeout: float = 2.0) -> dict[str, Any]:
        """Switch detector target type (`TrackingStatus.target_type`: `0=person`, `1=dog`)."""
        return await self._request_ints(
            "/cmd/tracking/model_switch_cmd",
            "/cmd/tracking/model_switch_cmd_ack",
            ("ret_code", "current_target_type"),
            timeout=timeout,
            target_type=int(target_type),
        )

    async def switch_algorithm(self, model_version: int, timeout: float = 2.0) -> dict[str, Any]:
        return await self._request_ints(
            "/cmd/tracking/algo_switch_cmd",
            "/cmd/tracking/algo_switch_cmd_ack",
            ("ret_code", "current_model_version"),
            timeout=timeout,
            model_version=int(model_version),
        )

    async def update_tracking_param(self, param_id: int, value: float, timeout: float = 2.0) -> dict[str, Any]:
        return await self._request_ints(
            "/cmd/tracking/param_update_req",
            "/cmd/tracking/param_update_rsp",
            ("ret_code",),
            timeout=timeout,
            param_id=int(param_id),
            param_value=float(value),
        )

    # Modes of the robot's positioning service
    PNL_LOCALIZATION = 0     # VIO + (if enabled) relocalization against the saved map
    PNL_ONLINE_CALIBRATION = 1   # rewrites the IMU-camera calibration -- never send casually
    PNL_MAPPING = 2          # VIO reset, build a pose graph; leaving the mode saves the map
    PNL_STEREO_CHECK = 3     # stereo extrinsic check -- never send casually
    PNL_SELF_CALIBRATION = 4

    async def set_pnl_mode(self, pnl_mode: int, timeout: float = 3.0, *, allow_calibration: bool = False) -> dict[str, Any]:
        """Switch the positioning-service mode (0=LOCALIZATION, 2=MAPPING; 1/3/4 are calibration modes and are refused
        unless allow_calibration=True). Every switch resets the VIO: do it with the robot still.
        result_code: 0 ok, 1 invalid mode, 3 relocalization not enabled, 4 busy."""
        if int(pnl_mode) not in (self.PNL_LOCALIZATION, self.PNL_MAPPING) and not allow_calibration:
            raise ValueError(f"pnl_mode {pnl_mode} is a calibration mode; refusing (pass allow_calibration=True)")
        rsp = await self._bus.request(
            "/cmd/pnl/mode_ctrl_req",
            "/cmd/pnl/mode_ctrl_rsp",
            timeout=timeout,
            pnl_mode=int(pnl_mode),
        )
        return {
            "result_code": int(rsp.data.result_code),
            "previous_pnl_mode": int(getattr(rsp.data, "previous_pnl_mode", -1)),
            "current_pnl_mode": int(rsp.data.current_pnl_mode),
        }

    async def get_pnl_mode(self, timeout: float = 3.0) -> dict[str, Any]:
        """Read-only positioning-service mode query."""
        return await self._request_ints(
            "/cmd/pnl/mode_query_req", "/cmd/pnl/mode_query_rsp", ("result_code", "current_pnl_mode"), timeout=timeout
        )
