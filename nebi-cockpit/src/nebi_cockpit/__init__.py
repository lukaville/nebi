"""nebi-cockpit: FastAPI server, H.265 video pipeline and web cockpit UI for the Nebi robot (built on `nebi`)."""

from nebi_cockpit.app import NebiServerManager, create_app

__all__ = ["NebiServerManager", "create_app"]
