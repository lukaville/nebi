"""Generic extension API of nebi: discovery, loading and the shape of an extension object.

An extension is any object with a ``name`` and some of these optional members::

    class MyExtension:
        name = "hello"
        version = "0.1.0"

        def setup_agent(self, ctx) -> None: ...     # `nebi` commands + daemon hooks (nebi_cli.extensions.AgentExtensionContext)
        def setup_server(self, ctx) -> None: ...    # cockpit routes, static files, UI (nebi_cockpit.extensions.ServerExtensionContext)
        def guide_markdown(self) -> str | None: ... # appended to `nebi guide`

    extension = MyExtension()

Install it with an entry point (``pyproject.toml``)::

    [project.entry-points."nebi.extensions"]
    hello = "my_package:extension"

or, while developing, ``PYNEBI_EXTENSIONS="my_package:extension"``. ``PYNEBI_DISABLE_EXTENSIONS=1`` turns every
extension off (``PYNEBI_DISABLE_EXTENSIONS=name1,name2`` only those). Loading or setup failures are logged and shown
in ``nebi status`` and ``GET /api/extensions``; they never break the core.

nebi itself only discovers and loads extensions; the hosts that call the hooks live in the packages that
implement them: ``nebi_cli.extensions`` (agent hooks, plus everything a command extension needs: ``Cmd``, ``Arg``,
``command``, ``AgentError`` ...) and ``nebi_cockpit.extensions`` (server/UI). nebi never imports either.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

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
from nebi.tts import SpeechAudio, TtsEngine, TtsError

__all__ = [
    "ENTRY_POINT_GROUP",
    "ENV_DISABLE",
    "ENV_EXTENSIONS",
    "LoadedExtension",
    "NebiExtension",
    "SpeechAudio",
    "TtsEngine",
    "TtsError",
    "active_extensions",
    "extension_report",
    "load_extensions",
    "register_report_hook",
    "register_reset_hook",
    "reset_extensions",
    "use_extensions",
]


@runtime_checkable
class NebiExtension(Protocol):
    """What a nebi extension looks like. Only ``name`` is required; ``version``, ``setup_agent(ctx)``,
    ``setup_server(ctx)`` and ``guide_markdown()`` are optional and looked up by name (a Protocol cannot express
    optional methods, so implement just the ones you need)."""

    name: str
