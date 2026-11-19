"""Robot-side UCP v1 UDP server used by `MockNebiRobot` and tests."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable

from nebi.transport.ucp import (
    BytesCallback,
    JoystickWordCallback,
    UcpEndpointSession,
    VideoFrameCallback,
)
from nebi.transport.ucp_codec import UCP_ACK, UCP_LEAV, UCP_SYNC

logger = logging.getLogger(__name__)


class _ServerUdpProtocol(asyncio.DatagramProtocol):
    def __init__(self, server: UcpServer) -> None:
        self._server = server

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self._server.handle_udp_datagram(data, addr)


class UcpServer:
    """Async UCP v1 server (robot side) used by `MockNebiRobot` and tests."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 19007,
        *,
        on_video_frame: VideoFrameCallback | None = None,
        on_audio_frame: BytesCallback | None = None,
        on_zenoh_frame: BytesCallback | None = None,
        on_joystick_frame: JoystickWordCallback | None = None,
        on_peer_connected: Callable[[], None] | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self._transport: asyncio.DatagramTransport | None = None
        self._peer_addr: tuple[str, int] | None = None
        self._maint_task: asyncio.Task[None] | None = None
        self._on_video_frame = on_video_frame
        self._on_audio_frame = on_audio_frame
        self._on_zenoh_frame = on_zenoh_frame
        self._on_joystick_frame = on_joystick_frame
        self._on_peer_connected = on_peer_connected
        self.session: UcpEndpointSession | None = None
        self.drop_packets_until: float = 0.0

    def simulate_blackout(self, duration_sec: float) -> None:
        """Drop all inbound/outbound UDP datagrams for `duration_sec` to test link watchdog & auto-reconnect."""
        self.drop_packets_until = time.monotonic() + max(0.0, duration_sec)

    @property
    def bound_port(self) -> int:
        if self._transport is not None:
            sockname = self._transport.get_extra_info("sockname")
            if sockname:
                return int(sockname[1])
        return self.port

    def _send_to_peer(self, data: bytes) -> None:
        if time.monotonic() < self.drop_packets_until:
            return
        if self._transport is not None and self._peer_addr is not None and not self._transport.is_closing():
            self._transport.sendto(data, self._peer_addr)

    def handle_udp_datagram(self, data: bytes, addr: tuple[str, int]) -> None:
        """Process one inbound datagram from `addr` (SYNC/LEAV control or UCP segments of the current peer)."""
        if time.monotonic() < self.drop_packets_until:
            return
        if data == UCP_SYNC or data.startswith(UCP_SYNC):
            is_new = self._peer_addr != addr or self.session is None
            self._peer_addr = addr
            if is_new:
                self.session = UcpEndpointSession(
                    send_datagram=self._send_to_peer,
                    on_video_frame=self._on_video_frame,
                    on_audio_frame=self._on_audio_frame,
                    on_zenoh_frame=self._on_zenoh_frame,
                    on_joystick_frame=self._on_joystick_frame,
                )
                if self._on_peer_connected is not None:
                    self._on_peer_connected()
            self._send_to_peer(UCP_ACK)
            return
        if data == UCP_LEAV:
            if self._peer_addr == addr:
                self._peer_addr = None
                self.session = None
            return
        if self.session is not None and self._peer_addr == addr:
            self.session.handle_datagram(data)

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(
            lambda: _ServerUdpProtocol(self),
            local_addr=(self.host, self.port),
        )
        self._transport = transport
        self._maint_task = asyncio.create_task(self._maintenance_loop(), name="ucp-server-maintenance")

    async def _maintenance_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(0.05)
                if self.session is not None:
                    self.session.tick_maintenance()
        except asyncio.CancelledError:
            return

    async def stop(self) -> None:
        if self._maint_task is not None:
            self._maint_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._maint_task
            self._maint_task = None
        if self._transport is not None:
            if self._peer_addr is not None:
                try:
                    self._send_to_peer(UCP_LEAV)
                except OSError as exc:  # best-effort goodbye; the socket is closed right after
                    logger.debug("UCP server LEAV send failed: %s", exc)
            self._transport.close()
            self._transport = None
        self.session = None
        self._peer_addr = None
