"""Structured agent errors: `AgentError`, the error-code catalogue, and small shared argument helpers.

Import-light (stdlib + spec only) so every module, the CLI included, can import it without cycles.
"""

from __future__ import annotations

from typing import Any

from nebi_cli.settings import CACHE_DIR
from nebi_cli.spec_model import (
    EXIT_FAILED,
    EXIT_INTERRUPTED,
    EXIT_OK,
    EXIT_SAFETY,
    EXIT_TIMEOUT,
    EXIT_UNAVAILABLE,
    EXIT_USAGE,
)

__all__ = [
    "CACHE_DIR",
    "ERROR_CODES",
    "EXIT_FAILED",
    "EXIT_OK",
    "EXIT_SAFETY",
    "EXIT_TIMEOUT",
    "EXIT_UNAVAILABLE",
    "EXIT_USAGE",
    "STANDING",
    "AgentError",
    "bad_arg",
    "parse_xy",
    "register_error_codes",
]

# Postures in which the robot is up on its wheels and can move.
STANDING = frozenset({"BALANCING", "STANDING_UP"})

# Every `error.code` the agent produces -> (default exit code, meaning). A raise site may pass a different
# exit_code where the same code means different things (e.g. DRIVE_INCOMPLETE: timeout vs link lost).
# Codes of nebi.exceptions.NebiError (robot/transport) are passed through by runtime.agent_error_from_nebi.
ERROR_CODES: dict[str, tuple[int, str]] = {
    # arguments / dispatch
    "BAD_ARGUMENT": (EXIT_USAGE, "an argument is missing or malformed"),
    "BAD_CONDITION": (EXIT_USAGE, "`wait` condition cannot be parsed"),
    "UNKNOWN_COMMAND": (EXIT_USAGE, "no such command"),
    "UNKNOWN_FIELD": (EXIT_USAGE, "`wait` condition names an unknown state field"),
    "LED_BAD_COLOR": (EXIT_USAGE, "eye colour channel out of 0..255 / not a hex colour (`eyes`)"),
    "LED_BAD_HOLD": (EXIT_USAGE, "eye `--hold` out of range (`eyes`)"),
    "LED_BAD_LOOPS": (EXIT_USAGE, "eye expression `--repeat` out of range (`eyes`)"),
    "LED_BAD_BRIGHTNESS": (EXIT_USAGE, "eye `--brightness` not within 0..1 (`eyes`)"),
    "LED_UNKNOWN_EXPRESSION": (EXIT_USAGE, "no such eye expression (`nebi eyes list`)"),
    "EGO_MODE_NEEDS_STANDING": (EXIT_FAILED, "pet/ego mode only starts while the robot stands still"),
    "UNKNOWN_SOUND": (EXIT_USAGE, "no such sound"),
    "UNKNOWN_TOPIC": (EXIT_USAGE, "no such protocol topic"),
    "MISSING_EXTENT": (EXIT_USAGE, "`drive` needs a distance or a duration"),
    "NO_TARGET": (EXIT_FAILED, "nothing to connect to / follow"),
    "INTERNAL_ERROR": (EXIT_FAILED, "daemon-side bug or unexpected payload, not the caller's arguments"),
    "TIMEOUT": (EXIT_TIMEOUT, "an operation timed out"),
    # connection / services
    "NOT_CONNECTED": (EXIT_UNAVAILABLE, "robot not connected"),
    "VOICE_UNAVAILABLE": (EXIT_FAILED, "no TTS engine could render and no sound-effect fallback was possible"),
    "FACES_UNAVAILABLE": (EXIT_FAILED, "OpenCV not installed"),
    "FACE_MODELS_UNAVAILABLE": (EXIT_FAILED, "face models cannot be fetched/loaded"),
    "STT_FAILED": (EXIT_FAILED, "speech model failed to load"),
    # safety
    "BEING_CARRIED": (EXIT_SAFETY, "robot is being carried (only --ignore-carried overrides)"),
    "CARRIED_UNKNOWN": (EXIT_SAFETY, "stunt refused: carried state unknown (no carried-state source)"),
    "CONFIRM_REQUIRED": (EXIT_SAFETY, "risky action needs --force"),
    "OBSTACLE_AHEAD": (EXIT_SAFETY, "obstacle closer than --clearance"),
    "ROBOT_CHARGING_LOCKED": (EXIT_SAFETY, "locomotion locked while charging"),
    # robot state / results
    "NOT_STANDING": (EXIT_FAILED, "robot must stand first"),
    "DRIVE_INCOMPLETE": (EXIT_TIMEOUT, "drive stopped before the distance (timeout or link lost)"),
    "WAIT_TIMEOUT": (EXIT_TIMEOUT, "wait condition / event did not happen in time"),
    "NO_VIDEO": (EXIT_FAILED, "no video frames yet"),
    "BAD_FRAME": (EXIT_FAILED, "camera frame cannot be decoded"),
    "NO_AUDIO": (EXIT_FAILED, "no microphone audio"),
    "NO_FACE": (EXIT_FAILED, "no face visible"),
    "TARGET_NOT_FOUND": (EXIT_FAILED, "look-at target not found"),
    # CLI side (cli.py)
    "USAGE": (EXIT_USAGE, "argv could not be parsed"),
    "NOT_STARTED": (EXIT_UNAVAILABLE, "daemon not running and no saved target"),
    "DAEMON_START_FAILED": (EXIT_UNAVAILABLE, "daemon did not come up"),
    "DAEMON_UNREACHABLE": (EXIT_UNAVAILABLE, "cannot reach the daemon"),
    "DAEMON_HTTP_ERROR": (EXIT_FAILED, "daemon answered with an HTTP error"),
    "CONNECT_FAILED": (EXIT_UNAVAILABLE, "auto-start could not connect"),
    "CLI_TIMEOUT": (EXIT_TIMEOUT, "no reply from the daemon in time"),
    "INTERRUPTED": (EXIT_INTERRUPTED, "CLI interrupted; robot was sent `stop`"),
    "NOT_ALLOWED_IN_RUN": (EXIT_USAGE, "command cannot be used inside `nebi run`"),
    "STEP_FAILED": (EXIT_FAILED, "a `nebi run` step failed"),
}


