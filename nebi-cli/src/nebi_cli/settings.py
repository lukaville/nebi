"""Shared locations and ports for the `nebi` CLI and daemon. Pure stdlib (the CLI imports it)."""

from __future__ import annotations

import os
from pathlib import Path

CACHE_DIR = Path(os.environ.get("NEBI_CACHE_DIR", Path.home() / ".cache" / "pynebi"))
CONFIG_DIR = Path.home() / ".config" / "pynebi"
DAEMON_LOG = CACHE_DIR / "daemon.log"
DEFAULT_PORT = 8765  # the standalone `nebi` daemon; a `nebi-cockpit serve` cockpit hosts the same API on its own port
