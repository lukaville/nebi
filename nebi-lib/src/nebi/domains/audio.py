"""Speaker sound effects, audio file playback, voice session, BLE wireless mics, and intercom shims.

The full-duplex intercom lives in `nebi.intercom.Intercom` (`AsyncNebiClient.intercom`).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from nebi.domains._base import DomainController, proto_fields

if TYPE_CHECKING:
    from nebi.intercom import Intercom
    from nebi.transport.ucp import UcpEndpointSession
    from nebi.transport.zenoh_ucp import ZenohTopicBus

# WAV assets that ship on the robot under `/md/etc/` (LZ110).
# Note: `/cmd/speaker/play_cmd` (`SpeakerPlayCmd`) only supports legacy speaker horn IDs `0` (`trumpet.wav`)
# and `1` (`trumpet1.wav`), whereas rich character/game sound effects are played via `/cmd/audio/play_file_req`.
SOUND_EFFECT_CATALOG: list[dict[str, Any]] = [
    {"id": 1, "name": "Happy Greet", "audio_path": "/md/etc/motion_audio_resource/hello_sway_happy_chirp1.wav"},
    {"id": 2, "name": "Cute Meow", "audio_path": "/md/etc/motion_audio_resource/cat_meow_01.wav"},
    {"id": 3, "name": "Excited Chirp", "audio_path": "/md/etc/motion_audio_resource/excited_jump_chirp.wav"},
    {"id": 4, "name": "Shutter Chime", "audio_path": "/md/etc/game_audio_resource/camera_battle/shutter_file.wav"},
    {"id": 5, "name": "Victory Fanfare", "audio_path": "/md/etc/game_audio_resource/tug/tug_settlement_big.wav"},
    {"id": 6, "name": "Curious Chirp", "audio_path": "/md/etc/motion_audio_resource/confused_huh_query.wav"},
    {"id": 7, "name": "Cozy Purr", "audio_path": "/md/etc/motion_audio_resource/gaze_hold_cat_purr_111e.wav"},
    {"id": 8, "name": "Find-Me Horn", "speaker_id": 1},
]

_SOUND_EFFECT_BY_ID: dict[int, dict[str, Any]] = {item["id"]: item for item in SOUND_EFFECT_CATALOG}


class AudioController(DomainController):
    """High-level async controller for Nebi audio, intercom, voice assistant, and wireless microphones."""

    def __init__(
        self,
        bus: ZenohTopicBus,
        ucp_getter: Callable[[], UcpEndpointSession | None],
        intercom: Intercom | None = None,
    ) -> None:
        super().__init__(bus)
        self._get_ucp = ucp_getter
        self._session_counter = 1
        self.intercom: Intercom | None = intercom  # the client's persistent Intercom (full-duplex audio)

    def _next_session_id(self) -> int:
        self._session_counter += 1
        return self._session_counter

    def send_intercom_opus(self, opus_frame: bytes) -> None:
        """Send one 48 kHz mono 20 ms Opus frame to the robot speaker.

        App -> robot intercom audio travels on Zenoh `/data/audio/playback_data`,
        not UCP Channel 2 (which is robot -> app only). Delegates to `Intercom.send_opus()`.
        """
        if self.intercom is not None:
            self.intercom.send_opus(opus_frame)

    async def set_record_audio_enabled(self, enabled: bool, timeout: float = 2.0) -> dict[str, Any]:
        """Deprecated alias: start (`operation_type=0`) / stop (`1`) the robot-mic intercom uplink.

        The robot maps `0 = START_RECORD`, `1 = STOP_RECORD`; this does not affect camera AAC recording.
        Prefer `AsyncNebiClient.intercom.start()` / `.stop()`.
        """
        ic = self.intercom
        if ic is not None:  # keep the Intercom's state in sync instead of toggling the mic behind its back
            res = await (ic.start(timeout) if enabled else ic.stop(timeout))
            return {"ret_code": int(res.get("ret_code", 0))}
        return await self._request_ints(
            "/cmd/audio/op_req", "/cmd/audio/op_rsp", ("ret_code",), timeout=timeout, operation_type=0 if enabled else 1
        )

    async def play_sound_effect(self, sound_effect_id: int, timeout: float = 2.0) -> dict[str, Any]:
        """Play built-in robot character/game sound effect via `/cmd/audio/play_file_req` or legacy speaker horn."""
        entry = _SOUND_EFFECT_BY_ID.get(int(sound_effect_id))
        if entry is not None and "audio_path" in entry:
            res = await self.play_audio_file(entry["audio_path"], play_action=1, timeout=timeout)
            return {
                "ret_code": int(res["return_code"]),
                "session_id": int(res["session_id"]),
                "audio_path": entry["audio_path"],
            }
        spk_id = int(entry["speaker_id"]) if entry is not None and "speaker_id" in entry else int(sound_effect_id)
        return await self._request_ints(
            "/cmd/speaker/play_cmd", "/cmd/speaker/play_rsp", ("ret_code",), timeout=timeout, sound_effect_id=spk_id
        )

    async def play_audio_file(self, audio_path: str, play_action: int = 1, timeout: float = 2.0) -> dict[str, Any]:
        """Play or stop (`play_action=0`) an audio file on the robot (`/md/media/sfx/...`)."""
        return await self._request_ints(
            "/cmd/audio/play_file_req",
            "/cmd/audio/play_file_rsp",
            ("return_code", "session_id"),
            timeout=timeout,
            session_id=self._next_session_id(),
            play_action=int(play_action),
            audio_path=audio_path.encode("utf-8"),
        )

    async def set_voice_wakeup(self, enabled: bool) -> int:
        """Enable or pause offline wake-word session (`AudioVoiceSessionControl`)."""
        sid = self._next_session_id()
        return self._bus.publish(
            "/cmd/audio/voice_session_control",
            session_id=sid,
            control_seq=sid,
            state=1 if enabled else 0,
            deadline_ns=int(time.time_ns()),
        )

    # --- Speaker volume buses (audio service parameter center, `/cmd/common/{get,set}_parameter_req`) ---

    async def get_parameter(self, category: int, parameter_id: int, timeout: float = 2.0) -> dict[str, Any]:
        """Raw `CommonGetParameterReq` (answered by the audio service for audio parameters)."""
        rsp = await self._bus.request(
            "/cmd/common/get_parameter_req",
            "/cmd/common/get_parameter_rsp",
            timeout=timeout,
            category=int(category),
            parameter_id=int(parameter_id),
        )
        d = rsp.data
        raw = bytes(d.value)[: int(d.len)] if d.HasField("len") else bytes(d.value)
        return {
            "return_code": int(d.return_code),
            "category": int(d.category),
            "parameter_id": int(d.parameter_id),
            "type": int(d.type),
            "len": int(d.len),
            "value_hex": raw.hex(),
            "value_int": int.from_bytes(raw, "little", signed=True) if raw else None,
        }

    async def set_parameter(
        self, category: int, parameter_id: int, value: bytes, type_: int, timeout: float = 2.0
    ) -> dict[str, Any]:
        rsp = await self._bus.request(
            "/cmd/common/set_parameter_req",
            "/cmd/common/set_parameter_rsp",
            timeout=timeout,
            category=int(category),
            parameter_id=int(parameter_id),
            type=int(type_),
            len=len(value),
            value=value,
        )
        return {k: (v if not isinstance(v, bytes) else v.hex()) for k, v in proto_fields(rsp.data).items()}

    # --- Wireless BLE Microphones (`/cmd/mic/*`) ---

    async def scan_wireless_mics(self, start: bool = True, duration_s: int = 8, timeout: float = 2.0) -> dict[str, Any]:
        return await self._request_ints(
            "/cmd/mic/scan_req",
            "/cmd/mic/scan_rsp",
            ("ret_code",),
            timeout=timeout,
            action=1 if start else 0,
            duration_s=int(duration_s),
        )

    async def select_mic_source(self, kind: int, mac: str = "", timeout: float = 2.0) -> dict[str, Any]:
        """Select audio input (`kind`: `0=built-in array`, `1=wireless BLE mic`)."""
        return await self._request_ints(
            "/cmd/mic/source_select_req",
            "/cmd/mic/source_select_rsp",
            ("ret_code",),
            timeout=timeout,
            kind=int(kind),
            mac=mac.encode("utf-8"),
        )

    async def forget_wireless_mic(self, mac: str, timeout: float = 2.0) -> dict[str, Any]:
        return await self._request_ints(
            "/cmd/mic/forget_req",
            "/cmd/mic/forget_rsp",
            ("ret_code", "removed_count"),
            timeout=timeout,
            macs=mac.encode("utf-8"),
        )
