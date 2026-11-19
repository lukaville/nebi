"""Zenoh-over-UCP tunnel framing (`0xA4..0xA7`) and the client-side TCP <-> UCP tunnel for `eclipse-zenoh`.

`0xA7` DATA frames carry Zenoh 1.x TCP-stream-framed messages (`[u16 LE len][zenoh_msg]`), so a native
`eclipse-zenoh` session can connect to a local loopback TCP listener and run unchanged over UCP: reliable
messages on Channel 3, unreliable Zenoh Push on Channel 0 + SACK.
"""

from __future__ import annotations

import asyncio
import logging
import random
import struct
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from nebi.exceptions import NebiNotConnectedError
from nebi.transport.ucp import UcpEndpointSession
from nebi.transport.ucp_codec import (
    UCP_JOYSTICK_PAYLOAD_LEN,
    Z_UCP_ACK,
    Z_UCP_CLOSE,
    Z_UCP_DATA,
    Z_UCP_OPEN,
)

logger = logging.getLogger(__name__)

Z_UCP_MTU = 1457  # `0x5b1`, the robot's Zenoh-over-UCP MTU
_TX_BACKPRESSURE_MAX_WAIT = 2.0
_ZENOH_LEN_PREFIX = struct.Struct("<H")  # Zenoh TCP stream framing: u16 LE length before each message


@dataclass(slots=True)
class ZenohUcpFrame:
    frame_type: int  # 0xA4 | 0xA5 | 0xA6 | 0xA7
    attempt_id: int  # full u32 on control frames, low u16 on 0xA7 data frames
    payload: bytes = b""

    def encode(self) -> bytes:
        if self.frame_type in (Z_UCP_OPEN, Z_UCP_ACK, Z_UCP_CLOSE):
            return struct.pack(">BI", self.frame_type, self.attempt_id & 0xFFFFFFFF)
        if self.frame_type == Z_UCP_DATA:
            return struct.pack(">BH", Z_UCP_DATA, self.attempt_id & 0xFFFF) + self.payload
        raise ValueError(f"Unknown Zenoh-over-UCP frame_type: {hex(self.frame_type)}")

    @classmethod
    def decode(cls, raw: bytes) -> ZenohUcpFrame | None:
        if len(raw) < 3:
            return None
        ft = raw[0]
        if ft in (Z_UCP_OPEN, Z_UCP_ACK, Z_UCP_CLOSE):
            if len(raw) < 5:
                return None
            _, aid = struct.unpack_from(">BI", raw, 0)
            return cls(frame_type=ft, attempt_id=aid)
        if ft == Z_UCP_DATA:
            _, aid16 = struct.unpack_from(">BH", raw, 0)
            return cls(frame_type=Z_UCP_DATA, attempt_id=aid16, payload=raw[3:])
        return None


def is_zenoh_unreliable_push(zenoh_framed_chunk: bytes) -> bool:
    """Check if a `[u16 LE len][zenoh_hdr...]` frame is an unreliable Zenoh Frame/Push."""
    if len(zenoh_framed_chunk) < 3:
        return False
    hdr = zenoh_framed_chunk[2]
    msg_id = hdr & 0x1F
    # Unreliable when (hdr & 0x20) == 0 and (hdr & 0x1F) in {5, 6}
    return msg_id in (0x05, 0x06) and not bool(hdr & 0x20)


def send_data_frame(ucp: UcpEndpointSession, zframe: bytes, reliable: bool) -> None:
    """Send an `0xA7` frame, never emitting an 8-byte unreliable payload (robot would read it as joystick)."""
    if not reliable and len(zframe) == UCP_JOYSTICK_PAYLOAD_LEN:
        reliable = True
    ucp.send_zenoh_frame(zframe, reliable=reliable)


async def await_tx_window(get_ucp: Callable[[], UcpEndpointSession | None]) -> None:
    """Backpressure: pause the TCP->UCP pump while the UCP reliable send window is full (bounded wait)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _TX_BACKPRESSURE_MAX_WAIT
    while loop.time() < deadline:
        ucp = get_ucp()
        if ucp is None or not ucp.tx_window_full:
            return
        await asyncio.sleep(0.01)
    logger.debug("UCP tx window still full after %.1fs; sending anyway", _TX_BACKPRESSURE_MAX_WAIT)


async def read_zenoh_stream_message(reader: asyncio.StreamReader) -> bytes:
    """Read one length-prefixed Zenoh TCP-stream message, returned WITH its 2-byte length prefix."""
    len_hdr = await reader.readexactly(_ZENOH_LEN_PREFIX.size)
    (frame_len,) = _ZENOH_LEN_PREFIX.unpack(len_hdr)
    return len_hdr + await reader.readexactly(frame_len)


def log_task_exception(task: asyncio.Task[Any]) -> None:
    """Done-callback that logs (never swallows) a background task's failure."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning("Background Zenoh task %s failed: %r", task.get_name(), exc, exc_info=exc)


