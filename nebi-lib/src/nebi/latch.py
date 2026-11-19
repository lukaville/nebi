"""Persisted bit-59 (stand_up_fold) latch, per robot.

The robot remembers the last bit-59 value it received ACROSS client sessions (0->1 = fold, 1->0 = stand).
Every frame we send, including the UCP transport's own heartbeats from the moment a session opens, must
carry the last value we sent to THAT robot, or a reconnect can stand a folded robot up (seen 2026-11-16).

Keyed by robot host. key=None (simulator / loopback) means: no persistence, latch starts at 0. A shared
unkeyed file let the auto-started simulator overwrite the real robot's latch, which stood it up.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

BIT59 = 1 << 59  # RcJoystick stand_up_fold bit
_LOOPBACK = {"127.0.0.1", "localhost", "::1", ""}


def latch_key_for_host(host: str | None) -> str | None:
    """Persistence key for `host`'s latch; `None` (no persistence) for loopback / the simulator."""
    forced = os.environ.get("PYNEBI_LATCH_FORCE_KEY")       # tests: persist even for the loopback simulator
    if forced:
        return forced
    return None if host is None or host in _LOOPBACK or host.startswith("127.") else host


def latch_file() -> Path:
    """Latch store path (`PYNEBI_LATCH_FILE` overrides `~/.config/pynebi/stand_fold_latch.json`)."""
    return Path(os.environ.get("PYNEBI_LATCH_FILE", Path.home() / ".config" / "pynebi" / "stand_fold_latch.json"))


def _read() -> dict[str, Any]:
    try:
        d = json.loads(latch_file().read_text())
        return d if isinstance(d, dict) and isinstance(d.get("robots"), dict) else {"robots": {}}
    except (OSError, ValueError):
        return {"robots": {}}


def load_latch(key: str | None) -> bool:
    """Last bit-59 value sent to the robot `key` (or, for an unknown key, the most recently saved one)."""
    if key is None:
        return False
    robots = _read()["robots"]
    if key in robots:
        return bool(robots[key].get("stand_up_fold", False))
    # Unknown host (e.g. the SoftAP 192.168.7.1 during setup-wifi, or a new DHCP address): it is almost always the
    # same robot, so start from the most recently saved value rather than 0. Starting at 0 after the official app
    # left the bit at 1 is a 1 -> 0 edge = stand up (2026-11-17: setup-wifi after using the app).
    if robots:
        latest = max(robots.values(), key=lambda r: r.get("saved_at", 0))
        return bool(latest.get("stand_up_fold", False))
    return False


def save_latch(key: str | None, value: bool) -> None:
    """Persist the bit-59 value just sent to robot `key` (best effort: an unwritable store is logged, not raised)."""
    if key is None:
        return
    try:
        d = _read()
        d["robots"][key] = {"stand_up_fold": bool(value), "saved_at": time.time()}
        f = latch_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_name(f".{f.name}.{os.getpid()}.tmp")  # per-process temp: concurrent savers can't clash
        tmp.write_text(json.dumps(d, indent=1))
        tmp.replace(f)
    except OSError as exc:
        logger.warning("Could not persist stand/fold latch for %s: %s", key, exc)


def apply_latch(word: int, key: str | None) -> int:
    """Initial joystick word with bit 59 set to the persisted latch for `key`."""
    return (word | BIT59) if load_latch(key) else (word & ~BIT59)
