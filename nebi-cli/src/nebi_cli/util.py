"""Small helpers shared by the agent command modules."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any


async def wait_until(pred: Callable[[], bool], timeout: float, poll: float = 0.05) -> bool:
    """Poll `pred` until it holds or `timeout` passes; returns its final value.

    Telemetry is a plain snapshot with no change notification, so polling is the only option here.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        await asyncio.sleep(poll)
    return pred()


def param_values(raw: Any) -> dict[str, Any]:
    """Config agent replies `{"ok":..., "values": {...}}`; the Zenoh fallback returns a flat dict."""
    if isinstance(raw, dict) and isinstance(raw.get("values"), dict):
        return raw["values"]
    return raw if isinstance(raw, dict) else {}


def warn_once(warnings: list[str], msg: str) -> None:
    if msg not in warnings:
        warnings.append(msg)


def num(v: Any, default: float) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def where_in_image(cx: float) -> str:
    """Horizontal third of a normalized image x coordinate."""
    if cx < 0.33:
        return "left"
    if cx > 0.67:
        return "right"
    return "center"
