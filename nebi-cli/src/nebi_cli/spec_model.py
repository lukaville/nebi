"""Data model of the `nebi` command spec (`Arg`, `Cmd`) and the process exit codes. Pure stdlib."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

# Exit codes (also listed in `nebi guide` / `nebi schema`).
EXIT_OK = 0
EXIT_FAILED = 1  # command reached the robot but failed (robot error, RPC error, bad state)
EXIT_USAGE = 2  # bad arguments / unknown command
EXIT_UNAVAILABLE = 3  # daemon not running or robot not connected
EXIT_SAFETY = 4  # refused by a safety rule; re-run with --force only if it is really intended
EXIT_TIMEOUT = 5  # a wait condition / bounded action did not complete in time
EXIT_INTERRUPTED = 130  # the CLI was interrupted (Ctrl-C / SIGTERM)

EXIT_CODES: dict[int, str] = {
    EXIT_OK: "ok",
    EXIT_FAILED: "command failed (see error.code / error.hint)",
    EXIT_USAGE: "usage error (bad arguments or unknown command)",
    EXIT_UNAVAILABLE: "daemon not running or robot not connected (run `nebi up ...`)",
    EXIT_SAFETY: "refused by a safety rule (re-run with --force only if intended)",
    EXIT_TIMEOUT: "timed out (wait condition not met / action did not finish)",
}

ArgType = Literal["str", "int", "float", "bool", "distance", "angle", "duration", "json", "path", "text"]

# What a command physically does, for the safety precheck (safety_ops.SafetyGate):
#   body    = moves the body/wheels/legs: refused while carried (unless --ignore-carried)
#   stunt   = jump/flip: as body, and also refused when the carried state is unknown (no carried-state source)
#   gimbal  = camera tilt only: allowed while carried
#   express = voice/eyes/sound: never blocked by the carried check
#   rest    = sit: a safe resting pose, never blocked
MotionKind = Literal["", "body", "stunt", "gimbal", "express", "rest"]


@dataclass(frozen=True)
class Arg:
    name: str
    type: ArgType = "str"  # text = rest of argv, joined
    help: str = ""
    positional: bool = False
    required: bool = False
    default: Any = None
    choices: tuple[str, ...] | None = None
    flag: str = ""  # CLI spelling override for options (default: --name with _ -> -)
    short: str = ""

    @property
    def cli_name(self) -> str:
        return "--" + self.name.replace("_", "-")


@dataclass(frozen=True)
class Cmd:
    name: str
    group: str
    summary: str
    args: tuple[Arg, ...] = ()
    examples: tuple[str, ...] = ()
    notes: str = ""
    local: bool = False  # handled entirely in the CLI process (no daemon round-trip)
    moves_robot: bool = False  # counts as agent activity; the CLI sends `stop` if interrupted
    motion_kind: MotionKind = ""
    timeout_s: float = 30.0  # HTTP timeout for the daemon call (actions add their own bound)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["args"] = [{k: v for k, v in asdict(a).items() if v not in (None, "", False) or k == "positional"} for a in self.args]
        return d
