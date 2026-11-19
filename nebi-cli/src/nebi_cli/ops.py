"""Backwards-compatible import surface of the former monolithic ops module.

The daemon now lives in runtime.py (composition root) with one module per command area; import from there.
"""

from __future__ import annotations

from nebi_cli.conditions import _eval_clause, eval_clause, parse_condition
from nebi_cli.errors import CACHE_DIR, STANDING, AgentError
from nebi_cli.events import EventLog
from nebi_cli.express_ops import COLOR_NAMES, EMOTES, EYE_EXPRESSIONS, SOUND_SLUGS
from nebi_cli.locomotion_ops import FOLLOW_STYLES, GEAR_BY_NAME, GEAR_SCALE, MAX_DRIVE_M, MAX_DRIVE_S, MAX_MOVE_S
from nebi_cli.motion import DEFAULT_MAX_SPEED_MPS, DEFAULT_MAX_YAW_DPS
from nebi_cli.runtime import AgentRuntime, agent_error_from_nebi
from nebi_cli.views import compact_state

__all__ = [
    "CACHE_DIR",
    "COLOR_NAMES",
    "DEFAULT_MAX_SPEED_MPS",
    "DEFAULT_MAX_YAW_DPS",
    "EMOTES",
    "EYE_EXPRESSIONS",
    "FOLLOW_STYLES",
    "GEAR_BY_NAME",
    "GEAR_SCALE",
    "MAX_DRIVE_M",
    "MAX_DRIVE_S",
    "MAX_MOVE_S",
    "SOUND_SLUGS",
    "STANDING",
    "AgentError",
    "AgentRuntime",
    "EventLog",
    "_eval_clause",
    "agent_error_from_nebi",
    "compact_state",
    "eval_clause",
    "parse_condition",
]
