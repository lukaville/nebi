"""Camera capture, video recording, VT live streaming, zoom, EIS, horizon lock, pro exposure, and trajectory replay."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import time
from typing import TYPE_CHECKING, Any

from nebi.catalog.registry import CATALOG, CameraProfileSpec
from nebi.domains._base import DomainController, decode_fixed_str, int_fields
from nebi.exceptions import NebiError, NebiRpcTimeoutError
from nebi.types import CameraCmdType

if TYPE_CHECKING:
    from nebi.transport.zenoh_ucp import ZenohTopicBus

logger = logging.getLogger(__name__)

_RECORD_ACK_FIELDS = ("ret_code", "state", "request_id")
_CAMERA_STATUS_TOPIC = "/status/camera/status"

RECORD_ACTION_START = 1  # camera service record txn: (action - 1) <= 1 accepted, 1 = start
RECORD_ACTION_STOP = 2  # 2 = stop (legacy trigger path maps "not start" to 2 as well)
ZOOM_MIN_X = 1.0  # camera service accepts zoom_data 10..20 (tenths)
ZOOM_MAX_X = 2.0


def _request_id_ms(bits: int) -> int:
    """Millisecond wall-clock request id truncated to `bits` (what the app sends)."""
    return int(time.time() * 1000) & ((1 << bits) - 1)


class CameraController(DomainController):
    """High-level async controller for Nebi's camera service and video transmission."""

    def __init__(self, bus: ZenohTopicBus) -> None:
        super().__init__(bus)
        self._rec_state = False
        self._status_at = 0.0
        self._unsub_status = bus.subscribe_topic(_CAMERA_STATUS_TOPIC, self._on_camera_status)

    def rebind(self, bus: ZenohTopicBus) -> None:
        self._unsub_status()
        super().rebind(bus)
        self._status_at = 0.0
        self._unsub_status = bus.subscribe_topic(_CAMERA_STATUS_TOPIC, self._on_camera_status)

    @property
    def profiles(self) -> list[CameraProfileSpec]:
        return CATALOG.camera_profiles

    # --- Video Transmission ---

    async def query_stream_status(self, timeout: float = 2.0) -> dict[str, Any]:
        return await self._request_ints(
            "/cmd/vt/status_req",
            "/cmd/vt/status_rsp",
            ("ready", "target_fps", "target_kbps", "target_resolution"),
            timeout=timeout,
            version=1,
        )

    async def start_live_stream(self, mode: int = 1, timeout: float = 2.0) -> dict[str, Any]:
        res = await self._request_ints(
            "/cmd/vt/set_mode_req", "/cmd/vt/set_mode_rsp", ("ret_code",), timeout=timeout, mode=int(mode)
        )
        self.request_keyframe()
        return res

    async def stop_live_stream(self, timeout: float = 2.0) -> dict[str, Any]:
        return await self._request_ints(
            "/cmd/vt/set_mode_req", "/cmd/vt/set_mode_rsp", ("ret_code",), timeout=timeout, mode=0
        )

    async def reset_stream_connection(self, timeout: float = 2.0) -> dict[str, Any]:
        return await self._request_ints(
            "/cmd/vt/reset_conn_req", "/cmd/vt/reset_conn_rsp", ("ret_code",), timeout=timeout
        )

    def request_keyframe(self) -> int:
        """Request immediate H.265 IDR keyframe on UCP Channel 1."""
        return self._bus.publish("/cmd/vt/req_iframe", version=1)

    def set_stream_bitrate(self, kbps: int) -> int:
        return self._bus.publish("/cmd/vt/set_bitrate_req", bitrate=int(kbps))

    def set_stream_fps(self, fps: int) -> int:
        return self._bus.publish("/cmd/vt/set_fps_req", fps=int(fps))

    def set_stream_resolution(self, resolution: int) -> int:
        return self._bus.publish("/cmd/vt/set_resolution_req", resolution=int(resolution))

    # --- Unified Camera Commands & Recording (camera service on the A3 camera SoC) ---
    #
    # Firmware facts (LZ110 V26.10.28):
    # * `/cmd/camera/unified_cmd` must be sent WITHOUT `header` and with all 16 data fields present: the robot's
    #   bridge rejects a header and any missing field, and then drops the message silently. The
    #   transport strips the header (`HEADERLESS_TOPICS`) and `normalize_proto_for_videotrans` fills every field.
    # * The camera acts on ZOOM (zoom_data in tenths, 10..20), HIGHLIGHT (only while recording),
    #   EIS level, RECORD_PROFILE, RAW dump and legacy record/exposure bits. It has no still-photo handler at all.
    # * `/cmd/camera/record_req` actions: 1 = start, 2 = stop (anything else -> ret_code 1). Start/stop run on worker
    #   threads; the robot allows the reply up to 80 s, so the reply may come late or not within our wait. We
    #   therefore also confirm the transition from `/status/camera/status` (`rec_state` / `recording_state`).

    @property
    def photo_supported(self) -> bool:
        """False: this firmware's camera has no still-photo capture (see `take_photo`)."""
        return False

    def _on_camera_status(self, _topic: str, msg: Any, _seq: int) -> None:
        d = msg.data
        self._rec_state = bool(int(d.rec_state) == 1 or int(d.recording_state) == 1)
        self._status_at = time.monotonic()

    def _note_rec_state(self, recording: bool) -> None:
        """Record a transition the camera just acknowledged (status messages may lag behind the reply)."""
        self._rec_state = recording
        self._status_at = time.monotonic()

    def _status_recording(self, max_age_s: float = 5.0) -> bool | None:
        """Recording flag from the latest camera status, or None if no fresh status was received."""
        if self._status_at <= 0.0 or time.monotonic() - self._status_at > max_age_s:
            return None
        return self._rec_state

    def _publish_unified(self, cmd_type: int, **fields: Any) -> int:
        """Publish one `CameraUnifiedCmd` (headerless; unset fields are sent as explicit zeros by the transport)."""
        return self._bus.publish("/cmd/camera/unified_cmd", cmd_type=int(cmd_type), **fields)

    async def take_photo(self, raw: bool = False) -> int:
        """Still-photo capture is NOT available on this firmware: always raises `NebiError(CAMERA_PHOTO_UNSUPPORTED)`.

        The camera's `CameraUnifiedCmd` handling ignores the CAPTURE bit (only zoom, highlight, EIS,
        profile, RAW debug dump and legacy record/exposure) and the camera reports `cur_snap_width=0`. Grab a frame
        from the live video stream instead (cockpit: `GET /api/video/snapshot.jpg`) or record a short clip.
        """
        raise NebiError(
            "CAMERA_PHOTO_UNSUPPORTED",
            "Still-photo capture is not supported by this robot firmware (the camera has no photo handler).",
            stage="camera",
            recoverable=False,
            hint="Save a frame from the live video stream instead (cockpit: GET /api/video/snapshot.jpg), "
            "or record a short video with start_recording()/stop_recording().",
            details={"raw_requested": bool(raw)},
        )

    async def _record_txn(self, action: int, want_recording: bool, timeout: float) -> dict[str, Any]:
        """Send `record_req(action)` and return once the reply OR the camera status confirms the new state."""
        was_recording = self._status_recording()
        request_id = _request_id_ms(48)
        sent_at = time.monotonic()
        req = asyncio.ensure_future(
            self._bus.request(
                "/cmd/camera/record_req",
                "/cmd/camera/record_rsp",
                timeout=timeout,
                action=int(action),
                request_id=request_id,
            )
        )
        verb = "start" if want_recording else "stop"
        try:
            while True:
                if req.done():
                    exc = req.exception()
                    ack = int_fields(req.result().data, *_RECORD_ACK_FIELDS) if exc is None else {}
                    if exc is None and ack["request_id"] != request_id:
                        # A late reply to an earlier record transaction (e.g. the previous start's): it says
                        # nothing about this one, so ignore it and wait for the camera status instead.
                        logger.debug("Ignoring record_rsp for request_id=%s (sent %s)", ack["request_id"], request_id)
                    elif exc is None:
                        if ack["ret_code"] == 0:
                            self._note_rec_state(want_recording)
                            return {**ack, "recording": want_recording, "confirmed_by": "rsp"}
                        if self._status_recording() is want_recording:  # e.g. already stopped
                            return {**ack, "recording": want_recording, "confirmed_by": "status"}
                        raise NebiError(
                            f"RECORD_{verb.upper()}_REJECTED",
                            f"The camera rejected the recording {verb} request (ret_code={ack['ret_code']}).",
                            stage="camera",
                            hint="Check storage (SD card / internal space) and that no other recording "
                            "transition is in progress, then retry.",
                            details={**ack, "action": action},
                        )
                    if exc is not None and not isinstance(exc, NebiRpcTimeoutError):
                        raise exc
                if self._status_at > sent_at and self._rec_state is want_recording:
                    return {
                        "ret_code": 0,
                        "state": 1 if want_recording else 0,
                        "request_id": request_id,
                        "recording": want_recording,
                        "confirmed_by": "status",
                    }
                if time.monotonic() - sent_at > timeout:
                    break
                await asyncio.sleep(0.1)
        finally:
            if not req.done():
                req.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await req
        raise NebiError(
            "RECORD_STATE_UNCONFIRMED",
            f"Recording {verb} was sent but neither a reply nor the camera status confirmed it within {timeout:.1f}s.",
            stage="camera",
            hint="Watch telemetry `recording` (camera status rec_state); retry once if it did not change.",
            details={"action": action, "request_id": request_id, "was_recording": was_recording},
        )

    async def start_recording(self, timeout: float = 6.0) -> dict[str, Any]:
        """Start MP4 recording on robot storage (`record_req action=1`), confirmed by reply or camera status.

        Returns `{ret_code, state, request_id, recording: True, confirmed_by: "rsp"|"status"}`; raises
        `NebiError(RECORD_START_REJECTED | RECORD_STATE_UNCONFIRMED)` otherwise.
        """
        return await self._record_txn(RECORD_ACTION_START, True, timeout)

    async def stop_recording(self, timeout: float = 8.0) -> dict[str, Any]:
        """Stop MP4 recording (`record_req action=2`; action 0 is rejected by this firmware).

        The camera finalizes the file on a worker thread and often answers later than we wait (or not at all), so
        the stop is also confirmed from `/status/camera/status` going idle. Returns
        `{ret_code, state, request_id, recording: False, confirmed_by: "rsp"|"status"}`; raises
        `NebiError(RECORD_STOP_REJECTED | RECORD_STATE_UNCONFIRMED)` otherwise.
        """
        return await self._record_txn(RECORD_ACTION_STOP, False, timeout)

    async def mark_highlight(self) -> int:
        """Tag a highlight in the ACTIVE recording (`CameraCmdType.HIGHLIGHT_TAG`).

        The camera ignores highlights when not recording, so this raises `NebiError(CAMERA_NOT_RECORDING)` if the
        latest camera status says no recording is running.
        """
        if self._status_recording() is False:
            raise NebiError(
                "CAMERA_NOT_RECORDING",
                "Highlights can only be marked during a recording; the camera is not recording.",
                stage="camera",
                hint="Start a recording first (start_recording()), then mark highlights.",
            )
        return self._publish_unified(CameraCmdType.HIGHLIGHT_TAG, trigger_flags=1)

    async def set_zoom(self, zoom_x: float) -> int:
        """Set digital zoom, clamped to `ZOOM_MIN_X`..`ZOOM_MAX_X` (1.0x..2.0x) in 0.1 steps.

        Wire value: `zoom_data = round(zoom_x * 10)` (tenths; camera status reports the same unit, 10 = 1.0x). The
        camera ignores values outside 10..20.
        """
        z = float(zoom_x)
        if not math.isfinite(z):
            z = ZOOM_MIN_X
        zoom_data = max(round(ZOOM_MIN_X * 10), min(round(ZOOM_MAX_X * 10), round(z * 10.0)))
        return self._publish_unified(CameraCmdType.ZOOM, zoom_data=zoom_data)

    async def set_eis(self, enabled: bool, level: int = 1) -> int:
        """Set Electronic Image Stabilization level (`eis_level`: `0=off, 1=standard, 2=enhanced`).

        Note: the camera service keeps EIS always on and ignores `eis_enable`; only the level is applied.
        """
        return self._publish_unified(
            CameraCmdType.EIS,
            eis_enable=1 if enabled else 0,
            eis_level=int(level) if enabled else 0,
        )

    async def set_horizon_lock(self, enabled: bool) -> int:
        """Horizon lock is NOT implemented by this firmware's camera pipeline: raises `CAMERA_HORIZON_LOCK_UNSUPPORTED`."""
        raise NebiError(
            "CAMERA_HORIZON_LOCK_UNSUPPORTED",
            "Horizon lock is not supported by this robot firmware (the camera logs it as 'not yet supported').",
            stage="camera",
            recoverable=False,
            hint="EIS stays on automatically; use set_eis(level=...) to adjust stabilization.",
            details={"requested": bool(enabled)},
        )

    async def set_profile(self, profile_id: int) -> int:
        """Switch sensor/recording profile (ids in `nebi.catalog.camera_profiles`).

        The camera service rejects the switch while recording or while pre-record is enabled.
        """
        self._bus.publish("/cmd/camera/config_switch_cmd", profile_id=int(profile_id))
        return self._publish_unified(CameraCmdType.RECORD_PROFILE, record_profile_id=int(profile_id))

    async def set_pro_exposure(
        self,
        *,
        pro_mode: bool = True,
        shutter_ns: int = 0,
        iso: int = 0,
        wb_kelvin: int = 5500,
        ev_compensation: float = 0.0,
        timeout: float = 2.0,
    ) -> dict[str, Any]:
        """Configure manual Pro exposure controls via `CaptureControlsSetReq` (the camera's current API).

        The legacy `CameraUnifiedCmd` PRO_EXPOSURE bit is deliberately not sent: it applies index-based exposure
        fields, which would all be zero here.
        """
        rsp = await self._bus.request(
            "/cmd/camera/capture_controls_set_req",
            "/cmd/camera/capture_controls_set_rsp",
            timeout=timeout,
            update_mask=0x1F,
            is_pro_mode=bool(pro_mode),
            shutter_mode=1 if (pro_mode and shutter_ns > 0) else 0,
            shutter_ns=int(shutter_ns),
            iso_mode=1 if (pro_mode and iso > 0) else 0,
            iso=int(iso),
            wb_mode=1 if (pro_mode and wb_kelvin > 0) else 0,
            wb_kelvin=int(wb_kelvin),
            ev_compensation=float(ev_compensation),
        )
        return int_fields(rsp.data, "result", "applied_mask")

    async def set_pre_record(self, enabled: bool, duration_s: int = 15, timeout: float = 2.0) -> dict[str, Any]:
        return await self._request_ints(
            "/cmd/camera/pre_record_set_req",
            "/cmd/camera/pre_record_set_rsp",
            ("error_code",),
            timeout=timeout,
            enabled=1 if enabled else 0,
            duration_s=int(duration_s),
        )

    async def set_snapback(self, enabled: bool, manual_capture: bool = True) -> int:
        return self._bus.publish(
            "/cmd/snapback/capture_control",
            enabled=1 if enabled else 0,
            manual_capture_enabled=1 if manual_capture else 0,
            capture_version=1,
        )

    # --- Relative Trajectory Replay ---

    async def list_trajectory_replays(self, timeout: float = 2.0) -> list[dict[str, Any]]:
        rsp = await self._bus.request(
            "/cmd/camera/relative_trajectory_replay_catalog_query_req",
            "/cmd/camera/relative_trajectory_replay_catalog_query_rsp",
            timeout=timeout,
            version=1,
            client_request_id=_request_id_ms(32),
        )
        items: list[dict[str, Any]] = []
        for desc in rsp.data.descriptors:
            tid = decode_fixed_str(desc.id, desc.id_len)
            name = decode_fixed_str(desc.name, desc.name_len)
            items.append({"id": tid, "name": name, "sample_count": int(desc.sample_count)})
        return items

    async def play_trajectory_replay(self, trajectory_id: str) -> int:
        raw_id = trajectory_id.encode("utf-8")
        return self._bus.publish(
            "/cmd/camera/relative_trajectory_replay",
            version=1,
            action=1,
            trajectory_id_len=len(raw_id),
            trajectory_id=raw_id,
            client_request_id=_request_id_ms(32),
        )

    async def stop_trajectory_replay(self) -> int:
        return self._bus.publish(
            "/cmd/camera/relative_trajectory_replay",
            version=1,
            action=2,
            trajectory_id_len=0,
            trajectory_id=b"",
            client_request_id=_request_id_ms(32),
        )
