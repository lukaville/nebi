"""Browser <-> robot intercom bridge for the Web Cockpit (`/ws/intercom`).

The browser exchanges plain PCM (s16le, 48 kHz, mono) with the server and the server does the Opus
work with `nebi.intercom` (PyAV/libopus). That keeps the browser side to two small AudioWorklets,
works in every browser (no WebCodecs `AudioEncoder` needed), and costs ~768 kbit/s per direction,
which is fine on localhost / LAN.

Protocol
--------
server -> browser
  text   {"type": "hello", "sample_rate": 48000, "frame_samples": 960, "format": "s16le"}
  text   {"type": "state", "active": bool, "talking": bool, "connected": bool, "rx_frames": int, ...}
  text   {"type": "error", "message": str}
  binary robot microphone PCM, one 20 ms frame (1920 bytes) per message
browser -> server
  text   {"type": "start"}                   robot mic on (START_RECORD) + stream it to this browser
  text   {"type": "stop"}                    leave the intercom (robot mic off when no browser is left)
  text   {"type": "talk", "on": bool}        push-to-talk edge; "on" starts a new downlink session (seq 0),
                                             interrupts TTS and holds the speaker (`/api/voice/say` -> 409) until "off"
  binary browser microphone PCM, any length (re-framed into 20 ms Opus packets server-side)
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from fastapi import WebSocket, WebSocketDisconnect

from nebi.intercom import INTERCOM_FRAME_SAMPLES, INTERCOM_SAMPLE_RATE

if TYPE_CHECKING:
    from nebi.client import AsyncNebiClient
    from nebi.speaker import Speaker

logger = logging.getLogger(__name__)

_RX_QUEUE_FRAMES = 15  # 300 ms of robot audio buffered per browser before dropping the oldest
_STATE_PERIOD_S = 0.5  # state push + client-switch follow-up
PCM_FORMAT = "s16le"

GetClient = Callable[[], "AsyncNebiClient | None"]


class IntercomHub:
    """Shares the robot's single intercom between any number of browser tabs."""

    def __init__(self, get_client: GetClient) -> None:
        self._get_client = get_client
        self._members: set[int] = set()  # websocket sessions that pressed "start"
        self._talker: int | None = None
        self._speaker: Speaker | None = None  # the speaker arbiter the current talker holds the mic on
        self._lock = asyncio.Lock()

    async def join(self, member: int) -> dict[str, Any]:
        async with self._lock:
            client = self._require()
            self._members.add(member)
            if not client.intercom.active:
                return await client.intercom.start()
            client.intercom.speaker.adopt_session()  # a speech-opened session now belongs to the intercom too
            return {"operation": "START_RECORD", "ret_code": 0, "shared": True}

    async def leave(self, member: int) -> None:
        async with self._lock:
            self._members.discard(member)
            if self._talker == member:
                self._talker = None
                self._release_speaker(member)
            client = self._get_client()
            if self._members or client is None or not client.intercom.active:
                return
            if client.intercom.speaker.release_session():
                return  # speech still playing: the speaker closes the session after it
            if client.connected:
                try:
                    await client.intercom.stop()
                except Exception as exc:  # robot gone mid-stop: nothing left to release
                    logger.debug("intercom stop on last leave failed: %s", exc)
            else:
                client.intercom.detach()

    def set_talking(self, member: int, on: bool) -> bool:
        client = self._get_client()
        if on:
            if self._talker not in (None, member):
                return False  # one talker at a time; the robot has one speaker stream
            self._talker = member
            if client is not None:
                # a person talking wins over TTS: interrupts speech, refuses new speech until released
                self._release_speaker(member)
                client.intercom.speaker.hold_mic(self._holder(member), True)
                self._speaker = client.intercom.speaker
                client.intercom.new_talk_session()
        elif self._talker == member:
            self._talker = None
            self._release_speaker(member)
        return True

    @staticmethod
    def _holder(member: int) -> str:
        return f"intercom:{member}"

    def _release_speaker(self, member: int) -> None:
        """Release the speaker this member holds (on the client it was taken on, even after a robot switch)."""
        if self._speaker is not None:
            self._speaker.hold_mic(self._holder(member), False)
            self._speaker = None

    def is_talker(self, member: int) -> bool:
        return self._talker == member

    def _require(self) -> AsyncNebiClient:
        client = self._get_client()
        if client is None or not client.connected:
            raise RuntimeError("Robot not connected")
        return client


