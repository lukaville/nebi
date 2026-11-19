"""Interactive robot games (`MissionId.GAME`): Motion-Sense, Tug-of-War, Camera Battle, Balance, Treasure Hunt."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from nebi.domains._base import DomainController
from nebi.domains._mission import request_mission, require_accepted
from nebi.types import GameAction, GameType, MissionId

if TYPE_CHECKING:
    from nebi.transport.zenoh_ucp import ZenohTopicBus

_GAME_INPUT_PERIOD_US = 20_000  # 50 Hz player-input stream, like the app


class GamesController(DomainController):
    """High-level async controller for Nebi's 5 interactive robot games."""

    def __init__(self, bus: ZenohTopicBus) -> None:
        super().__init__(bus)
        self._input_seq = 0
        self._active_session_id = 1
        self._active_game = int(GameType.NONE)

    async def _control(
        self,
        game_type: GameType | int,
        action: GameAction | int,
        timeout: float = 2.5,
    ) -> dict[str, Any]:
        if int(action) == int(GameAction.START):
            require_accepted(
                await request_mission(self._bus, int(MissionId.GAME), int(game_type), timeout=timeout), "starting the game"
            )
            self._active_session_id += 1
            self._active_game = int(game_type)
        return await self._request_ints(
            "/cmd/game/control_req",
            "/cmd/game/control_rsp",
            ("ret_code", "game_type", "action", "session_id"),
            timeout=timeout,
            timestamp_ns=int(time.time_ns()),
            version=1,
            game_type=int(game_type),
            action=int(action),
            flags=0,
            session_id=self._active_session_id,
            param_hash=0,
        )

    async def start_game(self, game_type: GameType | int, timeout: float = 2.5) -> dict[str, Any]:
        return await self._control(game_type, GameAction.START, timeout=timeout)

    async def pause_game(self, timeout: float = 2.0) -> dict[str, Any]:
        return await self._control(self._active_game or GameType.MOTION_SENSE, GameAction.PAUSE, timeout=timeout)

    async def resume_game(self, timeout: float = 2.0) -> dict[str, Any]:
        return await self._control(self._active_game or GameType.MOTION_SENSE, GameAction.RESUME, timeout=timeout)

    async def stop_game(self, timeout: float = 2.0) -> dict[str, Any]:
        res = await self._control(self._active_game or GameType.MOTION_SENSE, GameAction.STOP, timeout=timeout)
        self._active_game = int(GameType.NONE)
        return res

    async def restart_game(self, timeout: float = 2.0) -> dict[str, Any]:
        return await self._control(self._active_game or GameType.MOTION_SENSE, GameAction.RESTART, timeout=timeout)

    def send_player_input(
        self,
        *,
        pitch_deg: float = 0.0,
        roll_deg: float = 0.0,
        yaw_deg: float = 0.0,
        jump: bool = False,
        action_pressed: bool = False,
        player_slot: int = 0,
        game_type: int | None = None,
    ) -> int:
        """Stream IMU tilt / motion-sense player input (`GamePlayerInput`)."""
        self._input_seq = (self._input_seq + 1) & 0xFFFFFFFF
        flags = (1 if jump else 0) | (2 if action_pressed else 0)
        return self._bus.publish(
            "/cmd/game/player_input",
            timestamp_ns=int(time.time_ns()),
            version=1,
            game_type=int(game_type if game_type is not None else (self._active_game or 1)),
            player_slot=int(player_slot),
            source=1,
            flags=flags,
            profile_id=0,
            session_id=self._active_session_id,
            seq=self._input_seq,
            sample_period_us=_GAME_INPUT_PERIOD_US,
            acc_g=[0.0, 0.0, 1.0],
            gyro_dps=[0.0, 0.0, 0.0],
            attitude_deg=[float(pitch_deg), float(roll_deg), float(yaw_deg)],
        )

    async def get_game_menu(self, timeout: float = 2.0) -> dict[str, Any]:
        rsp = await self._bus.request(
            "/cmd/game/menu_get_req",
            "/cmd/game/menu_get_rsp",
            timeout=timeout,
            version=1,
        )
        return {
            "ret_code": int(rsp.data.ret_code),
            "effective_slots": list(bytes(rsp.data.effective_slots)),
        }
