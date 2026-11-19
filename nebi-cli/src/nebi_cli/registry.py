"""Command registry: `@command("name")` marks a handler; `collect_handlers` binds them to the spec.

Adding a daemon command = one `Cmd` in command_table.py + one `@command("name")` method in a CommandGroup.
Extensions do the same through `AgentExtensionContext.add_commands(cmds, handlers=TheirGroup)`.

Handlers take the coerced argument dict and return a JSON-able result (by convention a dict with a one-line
`summary`). Internal keys (never accepted from outside, see AgentRuntime.call):
  * `_warnings`  — list the handler appends human-readable warnings to (always present)
  * `_speed_cap` — safety_ops.SpeedCap set by the precheck for drive/move near people
  * `_auto`      — low-priority automatic speech (idle/body babble)
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from typing import TYPE_CHECKING, Any, TypeVar

from nebi_cli import spec as S

if TYPE_CHECKING:
    from nebi.client import AsyncNebiClient
    from nebi_cli.activity import ActivityTracker
    from nebi_cli.events import EventLog
    from nebi_cli.motion import MotionGate
    from nebi_cli.runtime import AgentRuntime
    from nebi_cockpit.app import NebiServerManager

Args = dict[str, Any]
Handler = Callable[[Args], Awaitable[Any]]
F = TypeVar("F", bound=Callable[..., Awaitable[Any]])

_COMMAND_ATTR = "__nebi_command__"


def command(name: str) -> Callable[[F], F]:
    """Mark a CommandGroup coroutine method as the daemon handler of spec command `name`."""

    def mark(fn: F) -> F:
        setattr(fn, _COMMAND_ATTR, name)
        return fn

    return mark


class CommandGroup:
    """Base of the *_ops command groups: handlers get the runtime's services through these accessors."""

    def __init__(self, rt: AgentRuntime) -> None:
        self.rt = rt

    @property
    def manager(self) -> NebiServerManager:
        return self.rt.manager

    @property
    def motion(self) -> MotionGate:
        return self.rt.motion

    @property
    def services(self) -> dict[str, Any]:
        """Shared objects registered by extensions (`AgentExtensionContext.add_service`)."""
        return self.rt.services

    @property
    def events(self) -> EventLog:
        return self.rt.events

    @property
    def activity(self) -> ActivityTracker:
        return self.rt.activity

    def client(self) -> AsyncNebiClient:
        """The connected robot client, or NOT_CONNECTED."""
        return self.rt.client()

    def connected_client(self) -> AsyncNebiClient | None:
        """The connected robot client, or None (for commands that also work without the robot)."""
        return self.rt.connected_client()


def collect_handlers(groups: Iterable[CommandGroup], *, disabled: frozenset[str] = frozenset()) -> dict[str, Handler]:
    """name -> bound handler for every `@command` in `groups`, skipping `disabled` names.

    Raises ValueError for a handler whose name is not in the spec (typo) or is registered twice.
    """
    known = set(S.COMMANDS_BY_NAME) | S.INTERNAL_COMMANDS
    out: dict[str, Handler] = {}
    for group in groups:
        for attr in dir(type(group)):
            name = getattr(getattr(type(group), attr, None), _COMMAND_ATTR, None)
            if name is None:
                continue
            if name not in known:
                raise ValueError(f"{type(group).__name__}.{attr}: @command({name!r}) is not in the spec")
            if name in out:
                raise ValueError(f"command {name!r} registered twice")
            if name not in disabled:
                out[name] = getattr(group, attr)
    return out
