"""Common base and response-decoding helpers for the domain controllers."""

from __future__ import annotations

import socket
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nebi.transport.zenoh_ucp import ZenohTopicBus


class DomainController:
    """Base for the controllers hung off `AsyncNebiClient` (`robot.motion`, `robot.camera`, ...).

    Controllers are created once per client and survive transport rebuilds (reconnect, Wi-Fi handoff): the client
    calls `rebind()` with the new bus, so state they hold (joystick latch, LED task, game session) and references
    callers keep (`m = robot.motion`) never go stale.
    """

    def __init__(self, bus: ZenohTopicBus) -> None:
        self._bus = bus

    def rebind(self, bus: ZenohTopicBus) -> None:
        """Point at a rebuilt bus after reconnect/handoff."""
        self._bus = bus

    async def _request_ints(
        self, req_topic: str, rsp_topic: str, fields: tuple[str, ...], *, timeout: float, **kwargs: Any
    ) -> dict[str, int]:
        """`bus.request()` and return the named integer fields of the response's `data` sub-message."""
        rsp = await self._bus.request(req_topic, rsp_topic, timeout=timeout, **kwargs)
        return int_fields(rsp.data, *fields)


def int_fields(data: Any, *names: str) -> dict[str, int]:
    """`{name: int(data.name)}` for each name (the usual shape of a firmware ack)."""
    return {name: int(getattr(data, name)) for name in names}


def proto_fields(msg: Any) -> dict[str, Any]:
    """All set fields of a protobuf message as a dict."""
    return {f.name: getattr(msg, f.name) for f, _ in msg.ListFields()}


def decode_fixed_str(raw: Any, length: int = 0, *, encoding: str = "utf-8") -> str:
    """Decode a firmware fixed-size char array (`raw[:length]`, or all of it when `length` is 0), NUL-trimmed."""
    data = bytes(raw[: length or len(raw)])
    return data.decode(encoding, errors="replace").rstrip("\x00")


def decode_ipv4_field(raw: bytes) -> str:
    """Firmware IPv4 fields are either 4 packed bytes or an ASCII dotted quad; return the dotted quad or ''."""
    clean = bytes(raw).rstrip(b"\x00")
    if not clean:
        return ""
    if len(clean) == 4 and any(b < 0x20 or b > 0x7E for b in clean):
        return socket.inet_ntoa(clean)
    txt = clean.decode("ascii", errors="ignore").strip()
    if "." in txt:
        return txt
    if len(raw) >= 4 and any(raw[:4]):
        return socket.inet_ntoa(bytes(raw[:4]))
    return ""
