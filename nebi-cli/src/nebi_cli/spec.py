"""Public facade of the `nebi` command spec, shared by the CLI (argv parsing, `nebi schema`) and the daemon (dispatch).

  * `spec_model.py`    — `Arg` / `Cmd` dataclasses and exit codes
  * `command_table.py` — the command table itself (data)
  * `units.py`         — unit parsers and argument coercion
  * this module        — the active command set (core + what loaded extensions registered), lookups, schema

Importing it must stay cheap (stdlib only, no robot/transport imports).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from nebi_cli.command_table import (
    BODY_CHOICES,
    CORE_COMMANDS,
    CORE_GROUP_TITLES,
    FORCE,
    IGNORE_CARRIED,
    LOOK_VIEWS,
)
from nebi_cli.spec_model import (
    EXIT_CODES,
    EXIT_FAILED,
    EXIT_INTERRUPTED,
    EXIT_OK,
    EXIT_SAFETY,
    EXIT_TIMEOUT,
    EXIT_UNAVAILABLE,
    EXIT_USAGE,
    Arg,
    Cmd,
    MotionKind,
)
from nebi_cli.units import coerce_arg, coerce_args, parse_angle, parse_distance, parse_duration

__all__ = [
    "BODY_CHOICES",
    "COMMANDS",
    "COMMANDS_BY_NAME",
    "CORE_WAIT_EVENTS",
    "EXIT_CODES",
    "EXIT_FAILED",
    "EXIT_INTERRUPTED",
    "EXIT_OK",
    "EXIT_SAFETY",
    "EXIT_TIMEOUT",
    "EXIT_UNAVAILABLE",
    "EXIT_USAGE",
    "FORCE",
    "GROUP_TITLES",
    "IGNORE_CARRIED",
    "INTERNAL_COMMANDS",
    "Arg",
    "Cmd",
    "MotionKind",
    "coerce_arg",
    "coerce_args",
    "parse_angle",
    "parse_distance",
    "parse_duration",
    "register_extension_commands",
    "register_extension_event_source",
    "register_extension_look_view",
    "register_extension_wait_event",
    "reset_extension_spec",
    "schema_dict",
    "unregister_extension",
    "wait_event_names",
]

# Daemon commands without a CLI spelling: the CLI's local `up` / `down` send these (no spec entry, no coercion).
INTERNAL_COMMANDS: frozenset[str] = frozenset({"connect", "disconnect", "shutdown"})

# `nebi wait --for <name>` events the core answers from its own telemetry event log (agent/events.py).
# Extensions add more (AgentExtensionContext.add_wait_events); an extension event of the same name replaces these.
CORE_WAIT_EVENTS: dict[str, str] = {
    "person": "a person/pet detected by the camera (target_appeared)",
    "person_left": "a camera target lost (target_lost)",
    "battery_low": "battery dropping into a lower band",
    "charging": "charging started",
    "docked": "docked on the charger",
    "posture": "posture change (stand/fold)",
    "fault": "a new health fault",
}

# ---------------------------------------------------------------------------- extension contributions
# Filled by nebi_cli.extensions (one entry per registering extension); `_rebuild()` derives the active set.
_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
_ext_commands: dict[str, tuple[str, Cmd]] = {}  # command -> (extension, spec)
_ext_titles: dict[str, tuple[str, str]] = {}  # group -> (extension, title)
_ext_look_views: dict[str, tuple[str, str]] = {}  # view -> (extension, help)
_ext_wait_events: dict[str, tuple[str, str]] = {}  # event -> (extension, description)
_ext_event_sources: dict[str, tuple[str, str]] = {}  # source -> (extension, description)

COMMANDS: tuple[Cmd, ...] = CORE_COMMANDS
COMMANDS_BY_NAME: dict[str, Cmd] = {}
GROUP_TITLES: dict[str, str] = {}
_CORE_NAMES = frozenset(c.name for c in CORE_COMMANDS) | INTERNAL_COMMANDS


def _check_owner(table: Mapping[str, tuple[str, object]], key: str, ext: str, what: str) -> None:
    if not _NAME_RE.match(key):
        raise ValueError(f"{what} name {key!r} must be lowercase letters, digits, '_' or '-'")
    owner = table.get(key)
    if owner is not None and owner[0] != ext:
        raise ValueError(f"{what} {key!r} is already registered by extension {owner[0]!r}")


def register_extension_commands(ext: str, cmds: tuple[Cmd, ...] | list[Cmd], group_titles: dict[str, str] | None = None) -> None:
    """Add an extension's command specs (re-registering by the same extension replaces them)."""
    for c in cmds:
        if c.name in _CORE_NAMES:
            raise ValueError(f"extension {ext!r}: command {c.name!r} is a core command")
        _check_owner(_ext_commands, c.name, ext, "command")
    for c in cmds:
        _ext_commands[c.name] = (ext, c)
    for g, title in (group_titles or {}).items():
        if g in CORE_GROUP_TITLES:
            raise ValueError(f"extension {ext!r}: group {g!r} is a core group")
        _check_owner(_ext_titles, g, ext, "group")
        _ext_titles[g] = (ext, title)
    _rebuild()


