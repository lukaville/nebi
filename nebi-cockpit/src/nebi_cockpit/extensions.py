"""Server-side extension API: routes, static files, cockpit UI modules, allowed origins and lifespans.

An extension (see ``nebi.extensions``: discovered via the ``nebi.extensions`` entry-point group) gets its
``setup_server(ctx)`` called once per cockpit app (``create_app``). The cockpit page asks
``GET /api/extensions`` for the loaded extensions and their UI modules and imports each module URL (ES module);
the module adds its tab / view through ``window.NebiUI`` (see ``static/js/extensions.js``):

    NebiUI.registerTab({id, title, icon, html, onShow})   -> side-panel tab (pane element id `tab-<id>`)
    NebiUI.registerView({id, label, element, onShow, onHide})  -> video-stage view (camera is the built-in one)
    NebiUI.selectView(id) / NebiUI.currentView() / NebiUI.toast(msg, kind) / NebiUI.isTyping() / NebiUI.selectTab(id)

A failing ``setup_server`` is logged and reported in ``/api/extensions``; it never breaks the cockpit.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager, AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nebi.extension_loader import active_extensions, extension_report, load_extensions

if TYPE_CHECKING:
    from fastapi import APIRouter, FastAPI

__all__ = [
    "LifespanFactory",
    "ServerExtensionContext",
    "UiModule",
    "enter_extension_lifespans",
    "extensions_payload",
    "setup_server_extensions",
]

logger = logging.getLogger(__name__)

LifespanFactory = Callable[["FastAPI"], AbstractAsyncContextManager[Any]]
"""``factory(app)`` -> async context manager entered after the cockpit's robot link starts, exited before it stops."""


@dataclass(frozen=True)
class UiModule:
    """An ES module the cockpit page imports, plus optional stylesheets loaded before it."""

    extension: str
    url: str
    styles: tuple[str, ...] = ()


@dataclass
class ServerExtensionState:
    ui_modules: list[UiModule] = field(default_factory=list)
    origins: set[str] = field(default_factory=set)
    lifespans: list[tuple[str, LifespanFactory]] = field(default_factory=list)


class ServerExtensionContext:
    """Passed to ``NebiExtension.setup_server``."""

    def __init__(self, extension: str, app: FastAPI) -> None:
        self.extension = extension
        self.app = app

    @property
    def _state(self) -> ServerExtensionState:
        return server_state(self.app)

    @property
    def port(self) -> int | None:
        """The port the cockpit is served on, when the launcher said so (``nebi-cockpit serve`` does)."""
        return getattr(self.app.state, "port", None)

    @property
    def manager(self) -> Any:
        """The cockpit's `NebiServerManager` (the single robot link, simulator and video hub)."""
        return self.app.state.manager

    def add_lifespan(self, factory: LifespanFactory) -> None:
        """Run ``factory(app)`` (an async context manager) for the app's lifetime, inside the robot link's.
        May also be called after ``create_app`` returned, as long as the app has not started yet."""
        self._state.lifespans.append((self.extension, factory))

    def include_router(self, router: APIRouter, **kwargs: Any) -> None:
        """Add FastAPI routes (``prefix=``, ``tags=`` ... as for ``FastAPI.include_router``)."""
        self.app.include_router(router, **kwargs)

    def mount_static(self, path: str, directory: str | Path, *, name: str | None = None) -> None:
        """Serve `directory` at `path` (e.g. ``/ext/<name>``), with the cockpit's no-cache policy."""
        from nebi_cockpit.app import CockpitStaticFiles

        self.app.mount(path, CockpitStaticFiles(directory=str(directory)), name=name or f"ext-{self.extension}")

    def add_ui_module(self, url: str, *, styles: tuple[str, ...] | list[str] = ()) -> None:
        """Have the cockpit page import this ES module URL (and load these stylesheets first)."""
        self._state.ui_modules.append(UiModule(self.extension, url, tuple(styles)))

    def add_allowed_origins(self, *origins: str) -> None:
        """Origins (``scheme://host:port``) whose browser pages may POST / open WebSockets to the cockpit."""
        self._state.origins.update(origins)
        security = getattr(self.app.state, "security", None)
        if security is not None:
            security.allowed_origins.update(origins)


def server_state(app: FastAPI) -> ServerExtensionState:
    st = getattr(app.state, "extensions", None)
    if st is None:
        st = ServerExtensionState()
        app.state.extensions = st
    return st


def setup_server_extensions(app: FastAPI) -> None:
    server_state(app)
    for ext in active_extensions():
        setup = getattr(ext.obj, "setup_server", None)
        if setup is None:
            continue
        st = server_state(app)
        before, before_lifespans = len(st.ui_modules), len(st.lifespans)
        try:
            setup(ServerExtensionContext(ext.name, app))
        except Exception as exc:  # routes added before the failure stay; UI modules / lifespans of a broken one don't run
            del st.ui_modules[before:]
            del st.lifespans[before_lifespans:]
            ext.record_error("setup_server", exc)


async def enter_extension_lifespans(app: FastAPI, stack: AsyncExitStack) -> None:
    """Enter every registered extension lifespan on `stack`; one that fails to start is reported, not fatal."""
    for name, factory in list(server_state(app).lifespans):
        try:
            await stack.enter_async_context(factory(app))
        except Exception as exc:
            ext = next((e for e in load_extensions() if e.name == name), None)
            if ext is not None:
                ext.record_error("lifespan", exc)
            else:
                logger.warning("cockpit extension %s failed to start", name, exc_info=exc)


def extensions_payload(app: FastAPI) -> dict[str, Any]:
    """`GET /api/extensions`."""
    st = server_state(app)
    exts = extension_report()
    for e in exts:
        mods = [m for m in st.ui_modules if m.extension == e["name"]]
        e["ui_modules"] = [m.url for m in mods]
        e["styles"] = [s for m in mods for s in m.styles]
    return {"extensions": exts}
