"""MD-TS session negotiation over Zenoh (`app/security/session` <-> `device/security/session`).

Client side: `negotiate_client_session` runs `msg_type = 4 -> 5 -> 6 -> 7` and returns the confirmed
`MdTsSession` (or `None` to stay in plaintext). Robot side: `ZenohMdTsResponder` mirrors the robot's
handler for the simulator's device-role bus. Both only need a raw-publish callable, so they are independent
of the bus implementation.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from nebi.exceptions import NebiHandshakeError
from nebi.md_ts import (
    DEFAULT_IDENTITY_STORE,
    MD_TS_ERR_UNAUTHORIZED,
    MD_TS_HS_ERROR,
    MD_TS_HS_SESSION_CHALLENGE,
    MD_TS_HS_SESSION_START,
    MD_TS_ROLE_CLIENT,
    MD_TS_TRANSPORT_ZENOH,
    ZENOH_APP_SECURITY_KEYEXPR,
    ZENOH_DEVICE_SECURITY_KEYEXPR,
    MdTsAuthority,
    MdTsHandshakeMessage,
    MdTsIdentityStore,
    MdTsServerHandshake,
    MdTsSession,
    _random_nonzero_bytes,
    build_hs_error,
    build_hs_session_start,
    decode_zenoh_envelope,
    encode_zenoh_enc_envelope,
    encode_zenoh_hs_envelope,
    parse_hs_message,
)

logger = logging.getLogger(__name__)

RawPublish = Callable[[str, bytes], None]
SecurityEnvelope = tuple[str, bytes]  # ("handshake" | "encrypted", body) from `decode_zenoh_envelope`

SECURITY_MODES = ("auto", "md-ts", "plaintext")


async def negotiate_client_session(
    publish: RawPublish,
    rx_queue: asyncio.Queue[SecurityEnvelope],
    saw_plaintext: asyncio.Event,
    *,
    identity_store: MdTsIdentityStore | None = None,
    robot_public_key: bytes | None = None,
    security_mode: str = "auto",
    timeout: float = 2.0,
) -> MdTsSession | None:
    """Client-side (`role='app'`) MD-TS Zenoh handshake; see `ZenohTopicBus.establish_md_ts_session`.

    `rx_queue` receives decoded envelopes from `device/security/session`; `saw_plaintext` is set by the bus when
    a plaintext `device/**` frame arrives (a peer that does not speak MD-TS).
    """
    store = identity_store or DEFAULT_IDENTITY_STORE
    ident = store.identity

    while not rx_queue.empty():
        rx_queue.get_nowait()

    # Fast-path in "auto" mode if plaintext frames have already arrived from a simulator/dev unit
    if security_mode == "auto" and saw_plaintext.is_set() and robot_public_key is None:
        return None

    client_nonce = _random_nonzero_bytes(32)
    hs4_env = encode_zenoh_hs_envelope(
        build_hs_session_start(ident.client_id, ident.key_version, client_nonce)
    )

    # Give Zenoh subscriber declaration a brief moment to propagate across the loopback tunnel
    await asyncio.sleep(0.03)

    probe_window = timeout if (security_mode == "md-ts" or robot_public_key is not None) else min(0.35, timeout)
    deadline = asyncio.get_running_loop().time() + probe_window
    hs5: MdTsHandshakeMessage | None = None

    while asyncio.get_running_loop().time() < deadline:
        if security_mode == "auto" and saw_plaintext.is_set() and robot_public_key is None:
            return None

        publish(ZENOH_APP_SECURITY_KEYEXPR, hs4_env)
        rem = deadline - asyncio.get_running_loop().time()
        if rem <= 0:
            break

        sec_task = asyncio.create_task(rx_queue.get(), name="md-ts-security-rx")
        pt_task = asyncio.create_task(saw_plaintext.wait(), name="md-ts-plaintext-probe")
        try:
            done, pending = await asyncio.wait(
                {sec_task, pt_task},
                timeout=min(0.25, rem),
                return_when=asyncio.FIRST_COMPLETED,
            )
            for p in pending:
                p.cancel()
            if pt_task in done and security_mode == "auto" and robot_public_key is None and sec_task not in done:
                return None
            if sec_task not in done:
                continue
            kind, body = sec_task.result()
        except Exception as exc:  # e.g. the queue get was cancelled underneath us: just probe again
            logger.debug("MD-TS probe wait interrupted: %r", exc)
            sec_task.cancel()
            pt_task.cancel()
            continue

        if kind != "handshake":
            continue
        try:
            cand = parse_hs_message(body)
        except ValueError as exc:
            logger.debug("Skipping malformed MD-TS handshake frame on device/security/session: %s", exc)
            continue
        if cand.msg_type == MD_TS_HS_ERROR:
            if cand.error_code == MD_TS_ERR_UNAUTHORIZED:
                raise NebiHandshakeError(
                    "ZENOH_AUTH_REQUIRED",
                    "Nebi rejected the Wi-Fi/LAN control session because nebi's identity is not yet authorized on this robot.",
                    stage="zenoh_tunneling",
                    recoverable=True,
                    hint=(
                        "Pair nebi once over Bluetooth LE (`nebi pair-ble` or the cockpit's 'Wake up over Bluetooth and connect to Home Wi-Fi', holding Nebi's button "
                        "for 3-5 seconds) to authorize nebi on this robot."
                    ),
                    details={"error_code": cand.error_code, "error_name": cand.error_name},
                )
            raise NebiHandshakeError(
                "ZENOH_SECURITY_REJECTED",
                f"Robot rejected Zenoh MD-TS session start ({cand.error_name}).",
                stage="zenoh_tunneling",
                recoverable=True,
                details={"error_code": cand.error_code, "error_name": cand.error_name},
            )
        if cand.msg_type == MD_TS_HS_SESSION_CHALLENGE:
            hs5 = cand
            break

    if hs5 is None:
        if security_mode == "auto" and robot_public_key is None:
            return None
        raise NebiHandshakeError(
            "ZENOH_SECURITY_TIMEOUT",
            "Timed out waiting for Zenoh MD-TS SESSION_CHALLENGE on device/security/session.",
            stage="zenoh_tunneling",
            recoverable=True,
        )

    resolved_robot_pub = robot_public_key or store.lookup_robot_public_key(allow_default=True)
    if not resolved_robot_pub:
        raise NebiHandshakeError(
            "ZENOH_MISSING_ROBOT_KEY",
            "Robot requires MD-TS Zenoh encryption, but Nebi's static X25519 public key is not cached locally.",
            stage="zenoh_tunneling",
            recoverable=True,
            hint="Connect once over Bluetooth LE (`nebi pair-ble` or the cockpit's 'Wake up over Bluetooth and connect to Home Wi-Fi') so nebi records Nebi's public key.",
        )

    session = MdTsSession.from_handshake(
        role=MD_TS_ROLE_CLIENT,
        transport_type=MD_TS_TRANSPORT_ZENOH,
        local_private_key_bytes=ident.private_key_bytes,
        peer_public_key_bytes=resolved_robot_pub,
        client_id=ident.client_id,
        key_version=ident.key_version,
        client_static_pub=ident.public_key_bytes,
        robot_static_pub=resolved_robot_pub,
        client_nonce=client_nonce,
        server_nonce=hs5.nonce,
        channel_id=hs5.channel_id,
    )

    confirm_rec = session.seal_handshake_confirm(
        client_id=ident.client_id,
        key_version=ident.key_version,
        zenoh_keyexpr=ZENOH_APP_SECURITY_KEYEXPR,
    )
    publish(ZENOH_APP_SECURITY_KEYEXPR, encode_zenoh_enc_envelope(confirm_rec))

    confirm_deadline = asyncio.get_running_loop().time() + max(1.5, timeout)
    while True:
        rem = confirm_deadline - asyncio.get_running_loop().time()
        if rem <= 0:
            raise NebiHandshakeError(
                "ZENOH_SECURITY_TIMEOUT",
                "Timed out waiting for Zenoh MD-TS SERVER_CONFIRM_ACK (msg_type=7).",
                stage="zenoh_tunneling",
                recoverable=True,
            )
        try:
            kind7, body7 = await asyncio.wait_for(rx_queue.get(), timeout=rem)
        except asyncio.TimeoutError as exc:
            raise NebiHandshakeError(
                "ZENOH_SECURITY_TIMEOUT",
                "Timed out waiting for Zenoh MD-TS SERVER_CONFIRM_ACK (msg_type=7).",
                stage="zenoh_tunneling",
                recoverable=True,
            ) from exc

        if kind7 == "handshake":
            try:
                err7 = parse_hs_message(body7)
            except ValueError as exc:
                logger.debug("Skipping malformed MD-TS handshake frame while awaiting msg_type=7: %s", exc)
                continue
            if err7.msg_type == MD_TS_HS_ERROR:
                raise NebiHandshakeError(
                    "ZENOH_SECURITY_REJECTED",
                    f"Robot rejected Zenoh CLIENT_CONFIRM ({err7.error_name}).",
                    stage="zenoh_tunneling",
                    recoverable=True,
                )
            continue

        if kind7 == "encrypted":
            try:
                session.open_handshake_confirm(
                    body7,
                    expected_client_id=ident.client_id,
                    expected_key_version=ident.key_version,
                    zenoh_keyexpr=ZENOH_DEVICE_SECURITY_KEYEXPR,
                )
            except ValueError as exc:
                raise NebiHandshakeError(
                    "ZENOH_SECURITY_REJECTED",
                    f"Zenoh SERVER_CONFIRM_ACK verification failed: {exc}",
                    stage="zenoh_tunneling",
                    recoverable=True,
                ) from exc
            return session


class ZenohMdTsResponder:
    """Robot-side handler for `app/security/session`, used by the simulator's device-role bus."""

    def __init__(self, authority: MdTsAuthority, publish: RawPublish, *, required: bool) -> None:
        self.authority = authority
        self._publish = publish
        self._required = required
        self._handshake = MdTsServerHandshake(MD_TS_TRANSPORT_ZENOH)

    def reset(self) -> None:
        self._handshake.reset()

    def _reply(self, envelope: bytes) -> None:
        self._publish(ZENOH_DEVICE_SECURITY_KEYEXPR, envelope)

    def handle(self, raw_bytes: bytes) -> MdTsSession | None:
        """Process one `app/security/session` sample; returns the session once CLIENT_CONFIRM verified."""
        env = decode_zenoh_envelope(raw_bytes)
        if env is None:
            return None
        kind, body = env

        if kind == "handshake":
            try:
                msg = parse_hs_message(body)
            except ValueError:
                return None
            if msg.msg_type != MD_TS_HS_SESSION_START:
                return None
            entry, err_hs = self.authority.lookup_for_session_start(msg, require_permanent=True)
            if err_hs is not None or entry is None:
                if not self._required and not self.authority.is_known_client(msg.client_id):
                    return None  # dev/simulator mode without BLE pairing: stay in plaintext without rejecting
                rsp_err = err_hs or build_hs_error(msg.client_id, msg.key_version, MD_TS_ERR_UNAUTHORIZED)
                self._reply(encode_zenoh_hs_envelope(rsp_err))
                return None
            self._reply(encode_zenoh_hs_envelope(self._handshake.challenge(msg, entry.public_key, self.authority)))
            return None

        if kind == "encrypted":
            result = self._handshake.confirm(
                body, rx_keyexpr=ZENOH_APP_SECURITY_KEYEXPR, tx_keyexpr=ZENOH_DEVICE_SECURITY_KEYEXPR
            )
            if result is None:
                return None
            if result.session is None:
                self._reply(encode_zenoh_hs_envelope(result.reply))
                return None
            self._reply(encode_zenoh_enc_envelope(result.reply))
            return result.session
        return None
