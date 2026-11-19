"""Backwards-compatible facade for the Zenoh-over-UCP stack.

The implementation is split by concern (see `nebi.transport`):
  - `zenoh_tunnel`   Zenoh-over-UCP (`0xA4..0xA7`) framing + client TCP <-> UCP tunnel
  - `zenoh_security` MD-TS negotiation over `app|device/security/session`
  - `zenoh_bus`      `ZenohTopicBus` Protobuf pub/sub + RPC
  - `zenoh_proto`    app-bridge Protobuf normalisation
The robot-side `ZenohUcpServerTunnel` lives in `nebi.sim.zenoh` (resolved lazily here).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from nebi.transport.ucp_codec import Z_UCP_ACK, Z_UCP_CLOSE, Z_UCP_DATA, Z_UCP_OPEN
from nebi.transport.zenoh_bus import HEADERLESS_TOPICS, TopicCallback, ZenohTopicBus
from nebi.transport.zenoh_proto import (
    _normalize_proto_for_videotrans,
    fill_message_data,
    normalize_proto_for_videotrans,
)
from nebi.transport.zenoh_security import ZenohMdTsResponder, negotiate_client_session
from nebi.transport.zenoh_tunnel import (
    Z_UCP_MTU,
    ZenohUcpClientTunnel,
    ZenohUcpFrame,
    is_zenoh_unreliable_push,
)

if TYPE_CHECKING:
    from nebi.sim.zenoh import ZenohUcpServerTunnel

__all__ = [
    "HEADERLESS_TOPICS",
    "Z_UCP_ACK",
    "Z_UCP_CLOSE",
    "Z_UCP_DATA",
    "Z_UCP_MTU",
    "Z_UCP_OPEN",
    "TopicCallback",
    "ZenohMdTsResponder",
    "ZenohTopicBus",
    "ZenohUcpClientTunnel",
    "ZenohUcpFrame",
    "ZenohUcpServerTunnel",
    "_normalize_proto_for_videotrans",
    "fill_message_data",
    "is_zenoh_unreliable_push",
    "negotiate_client_session",
    "normalize_proto_for_videotrans",
]


def __getattr__(name: str) -> Any:
    # Simulator-only robot-side tunnel, resolved lazily so production imports never load `nebi.sim`.
    if name == "ZenohUcpServerTunnel":
        from nebi.sim.zenoh import ZenohUcpServerTunnel

        return ZenohUcpServerTunnel
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
