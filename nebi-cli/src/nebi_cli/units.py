"""Unit parsers (distance / duration / angle) and spec-driven argument coercion. Pure stdlib.

Shared by the CLI (argv converters) and the daemon (validation of JSON args), so both accept the same input.
"""

from __future__ import annotations

import math
import re
from typing import Any

from nebi_cli.spec_model import Arg, Cmd

_NUM = r"([-+]?\d*\.?\d+)"

_DISTANCE_UNITS = {"m": 1.0, "cm": 0.01, "mm": 0.001, "ft": 0.3048, "in": 0.0254}
_DURATION_UNITS = {"ms": 0.001, "s": 1.0, "sec": 1.0, "m": 60.0, "min": 60.0, "h": 3600.0}
_NAMED_ANGLES = {"quarter": 90.0, "half": 180.0, "full": 360.0}


def parse_distance(text: str | float) -> float:
    """'1m' / '50cm' / '300mm' / '2ft' / '0.4' (meters) -> meters."""
    if isinstance(text, (int, float)):
        return float(text)
    m = re.fullmatch(_NUM + r"\s*(m|cm|mm|ft|in)?", str(text).strip().lower())
    if not m:
        raise ValueError(f"not a distance: {text!r} (examples: 1m, 50cm, 0.3)")
    return float(m.group(1)) * _DISTANCE_UNITS[m.group(2) or "m"]


def parse_duration(text: str | float) -> float:
    """'2s' / '500ms' / '1.5m' / '1h' / '3' (seconds) -> seconds."""
    if isinstance(text, (int, float)):
        return float(text)
    m = re.fullmatch(_NUM + r"\s*(ms|s|sec|m|min|h)?", str(text).strip().lower())
    if not m:
        raise ValueError(f"not a duration: {text!r} (examples: 2s, 500ms, 1m)")
    return float(m.group(1)) * _DURATION_UNITS[m.group(2) or "s"]


def parse_angle(text: str | float) -> float:
    """'90' / '90deg' / '1.57rad' / 'quarter' / 'half' -> degrees."""
    if isinstance(text, (int, float)):
        return float(text)
    t = str(text).strip().lower()
    if t in _NAMED_ANGLES:
        return _NAMED_ANGLES[t]
    m = re.fullmatch(_NUM + r"\s*(deg|°|rad)?", t)
    if not m:
        raise ValueError(f"not an angle: {text!r} (examples: 90, 45deg, quarter)")
    val = float(m.group(1))
    return math.degrees(val) if m.group(2) == "rad" else val


_TRUE = {"1", "true", "yes", "on", "y"}
_FALSE = {"0", "false", "no", "off", "n", ""}


def _to_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    sv = str(v).strip().lower()
    if sv in _TRUE:
        return True
    if sv in _FALSE:
        return False
    raise ValueError(f"not a boolean: {v!r}")


def _to_int(v: Any) -> int:
    if isinstance(v, bool):
        raise ValueError(f"not an integer: {v!r}")
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        if not v.is_integer():
            raise ValueError(f"not an integer: {v!r}")
        return int(v)
    return int(str(v).strip(), 0)


def _to_float(v: Any) -> float:
    if isinstance(v, bool):
        raise ValueError(f"not a number: {v!r}")
    return float(v)


def _to_text(v: Any) -> str:
    return " ".join(map(str, v)) if isinstance(v, (list, tuple)) else str(v)


def _to_str(v: Any) -> str:
    if isinstance(v, (dict, list)):
        raise ValueError(f"expected text, got {type(v).__name__}")
    return str(v)


_CONVERTERS = {
    "bool": _to_bool,
    "int": _to_int,
    "float": _to_float,
    "distance": parse_distance,
    "duration": parse_duration,
    "angle": parse_angle,
    "text": _to_text,
    "json": lambda v: v,
}


def coerce_arg(a: Arg, v: Any) -> Any:
    """Convert one JSON/CLI value to the spec type. Raises ValueError with a message naming the argument."""
    if v is None:
        return None
    try:
        out = _CONVERTERS.get(a.type, _to_str)(v)  # str / path fall back to _to_str
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{a.cli_name}: {exc}") from None
    if isinstance(out, float) and math.isnan(out):
        raise ValueError(f"{a.cli_name}: not a number")
    if a.choices and out is not None and str(out) not in a.choices:
        raise ValueError(f"{a.cli_name}: {out!r} is not one of {', '.join(a.choices)}")
    return out


def coerce_args(cmd: Cmd, args: dict[str, Any]) -> dict[str, Any]:
    """Validate + convert the declared arguments of `cmd` (unknown keys pass through unchanged).

    Raises ValueError for a bad value or a missing required argument; that is the only path that should
    produce a BAD_ARGUMENT for type/shape problems.
    """
    out = dict(args)
    for a in cmd.args:
        if a.name in out:
            out[a.name] = coerce_arg(a, out[a.name])
        if a.required and out.get(a.name) in (None, ""):
            raise ValueError(f"missing required argument {a.name!r}")
    return out
