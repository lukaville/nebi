"""Robot-side MD-TS handshake state machine shared by the BLE (network service) and Zenoh (app bridge) peers.

Only the simulator plays the robot, but this is protocol logic (not a mock), so it lives with the protocol and
both robot-side peers (`nebi.sim.ble.MockBlePeripheral`, the device-role `ZenohTopicBus`) reuse it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from nebi.md_ts.session import MdTsSession
from nebi.md_ts.wire import (
    MD_TS_ERR_UNAUTHORIZED,
    MD_TS_ROLE_SERVER,
    MdTsHandshakeMessage,
    _random_nonzero_bytes,
    build_hs_error,
    build_hs_session_challenge,
)


class MdTsAuthorizedClient(Protocol):
    """An entry of the robot's client authorization table."""

    @property
    def public_key(self) -> bytes: ...


class MdTsAuthority(Protocol):
    """Robot static X25519 identity plus its client authorization table."""

    @property
    def private_key_bytes(self) -> bytes: ...

    @property
    def public_key_bytes(self) -> bytes: ...

    def is_known_client(self, client_id: bytes) -> bool: ...

    def lookup_for_session_start(
        self, msg: MdTsHandshakeMessage, *, require_permanent: bool
    ) -> tuple[MdTsAuthorizedClient | None, bytes | None]: ...


@dataclass(frozen=True, slots=True)
class _PendingHandshake:
    client_id: bytes
    key_version: int
    client_nonce: bytes
    server_nonce: bytes
    channel_id: bytes
    session: MdTsSession


@dataclass(frozen=True, slots=True)
class MdTsConfirmResult:
    """Outcome of a CLIENT_CONFIRM (msg_type 6): `session` is set on success, `None` on rejection.

    `reply` is the sealed SERVER_CONFIRM record (send with the MD-TS flag) on success, or a plaintext
    `MD-TS-HS` error message on rejection.
    """

    client_id: bytes
    key_version: int
    session: MdTsSession | None
    reply: bytes


class MdTsServerHandshake:
    """SESSION_START (4) -> SESSION_CHALLENGE (5) and CLIENT_CONFIRM (6) -> SERVER_CONFIRM (7), robot side."""

    def __init__(self, transport_type: int) -> None:
        self._transport_type = transport_type
        self._pending: _PendingHandshake | None = None

    def reset(self) -> None:
        self._pending = None

    def pending_challenge(self) -> bytes | None:
        """The SESSION_CHALLENGE currently awaiting CLIENT_CONFIRM (as last sent), or `None`."""
        p = self._pending
        if p is None:
            return None
        return build_hs_session_challenge(p.client_id, p.key_version, p.server_nonce, p.channel_id)

    def challenge(self, msg: MdTsHandshakeMessage, client_public_key: bytes, authority: MdTsAuthority) -> bytes:
        """Answer an authorized SESSION_START with a SESSION_CHALLENGE (`msg_type = 5`).

        A byte-identical retry (same client, key version and nonce) gets the same challenge back, matching the
        idempotent retry handling of the robot's BLE and Zenoh peers.
        """
        p = self._pending
        if p is None or (p.client_id, p.key_version, p.client_nonce) != (msg.client_id, msg.key_version, msg.nonce):
            snonce = _random_nonzero_bytes(32)
            ch_id = _random_nonzero_bytes(8)
            session = MdTsSession.from_handshake(
                role=MD_TS_ROLE_SERVER,
                transport_type=self._transport_type,
                local_private_key_bytes=authority.private_key_bytes,
                peer_public_key_bytes=client_public_key,
                client_id=msg.client_id,
                key_version=msg.key_version,
                client_static_pub=client_public_key,
                robot_static_pub=authority.public_key_bytes,
                client_nonce=msg.nonce,
                server_nonce=snonce,
                channel_id=ch_id,
            )
            p = _PendingHandshake(msg.client_id, msg.key_version, msg.nonce, snonce, ch_id, session)
            self._pending = p
        return build_hs_session_challenge(p.client_id, p.key_version, p.server_nonce, p.channel_id)

    def confirm(
        self,
        record: bytes,
        *,
        ble_hdr: bytes | None = None,
        rx_keyexpr: str | None = None,
        ble_cmd_id: int | None = None,
        tx_keyexpr: str | None = None,
    ) -> MdTsConfirmResult | None:
        """Verify a CLIENT_CONFIRM record; `None` when no challenge is pending (the frame is ignored).

        `ble_hdr` / `rx_keyexpr` select the AAD of the received record, `ble_cmd_id` / `tx_keyexpr` that of the
        SERVER_CONFIRM reply. A failed verification keeps the pending challenge so the client may retry.
        """
        p = self._pending
        if p is None:
            return None
        try:
            p.session.open_handshake_confirm(
                record,
                expected_client_id=p.client_id,
                expected_key_version=p.key_version,
                ble_hdr=ble_hdr,
                zenoh_keyexpr=rx_keyexpr,
            )
        except ValueError:
            return MdTsConfirmResult(
                p.client_id, p.key_version, None, build_hs_error(p.client_id, p.key_version, MD_TS_ERR_UNAUTHORIZED)
            )
        self._pending = None
        ack = p.session.seal_handshake_confirm(
            client_id=p.client_id,
            key_version=p.key_version,
            ble_cmd_id=ble_cmd_id,
            zenoh_keyexpr=tx_keyexpr,
        )
        return MdTsConfirmResult(p.client_id, p.key_version, p.session, ack)
