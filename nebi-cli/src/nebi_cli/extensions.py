"""Agent-side extension API: the hooks the `nebi` daemon consults and the context extensions register them with.

This is the public facade for command/agent extensions: everything one normally needs (`Cmd`, `Arg`, `command`,
`CommandGroup`, `AgentError`, exit codes, the hook Protocols, plus nebi's generic loader names) is importable
from here. Discovery/loading is nebi's (`nebi.extensions`); the cockpit side is `nebi_cockpit.extensions`.

An extension's ``setup_agent(ctx)`` runs
  * once per process in **spec-only** mode (``ctx.runtime is None``: the `nebi` CLI process building its argv
    parser, `nebi schema`, `nebi guide`): only command specs, look views, wait-event names, event-source names and
    error codes matter there; every runtime registration is accepted and ignored, so ``setup_agent`` needs no
    branches (just keep it cheap: no I/O, connect lazily);
  * once per AgentRuntime (the daemon, or the agent embedded in a `nebi-cockpit serve` cockpit) with ``ctx.runtime`` set.

Hooks (all optional; the core works without any of them):
  * commands        ``add_commands(cmds, handlers=GroupClass, group_titles=...)`` — `Cmd` specs plus a CommandGroup
                    subclass whose ``@command("name")`` methods handle them (instantiated with the runtime)
  * carried state   ``set_carried_state_provider(p)`` — consulted by the SafetyGate before body/stunt commands,
                    by `emote`, `care` and idle. Without one the carried state is *unknown*: body moves warn
                    ``carried-check unavailable: ...`` and stunts are refused unless ``--ignore-carried``
  * people          ``set_people_provider(p)`` — speed cap for drive/move near people, `body look-at` bearings
                    (the core falls back to camera targets)
  * events          ``add_event_source(src)`` — merged into `nebi events` (``--source <name>``,
                    ``--<name>-since``) and the idle loop's reactions
  * look views      ``add_look_view(name, handler, help)`` — extra ``nebi look --view <name>``
  * wait events     ``add_wait_events({name: description}, handler)`` — extra ``nebi wait --for <name>``
  * care            ``add_care_provider(fn)`` — extra fields / needs / advice for `nebi care`
  * look-at targets ``add_target_locator(fn)`` — `body look-at <target>` for non-person targets
  * services        ``add_service(name, obj)`` — shared objects on ``runtime.services`` for your handlers
  * TTS             ``register_tts_engine(engine)`` — a nebi.tts.TtsEngine for `nebi say` (``--engine NAME``)
  * error codes     ``add_error_codes({code: (exit_code, meaning)})``
  * shutdown        ``on_shutdown(async_fn)`` — called when the runtime stops

A failing ``setup_agent`` is rolled back (its commands and hooks are removed), logged and reported in
`nebi status`; it never breaks the core.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, TypedDict, runtime_checkable

from nebi.extension_loader import (
    ENTRY_POINT_GROUP,
    ENV_DISABLE,
    ENV_EXTENSIONS,
    LoadedExtension,
    active_extensions,
    extension_report,
    load_extensions,
    register_report_hook,
    register_reset_hook,
    reset_extensions,
    use_extensions,
)
from nebi.extensions import NebiExtension
from nebi.tts import SpeechAudio, TtsEngine, TtsError, register_engine, unregister_engine
from nebi_cli import spec as S
from nebi_cli.command_table import FORCE, IGNORE_CARRIED
from nebi_cli.errors import CACHE_DIR, ERROR_CODES, STANDING, AgentError, bad_arg, parse_xy, register_error_codes
from nebi_cli.registry import Args, CommandGroup, collect_handlers, command
from nebi_cli.spec_model import (
    EXIT_FAILED,
    EXIT_OK,
    EXIT_SAFETY,
    EXIT_TIMEOUT,
    EXIT_UNAVAILABLE,
    EXIT_USAGE,
    Arg,
    Cmd,
)

if TYPE_CHECKING:
    from nebi_cli.runtime import AgentRuntime

__all__ = [
    "CACHE_DIR",
    "ENTRY_POINT_GROUP",
    "ENV_DISABLE",
    "ENV_EXTENSIONS",
    "EXIT_FAILED",
    "EXIT_OK",
    "EXIT_SAFETY",
    "EXIT_TIMEOUT",
    "EXIT_UNAVAILABLE",
    "EXIT_USAGE",
    "FORCE",
    "IGNORE_CARRIED",
    "STANDING",
    "AgentError",
    "AgentExtensionContext",
    "AgentHooks",
    "Arg",
    "Args",
    "CareContribution",
    "CareProvider",
    "CarriedStateProvider",
    "Cmd",
    "CommandGroup",
    "EventSource",
    "LoadedExtension",
    "LookViewHandler",
    "NebiExtension",
    "PeopleProvider",
    "ShutdownCallback",
    "SpeechAudio",
    "TargetLocator",
    "TtsEngine",
    "TtsError",
    "WaitHandler",
    "active_extensions",
    "bad_arg",
    "command",
    "ensure_agent_spec",
    "extension_guides",
    "extension_report",
    "load_extensions",
    "parse_xy",
    "reset_extensions",
    "setup_agent_extensions",
    "use_extensions",
]

logger = logging.getLogger(__name__)


# ============================================================================= hook protocols


@runtime_checkable
class CarriedStateProvider(Protocol):
    async def carried_state(self) -> tuple[bool | None, str]:
        """(True/False, source) when known, (None, why it is unknown) otherwise. Should answer within ~0.6 s."""
        ...


@runtime_checkable
class PeopleProvider(Protocol):
    async def people(self) -> list[dict[str, Any]]:
        """People/pets around the robot, each a dict with (as available) ``type`` ("person", "pet", ...),
        ``distance_m``, ``bearing_deg`` (left positive) and ``direction`` ("ahead", "front-left", "front-right",
        "left", "right", "behind"). Return [] when unknown."""
        ...


@runtime_checkable
class EventSource(Protocol):
    """A cursor-addressable event stream. Events are dicts with at least ``seq`` (int, increasing), ``kind`` and
    ``summary``; the idle loop reacts to kinds ``touched`` (``detail``: head_pat / pushed / shaken / startled),
    ``gesture`` (``gesture``: palm / palm_near / thumbs_up), ``person_appeared``, ``bumped``, ``battery_low``."""

    name: str  # lowercase; becomes `nebi events --source <name>` / `--<name>-since`
    description: str

    async def cursor(self) -> int:
        """The current last seq (only events after it are new)."""
        ...

    async def poll(self, since: int, *, wait: float = 0.0, limit: int = 50) -> tuple[list[dict[str, Any]], int]:
        """Events with seq > since (long-polling up to `wait` s for the first one) and the new last seq.
        Raise AgentError when the source is unavailable."""
        ...


class CareContribution(TypedDict, total=False):
    fields: dict[str, Any]  # merged into the `care` result
    needs: list[str]  # e.g. "cool_down"
    advice: list[str]
    warnings: list[str]


LookViewHandler = Callable[[Args], Awaitable[dict[str, Any]]]
"""`look --view <name>`: gets the coerced args (``out``, ``max_width``, ``_warnings``, ...); returns at least ``path``."""
WaitHandler = Callable[[str, str, float], Awaitable[dict[str, Any]]]
"""`wait --for name[=value]`: (name, value or "", timeout_s) -> ``{"met": True, "event": ..., "summary": ...}``;
raise AgentError("WAIT_TIMEOUT", ..., exit_code=EXIT_TIMEOUT) on timeout."""
CareProvider = Callable[[], Awaitable[CareContribution]]
TargetLocator = Callable[[str], Awaitable["tuple[float, str] | None"]]
"""`body look-at <target>`: target -> (bearing_deg, left positive; label) or None when unknown."""
ShutdownCallback = Callable[[], Awaitable[None]]


@dataclass
class AgentHooks:
    """What the runtime consults; filled by extensions (empty = core behaviour)."""

    carried: CarriedStateProvider | None = None
    people: PeopleProvider | None = None
    event_sources: dict[str, EventSource] = field(default_factory=dict)
    look_views: dict[str, LookViewHandler] = field(default_factory=dict)
    wait_events: dict[str, WaitHandler] = field(default_factory=dict)
    care: list[CareProvider] = field(default_factory=list)
    target_locators: list[TargetLocator] = field(default_factory=list)
    shutdown: list[ShutdownCallback] = field(default_factory=list)
    owners: dict[str, str] = field(default_factory=dict)  # "carried"/"people" -> extension name


# ============================================================================= context


class AgentExtensionContext:
    """Passed to ``NebiExtension.setup_agent``. ``runtime`` is None in spec-only mode (see module docs)."""

    def __init__(self, extension: str, runtime: AgentRuntime | None = None) -> None:
        self.extension = extension
        self.runtime = runtime
        self._undo: list[Callable[[], object]] = []

    @property
    def spec_only(self) -> bool:
        return self.runtime is None

    @property
    def _hooks(self) -> AgentHooks | None:
        return self.runtime.hooks if self.runtime is not None else None

    # ------------------------------------------------------------------ spec-level (both modes)

    def add_commands(
        self,
        cmds: Iterable[S.Cmd],
        handlers: Callable[[AgentRuntime], CommandGroup] | None = None,
        *,
        group_titles: dict[str, str] | None = None,
    ) -> None:
        """Register command specs and (with a runtime) the CommandGroup handling them."""
        cmds = tuple(cmds)
        S.register_extension_commands(self.extension, cmds, group_titles)
        self._undo.append(lambda: S.unregister_extension(self.extension))
        rt = self.runtime
        if rt is None or handlers is None:
            return
        found = collect_handlers([handlers(rt)])
        clash = sorted(set(found) & set(rt.handlers))
        if clash:
            raise ValueError(f"handlers already registered: {', '.join(clash)}")
        missing = [c.name for c in cmds if not c.local and c.name.replace("-", "_") not in found]
        if missing:
            raise ValueError(f"no @command handler for: {', '.join(missing)}")
        rt.handlers.update(found)
        self._undo.append(lambda: [rt.handlers.pop(n, None) for n in found])

    def add_error_codes(self, codes: dict[str, tuple[int, str]]) -> None:
        new = [c for c in codes if c not in ERROR_CODES]
        register_error_codes(codes)
        self._undo.append(lambda: [ERROR_CODES.pop(c, None) for c in new])

    def add_look_view(self, name: str, handler: LookViewHandler, help: str) -> None:
        S.register_extension_look_view(self.extension, name, help)
        self._undo.append(lambda: S.unregister_extension(self.extension))
        hooks = self._hooks
        if hooks is not None:
            hooks.look_views[name] = handler
            self._undo.append(lambda: hooks.look_views.pop(name, None))

    def add_wait_events(self, events: dict[str, str], handler: WaitHandler) -> None:
        """`nebi wait --for <name>` for each name (name -> one-line description)."""
        for name, desc in events.items():
            S.register_extension_wait_event(self.extension, name, desc)
        self._undo.append(lambda: S.unregister_extension(self.extension))
        hooks = self._hooks
        if hooks is not None:
            for name in events:
                hooks.wait_events[name] = handler
            self._undo.append(lambda: [hooks.wait_events.pop(n, None) for n in events])

    def add_event_source(self, source: EventSource) -> None:
        S.register_extension_event_source(self.extension, source.name, source.description)
        self._undo.append(lambda: S.unregister_extension(self.extension))
        hooks = self._hooks
        if hooks is not None:
            hooks.event_sources[source.name] = source
            self._undo.append(lambda: hooks.event_sources.pop(source.name, None))

    # ------------------------------------------------------------------ runtime-level (ignored in spec-only mode)

    def _set_single(self, slot: str, value: Any) -> None:
        hooks = self._hooks
        if hooks is None:
            return
        owner = hooks.owners.get(slot)
        if owner is not None and owner != self.extension:
            raise ValueError(f"the {slot} provider is already set by extension {owner!r}")
        setattr(hooks, slot, value)
        hooks.owners[slot] = self.extension

        def undo() -> None:
            setattr(hooks, slot, None)
            hooks.owners.pop(slot, None)

        self._undo.append(undo)

    def set_carried_state_provider(self, provider: CarriedStateProvider) -> None:
        self._set_single("carried", provider)

    def set_people_provider(self, provider: PeopleProvider) -> None:
        self._set_single("people", provider)

    def add_care_provider(self, provider: CareProvider) -> None:
        hooks = self._hooks
        if hooks is not None:
            hooks.care.append(provider)
            self._undo.append(lambda: hooks.care.remove(provider))

    def add_target_locator(self, locator: TargetLocator) -> None:
        hooks = self._hooks
        if hooks is not None:
            hooks.target_locators.append(locator)
            self._undo.append(lambda: hooks.target_locators.remove(locator))

    def add_service(self, name: str, service: Any) -> None:
        rt = self.runtime
        if rt is None:
            return
        if name in rt.services:
            raise ValueError(f"service {name!r} already registered")
        rt.services[name] = service
        self._undo.append(lambda: rt.services.pop(name, None))

    def register_tts_engine(self, engine: TtsEngine) -> None:
        """Make a nebi.tts.TtsEngine available to `nebi say` (process-wide registry, see nebi.tts)."""
        if self.runtime is None:
            return
        register_engine(engine, replace=True)
        self._undo.append(lambda: unregister_engine(engine.name, engine))

        async def _unregister() -> None:  # the engine lives as long as the runtime that registered it
            unregister_engine(engine.name, engine)

        self.on_shutdown(_unregister)

    def on_shutdown(self, callback: ShutdownCallback) -> None:
        hooks = self._hooks
        if hooks is not None:
            hooks.shutdown.append(callback)
            self._undo.append(lambda: hooks.shutdown.remove(callback))

    def rollback(self) -> None:
        for undo in reversed(self._undo):
            try:
                undo()
            except Exception:  # best effort: a half-registered hook must not crash the core
                logger.debug("extension %s rollback step failed", self.extension, exc_info=True)
        self._undo.clear()


# ============================================================================= application


def _setup_one(ext: LoadedExtension, runtime: AgentRuntime | None) -> None:
    setup = getattr(ext.obj, "setup_agent", None)
    if setup is None:
        return
    ctx = AgentExtensionContext(ext.name, runtime)
    try:
        setup(ctx)
    except Exception as exc:
        ctx.rollback()
        ext.record_error("setup_agent", exc)


_spec_ready = False


def ensure_agent_spec() -> None:
    """Spec-only setup of every extension, once per process (CLI parser, `nebi schema`, `nebi guide`)."""
    global _spec_ready
    if _spec_ready:
        return
    _spec_ready = True
    for ext in active_extensions():
        _setup_one(ext, None)


def setup_agent_extensions(runtime: AgentRuntime) -> None:
    """Run every extension's ``setup_agent`` against `runtime` (called from AgentRuntime.__init__)."""
    global _spec_ready
    _spec_ready = True
    for ext in active_extensions():
        _setup_one(ext, runtime)


def reset() -> None:
    global _spec_ready
    _spec_ready = False
    S.reset_extension_spec()


def _report(name: str) -> dict[str, Any]:
    return {"commands": S.extension_command_names(name)}


# nebi's loader knows nothing about the agent: plug the agent's reset + `commands` report field into it.
register_reset_hook(reset)
register_report_hook(_report)


def extension_guides() -> list[tuple[str, str]]:
    """(extension name, markdown) for each loaded extension that has a guide section."""
    out = []
    for ext in active_extensions():
        fn = getattr(ext.obj, "guide_markdown", None)
        if fn is None:
            continue
        try:
            md = fn()
        except Exception as exc:
            ext.record_error("guide_markdown", exc)
            continue
        if md:
            out.append((ext.name, str(md)))
    return out