class ZenohUcpClientTunnel:
    """Client-side Zenoh-over-UCP tunnel state machine + local TCP listener for `eclipse-zenoh`.

    Handshake order:
      1. Client sends `0xA4` (`OPEN`, `attempt_id`) on UCP Channel 3 (`reliable=True`).
      2. Client immediately opens its Zenoh session (`0xA7` `InitSyn` <-> `InitAck` / `OpenSyn` <-> `OpenAck`),
         allowing the robot's Zenoh server side to complete its open and declare `app/**`.
      3. Once local Zenoh + `device/**` subscriber are open, Client sends `0xA5` (`ACK` / `PEER_READY`,
         `attempt_id`) and awaits the robot's `0xA5` (`ACK` / `PEER_READY`, `attempt_id`).
    """

    def __init__(self, ucp_session_getter: Callable[[], UcpEndpointSession | None]) -> None:
        self._get_ucp = ucp_session_getter
        self.attempt_id: int = random.randint(1, 0xFFFE)
        self._ack_event = asyncio.Event()
        self._active = False
        self._ready = False
        self._attempt_started_once = False
        self._tcp_server: asyncio.Server | None = None
        self._tcp_writer: asyncio.StreamWriter | None = None
        self._pending_rx: list[bytes] = []
        self.local_port: int = 0

    @property
    def ready(self) -> bool:
        return self._ready

    def on_ucp_zenoh_frame(self, raw: bytes) -> None:
        frame = ZenohUcpFrame.decode(raw)
        if frame is None:
            return
        if frame.frame_type == Z_UCP_ACK and frame.attempt_id == self.attempt_id:
            self._ready = True
            self._ack_event.set()
        elif frame.frame_type == Z_UCP_CLOSE and frame.attempt_id == self.attempt_id:
            self._active = False
            self._ready = False
            if self._tcp_writer is not None and not self._tcp_writer.is_closing():
                self._tcp_writer.close()
        elif (
            self._active
            and frame.frame_type == Z_UCP_DATA
            and frame.attempt_id == (self.attempt_id & 0xFFFF)
        ):
            if self._tcp_writer is not None and not self._tcp_writer.is_closing():
                self._tcp_writer.write(frame.payload)
            else:
                self._pending_rx.append(frame.payload)

    async def start_link(self) -> str:
        """Start local TCP bridge and transmit `0xA4` (`OPEN`) so `zenoh.open` can immediately exchange `0xA7`."""
        if self._tcp_writer is not None:
            self._tcp_writer.close()
            self._tcp_writer = None
        if self._tcp_server is not None:
            self._tcp_server.close()
            await self._tcp_server.wait_closed()
            self._tcp_server = None

        if self._attempt_started_once:
            self.attempt_id = (self.attempt_id % 0xFFFE) + 1
        self._attempt_started_once = True

        self._pending_rx.clear()
        self._ack_event.clear()
        self._ready = False
        self._active = True

        self._tcp_server = await asyncio.start_server(self._handle_local_zenoh_tcp, "127.0.0.1", 0)
        sock = self._tcp_server.sockets[0]
        self.local_port = int(sock.getsockname()[1])

        ucp = self._get_ucp()
        if ucp is None:
            raise NebiNotConnectedError("UCP session not active.", stage="zenoh_tunneling")

        open_bytes = ZenohUcpFrame(Z_UCP_OPEN, self.attempt_id).encode()
        ucp.send_zenoh_frame(open_bytes, reliable=True)
        return f"tcp/127.0.0.1:{self.local_port}"

    async def complete_ready_barrier(self, timeout: float = 3.0) -> None:
        """Send client `0xA5` (`PEER_READY`) after subscriber setup and wait for the robot's `0xA5`."""
        ucp = self._get_ucp()
        if ucp is None:
            raise NebiNotConnectedError("UCP session not active.", stage="zenoh_tunneling")

        ack_bytes = ZenohUcpFrame(Z_UCP_ACK, self.attempt_id).encode()
        ucp.send_zenoh_frame(ack_bytes, reliable=True)

        if not self._ready:
            try:
                await asyncio.wait_for(self._ack_event.wait(), timeout=timeout)
            except asyncio.TimeoutError as exc:
                raise TimeoutError("Zenoh-over-UCP tunnel 0xA4->0xA5 link ready barrier timed out") from exc

    async def _handle_local_zenoh_tcp(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._tcp_writer = writer
        for item in self._pending_rx:
            writer.write(item)
        self._pending_rx.clear()
        await writer.drain()

        try:
            while True:
                full_zenoh_pkt = await read_zenoh_stream_message(reader)
                reliable = not is_zenoh_unreliable_push(full_zenoh_pkt)
                if reliable:
                    await await_tx_window(self._get_ucp)
                ucp = self._get_ucp()
                if ucp is not None and self._active:
                    zframe = ZenohUcpFrame(Z_UCP_DATA, self.attempt_id & 0xFFFF, full_zenoh_pkt).encode()
                    send_data_frame(ucp, zframe, reliable)
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.CancelledError):
            pass  # local Zenoh closed the stream or the tunnel is shutting down
        finally:
            writer.close()

    async def close(self) -> None:
        if self._active:
            ucp = self._get_ucp()
            if ucp is not None:
                ucp.send_zenoh_frame(ZenohUcpFrame(Z_UCP_CLOSE, self.attempt_id).encode(), reliable=True)
        self._active = False
        self._ready = False
        self._pending_rx.clear()
        if self._tcp_writer is not None:
            self._tcp_writer.close()
            self._tcp_writer = None
        if self._tcp_server is not None:
            self._tcp_server.close()
            await self._tcp_server.wait_closed()
            self._tcp_server = None