def register_extension_look_view(ext: str, view: str, help: str) -> None:
    if view in LOOK_VIEWS:
        raise ValueError(f"extension {ext!r}: look view {view!r} is a core view")
    _check_owner(_ext_look_views, view, ext, "look view")
    _ext_look_views[view] = (ext, help)
    _rebuild()


def register_extension_wait_event(ext: str, name: str, description: str) -> None:
    _check_owner(_ext_wait_events, name, ext, "wait event")
    _ext_wait_events[name] = (ext, description)
    _rebuild()


def register_extension_event_source(ext: str, name: str, description: str) -> None:
    if name in ("all", "robot"):
        raise ValueError(f"extension {ext!r}: event source name {name!r} is reserved")
    _check_owner(_ext_event_sources, name, ext, "event source")
    _ext_event_sources[name] = (ext, description)
    _rebuild()


def reset_extension_spec() -> None:
    """Back to the core command set (tests; nebi.extension_loader.reset_extensions)."""
    for table in (_ext_commands, _ext_titles, _ext_look_views, _ext_wait_events, _ext_event_sources):
        table.clear()
    _rebuild()


def unregister_extension(ext: str) -> None:
    """Drop everything `ext` registered (rollback of a failed setup)."""
    for table in (_ext_commands, _ext_titles, _ext_look_views, _ext_wait_events, _ext_event_sources):
        for key in [k for k, (owner, _v) in table.items() if owner == ext]:
            del table[key]
    _rebuild()


def extension_command_names(ext: str) -> list[str]:
    return [name for name, (owner, _c) in _ext_commands.items() if owner == ext]


def wait_event_names() -> dict[str, str]:
    """Every `wait --for` event name -> description (core, overlaid by extensions)."""
    return {**CORE_WAIT_EVENTS, **{k: d for k, (_e, d) in _ext_wait_events.items()}}


def look_views() -> tuple[str, ...]:
    return LOOK_VIEWS + tuple(_ext_look_views)


def event_sources() -> tuple[str, ...]:
    return tuple(_ext_event_sources)


def _patch_core(cmd: Cmd) -> Cmd:
    """Fold extension views / wait events / event sources into the core commands that expose them."""
    if cmd.name == "look" and _ext_look_views:
        help_ = "camera = what the camera sees; " + "; ".join(f"{v} = {h}" for v, (_e, h) in _ext_look_views.items())
        args = tuple(replace(a, choices=look_views(), help=help_) if a.name == "view" else a for a in cmd.args)
        return replace(cmd, args=args, examples=(*cmd.examples, *(f"nebi look --view {v}" for v in _ext_look_views)))
    if cmd.name == "wait":
        names = wait_event_names()
        args = tuple(replace(a, help="Wait for something to happen instead: " + ", ".join(names) + ".") if a.name == "for" else a
                     for a in cmd.args)
        notes = cmd.notes + " Events for --for: " + "; ".join(f"{k} = {d}" for k, d in names.items()) + "."
        return replace(cmd, args=args, notes=notes)
    if cmd.name == "events" and _ext_event_sources:
        out: list[Arg] = []
        for arg in cmd.args:
            out.append(replace(arg, choices=("all", "robot", *_ext_event_sources)) if arg.name == "source" else arg)
            if arg.name == "since":
                out += [Arg(f"{s}_since", "int", f"Only {s} events with seq > this (use `next_{s}_since`). {d}", default=0)
                        for s, (_e, d) in _ext_event_sources.items()]
        return replace(cmd, args=tuple(out))
    return cmd


def _rebuild() -> None:
    global COMMANDS
    COMMANDS = tuple(_patch_core(c) for c in CORE_COMMANDS) + tuple(c for _e, c in _ext_commands.values())
    COMMANDS_BY_NAME.clear()
    COMMANDS_BY_NAME.update({c.name: c for c in COMMANDS})
    GROUP_TITLES.clear()
    GROUP_TITLES.update(CORE_GROUP_TITLES)
    GROUP_TITLES.update({g: t for g, (_e, t) in _ext_titles.items()})


_rebuild()


def schema_dict(only: str | None = None) -> dict[str, Any]:
    cmds = [c for c in COMMANDS if only is None or c.name == only]
    return {
        "program": "nebi",
        "description": "Agent-friendly CLI compatible with the Beni two-wheeled-legged robot (LZ110/LZ100). All output is JSON on stdout. "
        "Unofficial; not affiliated with the robot's manufacturer.",
        "output": {
            "success": '{"ok": true, "command": ..., "result": {...}, "state": {compact robot state}, "warnings": [...]}',
            "failure": '{"ok": false, "command": ..., "error": {"code", "message", "hint"}, "state": ...}',
        },
        "exit_codes": {str(k): v for k, v in EXIT_CODES.items()},
        "global_flags": {
            "--no-state": "Omit the trailing compact `state` object.",
            "--pretty / --compact": "Indented (default on a TTY) or single-line JSON.",
            "--port N": "Daemon port (env NEBI_PORT, default 8765).",
        },
        "units": {
            "distance": "1m, 50cm, 300mm, 2ft, or bare meters",
            "duration": "2s, 500ms, 1m, or bare seconds",
            "angle": "90, 45deg, 1.57rad, quarter, half",
        },
        "commands": [c.to_dict() for c in cmds],
        "extension_commands": {name: ext for name, (ext, _c) in _ext_commands.items()},
    }
