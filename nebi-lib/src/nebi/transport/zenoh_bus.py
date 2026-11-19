"""`ZenohTopicBus`: Protobuf pub/sub + correlated RPC over `eclipse-zenoh`, optionally MD-TS encrypted.

The bus runs over either the Zenoh-over-UCP tunnel (`zenoh_tunnel`) or a direct `tcp/<ip>:7447` endpoint. It
injects/extracts `md.protocol.common.WireHeader(seq_num)`, applies the app bridge's Protobuf normalisation
(`zenoh_proto`) and delegates the MD-TS negotiation to `zenoh_security`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import zenoh
from google.protobuf.message import Message

from nebi.catalog.registry import CATALOG
from nebi.exceptions import NebiLinkLostError, NebiNotConnectedError, NebiRpcTimeoutError
from nebi.md_ts import (
    ZENOH_SECURITY_SESSION_TOPIC,
    MdTsAuthority,
    MdTsIdentityStore,
    MdTsSession,
    decode_zenoh_envelope,
)
from nebi.transport.zenoh_proto import fill_message_data, normalize_proto_for_videotrans
from nebi.transport.zenoh_security import SECURITY_MODES, ZenohMdTsResponder, negotiate_client_session
from nebi.transport.zenoh_tunnel import log_task_exception

logger = logging.getLogger(__name__)

# Topics the official app publishes WITHOUT `header`. The robot's app bridge silently drops /data/audio/playback_data frames
# that carry a header (A/B on the real robot 2026-11-17: header set -> 0 of 100 frames reach the bus, no header ->
# 100 of 100). It also needs `encoding_type` explicitly present (`normalize_proto_for_videotrans` sets
# proto3-optional defaults).
# Topics whose app-bridge codec rejects a `header` (message dropped before reaching the robot bus).
HEADERLESS_TOPICS = frozenset(
    {
        "/data/audio/playback_data",
        "/cmd/camera/unified_cmd",
        "/cmd/camera/relative_trajectory_replay",
        # The robot's codecs for these topics reject a protobuf `header` too (not yet confirmed on hardware).
        "/cmd/led/ctrl_oneway",
        "/cmd/hms/action_request",
        "/cmd/hms/action_response",
    }
)

# Keep Zenoh batches <= 1432B so 12B UCP hdr + 3B 0xA7 hdr + 2B len + body + 11B SACK <= 1460B UCP_MSS
_ZENOH_TX_BATCH_SIZE = "1432"

TopicCallback = Callable[[str, Message, int], Any]


@dataclass(slots=True, eq=False)
class _RpcWaiter:
    seq: int
    match_seq: bool
    future: asyncio.Future[Message]


class ZenohTopicBus:
    """High-level async Protobuf Pub/Sub + RPC bus over `eclipse-zenoh`, supporting `MD-TS` AEAD encryption."""

    def __init__(
        self,
        role: str = "app",
        *,
        md_ts_authority: MdTsAuthority | None = None,
        md_ts_required: bool = False,
    ) -> None:
        """`role="app"` publishes `app/<topic>` and subscribes `device/**`.
        `role="device"` (used by `MockNebiRobot`) publishes `device/<topic>` and subscribes `app/**`.
        """
        if role not in ("app", "device"):
            raise ValueError(f"ZenohTopicBus role must be 'app' or 'device', got {role!r}")
        self.role = role
        self._pub_prefix = "app" if role == "app" else "device"
        self._sub_wildcard = "device/**" if role == "app" else "app/**"
        self._sub_strip = "device/" if role == "app" else "app/"

        self._session: zenoh.Session | None = None
        self._subscriber: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None

        self._seq_num: int = 0
        self._topic_listeners: dict[str, list[TopicCallback]] = {}
        self._wildcard_listeners: list[TopicCallback] = []
        # Per reply topic, FIFO of in-flight RPC waiters `(seq, match_seq, future)`. A response is delivered to the
        # waiter whose seq it echoes (`header.seq_num`); a seq-less response goes to the oldest waiter that accepts
        # any seq; a non-zero seq matching no waiter is stale and dropped.
        self._pending_rpcs: dict[str, deque[_RpcWaiter]] = {}
        self._bg_tasks: set[asyncio.Task[Any]] = set()

        # MD-TS security state
        self.md_ts_authority: MdTsAuthority | None = md_ts_authority
        self.md_ts_required: bool = md_ts_required
        self._md_ts_session: MdTsSession | None = None
        self._responder: ZenohMdTsResponder | None = (
            ZenohMdTsResponder(md_ts_authority, self._publish_raw_keyexpr, required=md_ts_required)
            if md_ts_authority is not None and role == "device"
            else None
        )
        self._security_rx_queue: asyncio.Queue[tuple[str, bytes]] = asyncio.Queue()
        self._saw_plaintext_rx = asyncio.Event()

        self.rx_count: int = 0
        self.tx_count: int = 0

    @property
    def is_open(self) -> bool:
        return self._session is not None

    @property
    def md_ts_active(self) -> bool:
        return self._md_ts_session is not None and self._md_ts_session.handshake_confirmed

    def reset_md_ts_session(self) -> None:
        """Clear per-connection MD-TS session state (e.g. when UCP/Zenoh tunnel reconnects)."""
        self._md_ts_session = None
        if self._responder is not None:
            self._responder.reset()
        self._saw_plaintext_rx.clear()
        while not self._security_rx_queue.empty():
            self._security_rx_queue.get_nowait()

    async def open_client(self, connect_endpoint: str) -> None:
        self._loop = asyncio.get_running_loop()
        self.reset_md_ts_session()
        cfg = zenoh.Config()
        cfg.insert_json5("mode", '"client"')
        cfg.insert_json5("connect/endpoints", f'["{connect_endpoint}"]')
        cfg.insert_json5("scouting/multicast/enabled", "false")
        cfg.insert_json5("transport/link/tx/batch_size", _ZENOH_TX_BATCH_SIZE)
        self._session = await asyncio.to_thread(zenoh.open, cfg)
        self._subscriber = self._session.declare_subscriber(self._sub_wildcard, self._on_zenoh_sample)

    async def open_peer_listener(self, host: str = "127.0.0.1", port: int = 0) -> int:
        """Open a listening Zenoh peer/router session (used by `MockNebiRobot`) and return bound TCP port."""
        self._loop = asyncio.get_running_loop()
        self.reset_md_ts_session()
        if port == 0:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind((host, 0))
                port = int(s.getsockname()[1])

        cfg = zenoh.Config()
        cfg.insert_json5("mode", '"peer"')
        cfg.insert_json5("listen/endpoints", f'["tcp/{host}:{port}"]')
        cfg.insert_json5("scouting/multicast/enabled", "false")
        cfg.insert_json5("transport/link/tx/batch_size", _ZENOH_TX_BATCH_SIZE)
        self._session = await asyncio.to_thread(zenoh.open, cfg)
        self._subscriber = self._session.declare_subscriber(self._sub_wildcard, self._on_zenoh_sample)
        return port

    def next_seq(self) -> int:
        self._seq_num = (self._seq_num + 1) & 0x7FFFFFFF
        if self._seq_num == 0:
            self._seq_num = 1
        return self._seq_num

    def _require_session(self) -> zenoh.Session:
        if self._session is None:
            raise NebiNotConnectedError(
                "Zenoh topic bus is not open.",
                stage="zenoh_tunneling",
                hint="Connect to the robot (or simulator) before publishing or calling RPCs.",
            )
        return self._session

    def _publish_raw_keyexpr(self, key_expr: str, payload: bytes, *, block_congestion: bool = False) -> None:
        session = self._require_session()
        # Always emit inline full string keyexprs via Session.put() rather than numeric DeclareKeyExpr(expr_id)
        # aliases so the robot's zenoh-pico wildcard subscriber (`app/**`) matches every frame directly.
        session.put(
            key_expr,
            payload,
            encoding="application/protobuf",
            congestion_control=(
                zenoh.CongestionControl.BLOCK if block_congestion else zenoh.CongestionControl.DROP
            ),
        )
        self.tx_count += 1

    async def establish_md_ts_session(
        self,
        *,
        identity_store: MdTsIdentityStore | None = None,
        robot_public_key: bytes | None = None,
        security_mode: str = "auto",  # "auto" | "md-ts" | "plaintext"
        timeout: float = 2.0,
    ) -> bool:
        """Client-side (`role='app'`) `MD-TS` Zenoh handshake (`msg_type = 4 -> 5 -> 6 -> 7`).

        In `"auto"` mode:
          - If the peer emits plaintext `device/**` frames or does not respond on `device/security/session`
            within the probe window (and `robot_public_key` wasn't explicitly forced with `"md-ts"`),
            seamlessly falls back to plaintext (`returns False`).
          - If the peer replies with `0x7F` (`UNAUTHORIZED_CLIENT`), raises `NebiHandshakeError("ZENOH_AUTH_REQUIRED")`.
          - If the peer replies with `msg_type = 5`, completes `msg_type = 6 -> 7` and activates `MD-TS` (`returns True`).
        """
        if self.role != "app":
            raise RuntimeError("establish_md_ts_session is only valid on role='app'")
        if security_mode not in SECURITY_MODES:
            raise ValueError(f"Invalid Zenoh security_mode {security_mode!r}")
        if security_mode == "plaintext":
            return False
        if self.md_ts_active:
            return True

        session = await negotiate_client_session(
            self._publish_raw_keyexpr,
            self._security_rx_queue,
            self._saw_plaintext_rx,
            identity_store=identity_store,
            robot_public_key=robot_public_key,
            security_mode=security_mode,
            timeout=timeout,
        )
        if session is None:
            return False
        self._md_ts_session = session
        return True

    def _on_zenoh_sample(self, sample: Any) -> None:
        key_str = str(sample.key_expr).lstrip("/")
        topic = "/" + (key_str[len(self._sub_strip) :] if key_str.startswith(self._sub_strip) else key_str)
        raw_bytes = sample.payload.to_bytes()
        if self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._dispatch_decoded, key_str, topic, raw_bytes)

    def _dispatch_decoded(self, key_str: str, topic: str, raw_bytes: bytes) -> None:
        self.rx_count += 1

        if topic == ZENOH_SECURITY_SESSION_TOPIC:
            if self.role == "device":
                if self._responder is not None:
                    session = self._responder.handle(raw_bytes)
                    if session is not None:
                        self._md_ts_session = session
            else:
                env = decode_zenoh_envelope(raw_bytes)
                if env is not None:
                    self._security_rx_queue.put_nowait(env)
            return

        env = decode_zenoh_envelope(raw_bytes)
        if env is not None:
            kind, rec_bytes = env
            if kind != "encrypted" or self._md_ts_session is None:
                return
            try:
                raw_bytes = self._md_ts_session.open_zenoh_record(key_str, rec_bytes)
            except ValueError as exc:
                logger.debug("Dropped Zenoh MD-TS frame on %s: %s", key_str, exc)
                return
        else:
            # Plaintext Protobuf payload
            if self.md_ts_required or self._md_ts_session is not None:
                return
            self._saw_plaintext_rx.set()

        cls = CATALOG.get_proto_class(topic)
        if cls is None:
            return
        try:
            msg = cls.FromString(raw_bytes)
        except Exception as exc:
            logger.debug("Failed to parse protobuf on %s: %s", topic, exc)
            return

        seq = int(msg.header.seq_num) if hasattr(msg, "header") and msg.HasField("header") else 0

        self._resolve_rpc(topic, seq, msg)

        for cb in (*self._topic_listeners.get(topic, ()), *self._wildcard_listeners):
            try:
                res = cb(topic, msg, seq)
            except Exception:
                logger.exception("Zenoh topic listener %r failed on %s", cb, topic)
                continue
            if asyncio.iscoroutine(res):
                self._spawn(res, name=f"zenoh-listener:{topic}")

    def _spawn(self, coro: Any, *, name: str) -> asyncio.Task[Any]:
        """Create a background task, keep a strong reference, and log (not swallow) its failure."""
        task = asyncio.ensure_future(coro)
        task.set_name(name)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        task.add_done_callback(log_task_exception)
        return task

    def _resolve_rpc(self, topic: str, seq: int, msg: Message) -> None:
        waiters = self._pending_rpcs.get(topic)
        if not waiters:
            return
        # Drop waiters that already finished (timed out / cancelled) from the front.
        target: _RpcWaiter | None = None
        if seq:
            target = next((w for w in waiters if w.seq == seq and not w.future.done()), None)
            if target is None:
                # Echoes a seq no pending request owns: a stale reply to a request that already timed out or was
                # cancelled. Handing it to another waiter would resolve the wrong request, so drop it.
                logger.debug("Dropping stale RPC reply on %s (seq=%d matches no pending request)", topic, seq)
                return
        else:
            # Reply carries no seq (the robot may not echo header.seq_num):
            # hand it to the oldest waiter that accepts any reply on this topic.
            target = next((w for w in waiters if not w.match_seq and not w.future.done()), None)
        if target is None:
            return
        waiters.remove(target)
        if not waiters:
            self._pending_rpcs.pop(topic, None)
        target.future.set_result(msg)

    def subscribe_topic(self, topic: str, callback: TopicCallback) -> Callable[[], None]:
        norm = "/" + topic.lstrip("/")
        lst = self._topic_listeners.setdefault(norm, [])
        lst.append(callback)
        return lambda: lst.remove(callback) if callback in lst else None

    def subscribe_all(self, callback: TopicCallback) -> Callable[[], None]:
        self._wildcard_listeners.append(callback)
        return lambda: self._wildcard_listeners.remove(callback) if callback in self._wildcard_listeners else None

    def publish_proto(self, topic: str, msg: Message, seq_num: int | None = None) -> int:
        self._require_session()
        norm = "/" + topic.lstrip("/")
        seq = seq_num if seq_num is not None else self.next_seq()
        if norm in HEADERLESS_TOPICS:
            if hasattr(msg, "header"):
                msg.ClearField("header")
        elif hasattr(msg, "header"):
            msg.header.seq_num = seq
        if hasattr(msg, "data"):
            msg.data.SetInParent()
            if norm.startswith(("/cmd/", "/data/")):
                normalize_proto_for_videotrans(msg.data)

        # Like the robot: suppress device->app broadcasts until the MD-TS handshake completes
        if self.md_ts_required and self._md_ts_session is None:
            return seq

        key_expr = f"{self._pub_prefix}{norm}"
        spec = CATALOG.topics.get(norm)
        block_cong = bool(spec is not None and spec.reliable and norm.startswith("/cmd/ota"))

        raw_pt = msg.SerializeToString()
        wire_bytes = (
            self._md_ts_session.seal_zenoh_payload(key_expr, raw_pt)
            if self._md_ts_session is not None
            else raw_pt
        )
        self._publish_raw_keyexpr(key_expr, wire_bytes, block_congestion=block_cong)
        return seq

    def publish(self, topic: str, seq_num: int | None = None, **data_fields: Any) -> int:
        norm = "/" + topic.lstrip("/")
        cls = CATALOG.get_proto_class(norm)
        if cls is None:
            raise KeyError(f"Unknown topic protobuf class for {norm}")
        msg = cls()
        fill_message_data(msg, data_fields)
        return self.publish_proto(norm, msg, seq_num=seq_num)

    async def request(
        self,
        req_topic: str,
        rsp_topic: str | None = None,
        timeout: float = 2.0,
        match_seq: bool = False,
        **data_fields: Any,
    ) -> Any:
        """Publish on `req_topic` once and await the Protobuf reply on `rsp_topic`.

        Correlation: concurrent requests sharing a reply topic each get their own waiter. A reply whose
        `header.seq_num` equals a pending request's seq goes to that request; a reply with a non-zero seq that
        matches no pending request is a stale reply and is dropped. A reply without a seq (0) is delivered to the
        oldest pending request (FIFO), unless `match_seq=True` was given, in which case only an exact seq match
        resolves it. If the bus is closed while waiting, `NebiLinkLostError(LINK_CLOSED)` is raised.

        Delivery: the request is published exactly ONCE. The Zenoh session runs over the reliable UCP channel 3
        (or TCP), so a blind periodic resend only risks executing non-idempotent commands twice (each resend is
        sealed under a fresh MD-TS record seq, so the robot cannot de-duplicate it). On timeout
        `NebiRpcTimeoutError` (also a `TimeoutError`) is raised; callers may retry idempotent queries themselves.
        """
        norm_req = "/" + req_topic.lstrip("/")
        if rsp_topic is None:
            norm_rsp = norm_req[:-4] + "_rsp" if norm_req.endswith("_req") else norm_req + "_ack"
        else:
            norm_rsp = "/" + rsp_topic.lstrip("/")

        self._require_session()
        seq = self.next_seq()
        loop = asyncio.get_running_loop()
        waiter = _RpcWaiter(seq=seq, match_seq=match_seq, future=loop.create_future())
        self._pending_rpcs.setdefault(norm_rsp, deque()).append(waiter)
        try:
            self.publish(norm_req, seq_num=seq, **data_fields)
            try:
                return await asyncio.wait_for(waiter.future, timeout=timeout)
            except asyncio.TimeoutError:
                raise NebiRpcTimeoutError(
                    "RPC_TIMEOUT",
                    f"Zenoh RPC {norm_req} -> {norm_rsp} timed out after {timeout:.1f}s",
                    stage="zenoh_rpc",
                    recoverable=True,
                    hint="The robot did not answer; check the link, then retry if the command is safe to repeat.",
                    details={"request_topic": norm_req, "response_topic": norm_rsp, "seq_num": seq},
                ) from None
        finally:
            waiters = self._pending_rpcs.get(norm_rsp)
            if waiters is not None:
                with contextlib.suppress(ValueError):  # already removed by `_resolve_rpc`
                    waiters.remove(waiter)
                if not waiters:
                    self._pending_rpcs.pop(norm_rsp, None)

    async def close(self) -> None:
        self.reset_md_ts_session()
        for topic, waiters in self._pending_rpcs.items():
            for w in waiters:
                if not w.future.done():
                    # Not `cancel()`: a CancelledError would look like the *caller* was cancelled.
                    w.future.set_exception(
                        NebiLinkLostError(
                            "LINK_CLOSED",
                            f"The robot link was closed while waiting for a reply on {topic}.",
                            stage="zenoh_rpc",
                            recoverable=True,
                            hint="The connection was closed (disconnect or shutdown); reconnect, then retry "
                            "if the command is safe to repeat.",
                            details={"response_topic": topic, "seq_num": w.seq},
                        )
                    )
        self._pending_rpcs.clear()
        for task in list(self._bg_tasks):
            task.cancel()
        if self._subscriber is not None:
            try:
                self._subscriber.undeclare()
            except Exception as exc:  # session may already be gone; closing continues regardless
                logger.debug("Zenoh subscriber undeclare failed: %r", exc)
            self._subscriber = None
        if self._session is not None:
            sess = self._session
            self._session = None
            await asyncio.to_thread(sess.close)