def register_error_codes(codes: dict[str, tuple[int, str]]) -> None:
    """Add an extension's error codes (code -> (default exit code, meaning)). Core codes cannot be redefined."""
    for code, entry in codes.items():
        if code in ERROR_CODES and ERROR_CODES[code] != entry:
            raise ValueError(f"error code {code!r} is already defined as {ERROR_CODES[code]!r}")
    ERROR_CODES.update(codes)


class AgentError(Exception):
    """Structured failure returned to the CLI as `{"ok": false, "error": {...}}`.

    `exit_code` defaults to the code's entry in ERROR_CODES (EXIT_FAILED for codes not listed there).
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        hint: str = "",
        exit_code: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.exit_code = exit_code if exit_code is not None else ERROR_CODES.get(code, (EXIT_FAILED, ""))[0]
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"code": self.code, "message": self.message, "hint": self.hint}
        if self.details:
            d["details"] = self.details
        return d


def bad_arg(message: str, hint: str = "") -> AgentError:
    return AgentError("BAD_ARGUMENT", message, hint=hint, exit_code=EXIT_USAGE)


def parse_xy(raw: Any, *, what: str = "point", unit_range: bool = False) -> tuple[float, float]:
    """'x,y' -> (x, y). unit_range=True also requires both within 0..1 (normalized image point)."""
    try:
        xs, ys = str(raw).replace(" ", "").split(",", 1)
        x, y = float(xs), float(ys)
    except ValueError:
        raise bad_arg(f"{what} must look like 'x,y' (got {raw!r}).") from None
    if unit_range and not (0 <= x <= 1 and 0 <= y <= 1):
        raise bad_arg(f"{what} must be normalized x,y within 0..1 (got {raw!r}).")
    return x, y
