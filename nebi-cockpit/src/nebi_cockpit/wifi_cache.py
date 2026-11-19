"""`WifiSsidCache`: the host's current Wi-Fi SSID, refreshed off the event loop."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

from nebi.network import get_current_wifi_ssid

logger = logging.getLogger(__name__)

DEFAULT_SSID_TTL_S = 10.0


class WifiSsidCache:
    """Host Wi-Fi SSID, refreshed off the event loop.

    `get_current_wifi_ssid()` shells out to `networksetup` (~45 ms on macOS). The status payload is pushed
    at ~15 Hz per cockpit tab, so it must never run synchronously on the loop: `get()` returns the cached
    value immediately and kicks a background refresh (`asyncio.to_thread`) once the value is older than
    `ttl` seconds."""

    def __init__(self, ttl: float = DEFAULT_SSID_TTL_S, fetch: Callable[[], str | None] | None = None) -> None:
        self.ttl = ttl
        # resolved at call time so tests can monkeypatch `get_current_wifi_ssid` in this module
        self._fetch: Callable[[], str | None] = fetch or (lambda: get_current_wifi_ssid())  # noqa: PLW0108
        self.value: str | None = None
        self._ts = float("-inf")
        self._task: asyncio.Task[None] | None = None

    def get(self) -> str | None:
        if time.monotonic() - self._ts > self.ttl:
            self._kick()
        return self.value

    def _kick(self) -> None:
        if self._task is not None and not self._task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no loop (synchronous use): keep the cached value
        self._task = loop.create_task(self._refresh(), name="nebi-wifi-ssid")

    async def _refresh(self) -> None:
        try:
            value = await asyncio.to_thread(self._fetch)
        except Exception as exc:
            logger.debug("host Wi-Fi SSID lookup failed: %s", exc)
            value = None
        self.value = value
        self._ts = time.monotonic()

    async def refresh_now(self) -> str | None:
        if self._task is not None and not self._task.done():
            await asyncio.shield(self._task)
        else:
            await self._refresh()
        return self.value

    def cancel(self) -> None:
        if self._task is not None:
            self._task.cancel()
