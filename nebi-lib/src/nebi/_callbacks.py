"""Listener registries and guarded callback dispatch shared by the client and the intercom.

A listener may be sync or async. A raising listener is logged and never breaks the caller (transport receive
paths, connection handling); a coroutine result is run as a tracked task (reference kept, failures logged).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import Any, Generic, TypeVar

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])


class TaskSet:
    """Owns fire-and-forget tasks: keeps references (no GC mid-flight), logs failures, cancels on demand."""

    def __init__(self, name: str = "background") -> None:
        self._name = name
        self._tasks: set[asyncio.Task[Any]] = set()

    def spawn(self, coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._done)
        return task

    def _done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.error("%s task failed", self._name, exc_info=task.exception())

    def cancel_all(self) -> None:
        for task in tuple(self._tasks):
            task.cancel()

    def __len__(self) -> int:
        return len(self._tasks)


def call_guarded(cb: Callable[..., Any], args: tuple[Any, ...], tasks: TaskSet) -> None:
    """Invoke `cb(*args)`; log (do not propagate) exceptions; run an awaitable result on `tasks`."""
    try:
        res = cb(*args)
    except Exception:  # a listener must never break the dispatching code path
        logger.exception("listener %r raised", cb)
        return
    if asyncio.iscoroutine(res):
        tasks.spawn(res)


class ListenerSet(Generic[F]):
    """Ordered set of callbacks; `add()` returns an idempotent unsubscribe function."""

    def __init__(self, tasks: TaskSet) -> None:
        self._listeners: list[F] = []
        self._tasks = tasks

    def add(self, callback: F) -> Callable[[], None]:
        self._listeners.append(callback)

        def _remove() -> None:
            if callback in self._listeners:
                self._listeners.remove(callback)

        return _remove

    def emit(self, *args: Any) -> None:
        for cb in tuple(self._listeners):  # snapshot: listeners may unsubscribe while being called
            call_guarded(cb, args, self._tasks)

    def __bool__(self) -> bool:
        return bool(self._listeners)

    def __len__(self) -> int:
        return len(self._listeners)
