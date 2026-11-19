"""Robot-link bookkeeping for the cockpit: the stage log and the single-flight link-operation runner.

Link-mutating operations (connect / switch flow, BLE bootstrap, BLE -> home Wi-Fi provisioning, robot
Wi-Fi handoff, disconnect) all run through `LinkOpRunner.run()`: one tracked asyncio task at a time.
Starting a new operation cancels the one in flight (latest wins), so a user can always abort a 30 s BLE
pairing wait by disconnecting or picking another flow. The superseded caller gets a structured
`LINK_OP_CANCELLED` error instead of hanging.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Coroutine
from typing import Any, TypeVar

from nebi.exceptions import NebiError
from nebi.types import ConnectionStage

T = TypeVar("T")

STAGE_HISTORY_MAX = 18
"""Stage transitions kept for the cockpit's connection timeline."""


class LinkStageLog:
    """Manager-level connection stage, detail, last error and a bounded history.

    Authoritative while there is no client (mid-handshake / offline); a connected client's own link
    telemetry takes over in `NebiServerManager.connection_summary()`."""

    def __init__(self) -> None:
        self.stage: str = ConnectionStage.DISCONNECTED.value
        self.detail: str = "Disconnected"
        self.last_error: dict[str, Any] | None = None
        self.history: list[dict[str, Any]] = []

    def record(self, stage: ConnectionStage | str, detail: str = "", err: NebiError | None = None) -> None:
        """Stage callback (signature of `nebi.client.StageCallback`)."""
        value = stage.value if isinstance(stage, ConnectionStage) else str(stage)
        self.stage = value
        self.detail = detail
        if err is not None:
            self.last_error = err.to_dict()
        elif value == ConnectionStage.CONNECTED.value:
            self.last_error = None
        self.history.append({"stage": value, "detail": detail, "ts": round(time.time(), 3)})
        if len(self.history) > STAGE_HISTORY_MAX:
            del self.history[:-STAGE_HISTORY_MAX]

    def fail(self, exc: BaseException, hint: str) -> NebiError:
        """Record an ERROR stage for any exception and return it as a structured `NebiError`."""
        if isinstance(exc, NebiError):
            err = exc
        else:
            err = NebiError(
                "CONNECTION_FAILED",
                str(exc) or exc.__class__.__name__,
                stage=self.stage,
                recoverable=True,
                hint=hint,
            )
        self.record(ConnectionStage.ERROR, err.message, err)
        return err


class LinkOpRunner:
    """Single-flight guard for everything that changes the robot link (latest request wins)."""

    def __init__(self, log: LinkStageLog) -> None:
        self._log = log
        self._task: asyncio.Task[Any] | None = None
        self._name = ""
        self._guard = asyncio.Lock()  # serializes cancel-previous + start-next

    @property
    def busy(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def current_name(self) -> str | None:
        """Name of the operation in flight, or None."""
        return self._name if self.busy else None

    @property
    def guard(self) -> asyncio.Lock:
        """Held while a new operation is being started; shutdown takes it to stop new ones racing in."""
        return self._guard

    async def cancel_current(self) -> None:
        """Cancel the operation in flight (if any) and wait for it to finish. Caller holds `guard`."""
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.wait({task})

    async def run(self, name: str, fn: Callable[[], Coroutine[Any, Any, T]]) -> T:
        """Cancel the operation in flight and run `fn()` as the new tracked task."""
        async with self._guard:
            if self.busy:
                self._log.record(ConnectionStage.DISCONNECTED, f"Cancelled {self._name} (superseded by {name})")
            await self.cancel_current()
            task: asyncio.Task[T] = asyncio.get_running_loop().create_task(fn(), name=f"nebi-link-{name}")
            self._task = task
            self._name = name
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled() and not _current_task_cancelling():
                # Superseded by another link operation (or a disconnect), not cancelled ourselves.
                raise NebiError(
                    "LINK_OP_CANCELLED",
                    f"'{name}' was cancelled by a newer connection request.",
                    stage=self._log.stage,
                    recoverable=True,
                    hint="The newer request (connect / disconnect) is now in charge of the link.",
                ) from None
            task.cancel()  # our own request was cancelled: take the operation down with it
            raise


def _current_task_cancelling() -> bool:
    """True if the running task itself has a pending cancellation (`Task.cancelling()` is 3.11+)."""
    me = asyncio.current_task()
    cancelling = getattr(me, "cancelling", None)
    return bool(cancelling is not None and cancelling())
