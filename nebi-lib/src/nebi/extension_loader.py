"""Discovery and loading of nebi extensions (stdlib only; the public facade is `nebi.extensions`).

Sources, in order:
  * Python entry points in the group ``nebi.extensions`` (what an installed extension package declares);
  * ``PYNEBI_EXTENSIONS="module:attr,..."`` (development: load without installing an entry point).

``PYNEBI_DISABLE_EXTENSIONS=1`` turns all of it off; ``PYNEBI_DISABLE_EXTENSIONS=name1,name2`` skips just those
extensions (by extension name or entry-point name). Each entry point / spec resolves to an extension object, or to a
class (instantiated without arguments) or zero-argument factory function returning one. A failing extension is
logged and reported (``nebi status``, ``GET /api/extensions``) and never breaks the core.
"""

from __future__ import annotations

import importlib
import inspect
import logging
import os
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from importlib import metadata
from typing import Any

logger = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "nebi.extensions"
ENV_EXTENSIONS = "PYNEBI_EXTENSIONS"
ENV_DISABLE = "PYNEBI_DISABLE_EXTENSIONS"

_TRUE = frozenset({"1", "true", "yes", "on", "all"})


@dataclass
class LoadedExtension:
    """One discovered extension: the object (None when it failed to load) plus where it came from."""

    name: str
    source: str  # "entry_point:<name>" | "env:<module:attr>" | "explicit"
    obj: Any = None
    version: str = ""
    errors: list[str] = field(default_factory=list)  # load + setup failures (agent/server)

    @property
    def ok(self) -> bool:
        return self.obj is not None and not self.errors

    def record_error(self, where: str, exc: BaseException) -> None:
        msg = f"{where}: {type(exc).__name__}: {exc}"
        if msg not in self.errors:
            self.errors.append(msg)
        logger.warning("nebi extension %s failed in %s", self.name, msg, exc_info=exc)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "version": self.version, "source": self.source, "ok": self.ok, "errors": list(self.errors)}


_lock = threading.Lock()
_loaded: list[LoadedExtension] | None = None

# Hosts of the extension hooks (nebi_cli, nebi_cockpit, ...) plug in here, so this module never imports them.
ResetHook = Callable[[], None]
ReportHook = Callable[[str], dict[str, Any]]
_reset_hooks: list[ResetHook] = []
_report_hooks: list[ReportHook] = []


def register_reset_hook(fn: ResetHook) -> None:
    """`fn()` runs on every `reset_extensions()` (e.g. the agent drops the command specs extensions registered)."""
    if fn not in _reset_hooks:
        _reset_hooks.append(fn)


def register_report_hook(fn: ReportHook) -> None:
    """`fn(extension_name)` returns extra fields merged into that extension's `extension_report()` entry
    (e.g. ``{"commands": [...]}`` from the agent)."""
    if fn not in _report_hooks:
        _report_hooks.append(fn)


def extensions_disabled() -> bool:
    return os.environ.get(ENV_DISABLE, "").strip().lower() in _TRUE


def disabled_names() -> set[str]:
    """``PYNEBI_DISABLE_EXTENSIONS=a,b``: the extension (or entry-point) names to skip."""
    raw = os.environ.get(ENV_DISABLE, "").strip()
    if not raw or raw.lower() in _TRUE or raw.lower() in ("0", "false", "no", "off"):
        return set()
    return {n.strip() for n in raw.split(",") if n.strip()}


def _instantiate(target: Any) -> Any:
    """An extension object as-is; a class or a zero-argument factory function is called."""
    if inspect.isclass(target) or (inspect.isfunction(target) and not hasattr(target, "name")):
        return target()
    return target


def _wrap(target: Any, *, name: str, source: str) -> LoadedExtension:
    ext = LoadedExtension(name=name, source=source)
    try:
        obj = _instantiate(target)
    except Exception as exc:
        ext.record_error("load", exc)
        return ext
    ext.obj = obj
    ext.name = str(getattr(obj, "name", "") or name)
    ext.version = str(getattr(obj, "version", "") or "")
    return ext


def _resolve_spec(spec: str) -> Any:
    module, _, attr = spec.partition(":")
    obj: Any = importlib.import_module(module.strip())
    for part in (attr.strip() or "extension").split("."):
        obj = getattr(obj, part)
    return obj


def _discover() -> list[LoadedExtension]:
    found: list[LoadedExtension] = []
    try:
        eps: Iterable[metadata.EntryPoint] = metadata.entry_points(group=ENTRY_POINT_GROUP)
    except Exception as exc:  # broken metadata must not break the core
        logger.warning("cannot list %s entry points: %r", ENTRY_POINT_GROUP, exc)
        eps = ()
    skip = disabled_names()
    for ep in sorted(eps, key=lambda e: e.name):
        if ep.name in skip:
            continue
        try:
            target = ep.load()
        except Exception as exc:
            bad = LoadedExtension(name=ep.name, source=f"entry_point:{ep.name}")
            bad.record_error("load", exc)
            found.append(bad)
            continue
        found.append(_wrap(target, name=ep.name, source=f"entry_point:{ep.name}"))
    for spec in (s.strip() for s in os.environ.get(ENV_EXTENSIONS, "").split(",")):
        if not spec:
            continue
        try:
            target = _resolve_spec(spec)
        except Exception as exc:
            bad = LoadedExtension(name=spec, source=f"env:{spec}")
            bad.record_error("load", exc)
            found.append(bad)
            continue
        found.append(_wrap(target, name=spec, source=f"env:{spec}"))
    return [e for e in _dedupe(found) if e.name not in skip]


def _dedupe(found: list[LoadedExtension]) -> list[LoadedExtension]:
    """The same extension reachable twice (entry point + env) is loaded once."""
    out: list[LoadedExtension] = []
    seen: set[str] = set()
    for ext in found:
        if ext.obj is not None and ext.name in seen:
            logger.info("nebi extension %s listed twice (%s); using the first", ext.name, ext.source)
            continue
        seen.add(ext.name)
        out.append(ext)
    return out


def load_extensions(*, reload: bool = False) -> list[LoadedExtension]:
    """All discovered extensions (cached per process), including the ones that failed to load."""
    global _loaded
    with _lock:
        if _loaded is None or reload:
            _loaded = [] if extensions_disabled() else _discover()
        return list(_loaded)


def use_extensions(objs: Iterable[Any]) -> list[LoadedExtension]:
    """Replace the process's extension set with these objects (tests, or an app embedding nebi)."""
    global _loaded
    with _lock:
        _loaded = _dedupe([_wrap(o, name=str(getattr(o, "name", "") or type(o).__name__), source="explicit") for o in objs])
        return list(_loaded)


def reset_extensions() -> None:
    """Forget the loaded set (and any command specs extensions registered); the next load rediscovers."""
    global _loaded
    with _lock:
        _loaded = None
    for fn in list(_reset_hooks):
        fn()


def active_extensions() -> list[LoadedExtension]:
    """Extensions that loaded (they may still have failed a setup hook: see `errors`)."""
    return [e for e in load_extensions() if e.obj is not None]


def extension_report() -> list[dict[str, Any]]:
    """For `nebi status` / `GET /api/extensions`: name, version, source, ok, errors, commands (+ report hooks)."""
    out = []
    for e in load_extensions():
        d = e.to_dict()
        d["commands"] = []
        for fn in list(_report_hooks):
            try:
                d.update(fn(e.name))
            except Exception as exc:  # a broken host hook must not break the report
                logger.warning("extension report hook %r failed: %r", fn, exc)
        out.append(d)
    return out
