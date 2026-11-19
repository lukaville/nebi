"""nebi-cli's cockpit extension (entry point ``nebi-cli`` in group ``nebi.extensions``).

When nebi-cli is installed, every `nebi-cockpit serve` hosts the `nebi` agent API (`/agent/call`, `/agent/ping`,
`/agent/activity`), so the `nebi` CLI drives through the user's cockpit and its single robot link, and the
cockpit's "agent driving" pill lights up. `nebi-cockpit serve --disable-extension nebi-cli` turns it off.

Importing this module is cheap (the `nebi` CLI process loads every extension to build its parser): the server
side is imported only inside ``setup_server``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nebi_cockpit.extensions import ServerExtensionContext

__all__ = ["AgentApiExtension", "extension"]


class AgentApiExtension:
    name = "nebi-cli"

    @property
    def version(self) -> str:
        from nebi_cli import __version__

        return __version__

    def setup_server(self, ctx: ServerExtensionContext) -> None:
        from nebi_cli.daemon import setup_embedded

        setup_embedded(ctx)


extension = AgentApiExtension()