class IntercomSession:
    """One `/ws/intercom` browser connection: uplink PCM fan-in, robot PCM fan-out, state pushes."""

    def __init__(self, ws: WebSocket, hub: IntercomHub, get_client: GetClient) -> None:
        self.ws = ws
        self.hub = hub
        self._get_client = get_client
        self.member = id(ws)
        self.joined = False
        self._rx_q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=_RX_QUEUE_FRAMES)
        self._bound_client: AsyncNebiClient | None = None
        self._unsub: Callable[[], None] | None = None

    # ------------------------------------------------------------------ robot PCM listener

    def _on_robot_pcm(self, pcm: bytes) -> None:
        if self._rx_q.full():  # keep latency bounded: drop the oldest frame
            with contextlib.suppress(asyncio.QueueEmpty):
                self._rx_q.get_nowait()
        self._rx_q.put_nowait(pcm)

    def _bind_listener(self) -> None:
        """(Re)attach to whatever client the manager holds now (robot switch, reconnect, handoff)."""
        client = self._get_client() if self.joined else None
        if client is self._bound_client and (self._unsub is not None or client is None):
            return
        if self._unsub is not None:
            self._unsub()
            self._unsub = None
        self._bound_client = client
        if client is not None:
            self._unsub = client.intercom.add_pcm_listener(self._on_robot_pcm)

    def _unbind_listener(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None
        self._bound_client = None
        while not self._rx_q.empty():
            self._rx_q.get_nowait()

    # ------------------------------------------------------------------ messages

    def _state(self) -> dict[str, Any]:
        client = self._get_client()
        st = client.intercom.status() if client is not None else {}
        return {
            "type": "state",
            "connected": bool(client is not None and client.connected),
            "joined": self.joined,
            "talking": self.hub.is_talker(self.member),
            **st,
        }

    async def _send_json(self, obj: dict[str, Any]) -> None:
        await self.ws.send_json(obj)

    async def _send_error(self, message: str) -> None:
        await self._send_json({"type": "error", "message": message})

    async def _report_refusal(self, res: dict[str, Any]) -> None:
        if res.get("ret_code", 0) != 0:
            await self._send_error(f"Robot refused intercom (ret_code={res['ret_code']})")

    # ------------------------------------------------------------------ loops

    async def _tx_loop(self) -> None:
        while True:
            pcm = await self._rx_q.get()
            await self.ws.send_bytes(pcm)

    async def _follow_client(self) -> None:
        """Robot switch / reconnect / handoff: the manager now holds a different client. Re-run the join
        (START_RECORD) on it, otherwise the listener is attached to a robot whose mic was never started."""
        client = self._get_client() if self.joined else None
        if client is self._bound_client:
            return
        if client is not None:
            if not client.connected:
                return  # retry on the next tick once the new link is up
            try:
                res = await self.hub.join(self.member)
            except Exception as exc:
                logger.debug("intercom re-join after client switch failed: %s", exc)
                return
            await self._report_refusal(res)
        self._bind_listener()

    async def _state_loop(self) -> None:
        while True:
            await asyncio.sleep(_STATE_PERIOD_S)
            await self._follow_client()
            await self._send_json(self._state())

    def _uplink(self, data: bytes) -> None:
        client = self._get_client()
        if self.joined and client is not None and client.connected and self.hub.is_talker(self.member):
            # Opus encode is ~0.1 ms/frame; run inline to preserve frame order
            try:
                client.intercom.send_pcm(data)
            except Exception as exc:
                logger.debug("intercom downlink failed: %s", exc)

    async def _command(self, cmd: dict[str, Any]) -> None:
        kind = cmd.get("type")
        if kind == "start":
            res = await self.hub.join(self.member)
            self.joined = True
            self._bind_listener()
            await self._report_refusal(res)
        elif kind == "stop":
            self.joined = False
            self._unbind_listener()
            await self.hub.leave(self.member)
        elif kind == "talk":
            on = bool(cmd.get("on"))
            if not self.joined and on:
                await self._send_error("Start the intercom first")
            elif not self.hub.set_talking(self.member, on):
                await self._send_error("Another cockpit tab is talking")

    async def _rx_loop(self) -> None:
        while True:
            msg = await self.ws.receive()
            if msg.get("type") == "websocket.disconnect":
                raise WebSocketDisconnect(msg.get("code", 1000))
            data = msg.get("bytes")
            if data is not None:
                self._uplink(data)
                continue
            text = msg.get("text")
            if not text:
                continue
            try:
                cmd = json.loads(text)
            except ValueError:
                continue  # not a command; ignore like any unknown message
            try:
                await self._command(cmd)
            except Exception as exc:
                await self._send_error(str(exc) or exc.__class__.__name__)
            await self._send_json(self._state())

    # ------------------------------------------------------------------ lifecycle

    async def run(self) -> None:
        await self.ws.accept()
        await self._send_json(
            {
                "type": "hello",
                "sample_rate": INTERCOM_SAMPLE_RATE,
                "frame_samples": INTERCOM_FRAME_SAMPLES,
                "format": PCM_FORMAT,
            }
        )
        await self._send_json(self._state())
        loop = asyncio.get_running_loop()
        tasks = [
            loop.create_task(fn(), name=f"nebi-intercom-{fn.__name__.strip('_')}")
            for fn in (self._tx_loop, self._state_loop, self._rx_loop)
        ]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
            for t in done:
                exc = t.exception()
                if exc is not None and not isinstance(exc, (WebSocketDisconnect, RuntimeError)):
                    logger.debug("intercom socket ended: %r", exc)
        finally:
            for t in tasks:
                t.cancel()
            self._unbind_listener()
            self.hub.set_talking(self.member, False)
            await self.hub.leave(self.member)


async def intercom_socket(ws: WebSocket, hub: IntercomHub, get_client: GetClient) -> None:
    """Serve one `/ws/intercom` connection until it closes."""
    await IntercomSession(ws, hub, get_client).run()
