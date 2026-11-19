"""Robot-side Zenoh-over-UCP tunnel responder used by `MockNebiRobot`."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from nebi.transport.ucp import UcpEndpointSession
from nebi.transport.ucp_codec import Z_UCP_ACK, Z_UCP_CLOSE, Z_UCP_DATA, Z_UCP_OPEN
from nebi.transport.zenoh_tunnel import (
    ZenohUcpFrame,
    is_zenoh_unreliable_push,
    log_task_exception,
    read_zenoh_stream_message,
    send_data_frame,
)

logger = logging.getLogger(__name__)


class ZenohUcpServerTunnel:
    """Robot-side Zenoh-over-UCP tunnel responder forwarding `0xA7` frames into a local Zenoh TCP listener."""

    def __init__(
        self,
        ucp_session_getter: Callable[[], UcpEndpointSession | None],
        zenoh_listen_port: int,
        on_tunnel_reset: Callable[[], None] | None = None,
    ) -> None:
        self._get_ucp = ucp_session_getter
        self._zenoh_port = zenoh_listen_port
        self._on_tunnel_reset = on_tunnel_reset
        self.attempt_id: int = 0
        self._tcp_writer: asyncio.StreamWriter | None = None
        self._pump_task: asyncio.Task[None] | None = None
        self._pending_to_zenoh: asyncio.Queue[bytes] = asyncio.Queue()
        self._ack_sent: bool = False

    def on_ucp_zenoh_frame(self, raw: bytes) -> None:
        frame = ZenohUcpFrame.decode(raw)
        if frame is None:
            return
        ucp = self._get_ucp()
        if frame.frame_type == Z_UCP_OPEN:
            if frame.attempt_id != self.attempt_id or self._pump_task is None or self._pump_task.done():
                self.attempt_id = frame.attempt_id
                self._ack_sent = False
                if self._on_tunnel_reset is not None:
                    self._on_tunnel_reset()
                self._restart_tcp_bridge()
            elif self._ack_sent and ucp is not None:
                ucp.send_zenoh_frame(ZenohUcpFrame(Z_UCP_ACK, self.attempt_id).encode(), reliable=True)
        elif frame.frame_type == Z_UCP_CLOSE and frame.attempt_id == self.attempt_id:
            self._ack_sent = False
            if self._on_tunnel_reset is not None:
                self._on_tunnel_reset()
            self._stop_bridge()
        elif frame.frame_type == Z_UCP_DATA and frame.attempt_id == (self.attempt_id & 0xFFFF):
            self._pending_to_zenoh.put_nowait(frame.payload)

    def _restart_tcp_bridge(self) -> None:
        self._stop_bridge()
        self._pending_to_zenoh = asyncio.Queue()
        self._pump_task = asyncio.create_task(self._bridge_loop(self.attempt_id), name="zenoh-ucp-server-bridge")
        self._pump_task.add_done_callback(log_task_exception)

    def _stop_bridge(self) -> None:
        if self._pump_task is not None:
            self._pump_task.cancel()
            self._pump_task = None
        if self._tcp_writer is not None:
            self._tcp_writer.close()
            self._tcp_writer = None

    async def _bridge_loop(self, active_attempt: int) -> None:
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", self._zenoh_port)
        except OSError as exc:
            logger.warning("Zenoh-over-UCP server bridge could not reach local Zenoh on :%d: %s", self._zenoh_port, exc)
            return
        self._tcp_writer = writer

        async def _tx_to_zenoh() -> None:
            while True:
                chunk = await self._pending_to_zenoh.get()
                writer.write(chunk)
                await writer.drain()

        async def _rx_from_zenoh() -> None:
            rx_frames = 0
            while True:
                pkt = await read_zenoh_stream_message(reader)
                rx_frames += 1
                ucp = self._get_ucp()
                if ucp is not None and self.attempt_id == active_attempt:
                    zframe = ZenohUcpFrame(Z_UCP_DATA, active_attempt & 0xFFFF, pkt).encode()
                    reliable = not is_zenoh_unreliable_push(pkt)
                    send_data_frame(ucp, zframe, reliable)
                    # Like the robot: send the 0xA5 ACK only AFTER Zenoh InitAck + OpenAck!
                    if rx_frames >= 2 and not self._ack_sent:
                        self._ack_sent = True
                        ucp.send_zenoh_frame(ZenohUcpFrame(Z_UCP_ACK, active_attempt).encode(), reliable=True)

        tx_t = asyncio.create_task(_tx_to_zenoh(), name="zenoh-ucp-server-tx")
        rx_t = asyncio.create_task(_rx_from_zenoh(), name="zenoh-ucp-server-rx")
        try:
            await asyncio.gather(tx_t, rx_t)
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.CancelledError):
            pass  # peer closed or bridge restarted; both pumps are cancelled below
        finally:
            tx_t.cancel()
            rx_t.cancel()
            writer.close()

    async def close(self) -> None:
        self._stop_bridge()
