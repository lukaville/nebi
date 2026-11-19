"""TOML loading (Python 3.10+ compatible) and vendor-layout `mass.cfg` serialization."""

from __future__ import annotations

import sys
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


def loads(text: str) -> dict[str, Any]:
    """Parse a TOML string into a dictionary."""
    data: dict[str, Any] = tomllib.loads(text)
    return data


def _format_value(val: Any) -> str:
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, int):
        return str(val)
    if isinstance(val, float):
        return repr(val)
    if isinstance(val, str):
        escaped = val.replace("'", "''")
        return f"'{escaped}'"
    raise TypeError(f"Unsupported TOML value type: {type(val).__name__}")


def render_mass_cfg(cfg: dict[str, Any]) -> str:
    """Serialize a `mass.cfg` dictionary back to TOML matching the vendor layout."""
    lines: list[str] = ["[package]"]
    for key, val in cfg.get("package", {}).items():
        lines.append(f"{key} = {_format_value(val)}")
    lines.append("")

    for fw in cfg.get("firmwares", []):
        lines.append("[[firmwares]]")
        for key, val in fw.items():
            lines.append(f"{key} = {_format_value(val)}")
        lines.append("")

    if "strategy" in cfg:
        lines.append("[strategy]")
        for key, val in cfg["strategy"].items():
            lines.append(f"{key} = {_format_value(val)}")
        lines.append("")

    return "\n".join(lines)
