"""FastAPI dependencies: the app's `NebiServerManager` and its connected robot client.

`create_app()` stores the manager on `app.state.manager`; routes declare what they need instead of
closing over it:

    @router.post("/stand")
    async def stand(client: ClientDep) -> dict[str, Any]: ...
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, HTTPException
from starlette.requests import HTTPConnection

from nebi.client import AsyncNebiClient
from nebi_cockpit.connection_manager import NebiServerManager
from nebi_cockpit.intercom_ws import IntercomHub

if TYPE_CHECKING:
    from nebi.simulator import MockNebiRobot


def get_manager(conn: HTTPConnection) -> NebiServerManager:
    """The app's manager (works for HTTP routes and WebSockets)."""
    manager: NebiServerManager = conn.app.state.manager
    return manager


def get_client(conn: HTTPConnection) -> AsyncNebiClient:
    """The connected robot client; raises `NebiNotConnectedError` (-> 503 JSON) when there is none."""
    return get_manager(conn).require_client()


def get_simulator(conn: HTTPConnection) -> MockNebiRobot:
    """The running simulator, for the simulator-only QA routes (400 when it is not active)."""
    sim = get_manager(conn).simulator
    if sim is None:
        raise HTTPException(status_code=400, detail="Simulator is not active")
    return sim


def get_intercom_hub(conn: HTTPConnection) -> IntercomHub:
    hub: IntercomHub = conn.app.state.intercom_hub
    return hub


ManagerDep = Annotated[NebiServerManager, Depends(get_manager)]
ClientDep = Annotated[AsyncNebiClient, Depends(get_client)]
SimulatorDep = Annotated["MockNebiRobot", Depends(get_simulator)]
IntercomHubDep = Annotated[IntercomHub, Depends(get_intercom_hub)]
