"""Simulated robot static identity + client authorization table (shared by the BLE and Zenoh peers)."""

from __future__ import annotations

from dataclasses import dataclass, field

from nebi.md_ts import (
    MD_TS_ERR_PENDING_NOT_FINALIZED,
    MD_TS_ERR_PUBKEY_MISMATCH,
    MD_TS_ERR_UNAUTHORIZED,
    MdTsHandshakeMessage,
    build_hs_error,
    build_hs_pending_button,
    build_hs_server_hello,
    generate_x25519_keypair,
)

AUTH_STATUS_STAGED = 1     # button pressed, awaiting the msg_type=6 confirm
AUTH_STATUS_PERMANENT = 2


@dataclass
class _AuthorizedClientEntry:
    client_id: bytes
    key_version: int
    public_key: bytes
    status: int  # AUTH_STATUS_STAGED | AUTH_STATUS_PERMANENT


@dataclass
class MdTsServerAuthority:
    """Simulated robot static identity + client authorization table shared across BLE & Zenoh."""

    private_key_bytes: bytes = field(default_factory=bytes)
    public_key_bytes: bytes = field(default_factory=bytes)
    require_button_auth: bool = False
    auto_authorize_unknown: bool = True
    clients: dict[bytes, _AuthorizedClientEntry] = field(default_factory=dict)
    pending_button_client: tuple[bytes, int, bytes] | None = None

    def __post_init__(self) -> None:
        if not self.private_key_bytes or not self.public_key_bytes:
            self.private_key_bytes, self.public_key_bytes = generate_x25519_keypair()

    def is_known_client(self, client_id: bytes) -> bool:
        return client_id in self.clients

    def reset(self, *, require_button_auth: bool = True) -> None:
        self.clients.clear()
        self.pending_button_client = None
        self.require_button_auth = require_button_auth
        self.auto_authorize_unknown = not require_button_auth

    def approve_pending_button(self) -> _AuthorizedClientEntry | None:
        """Stage the pending BLE client (`status = 1`) when the physical 5s button is pressed."""
        if self.pending_button_client is None:
            self.auto_authorize_unknown = True
            return None
        cid, kv, cpub = self.pending_button_client
        self.pending_button_client = None
        entry = _AuthorizedClientEntry(client_id=cid, key_version=kv, public_key=cpub, status=AUTH_STATUS_STAGED)
        self.clients[cid] = entry
        return entry

    def handle_ble_client_hello(
        self,
        msg: MdTsHandshakeMessage,
    ) -> tuple[str, bytes]:
        """Process BLE `msg_type = 1`, returning `(state, response_hs_bytes)`."""
        existing = self.clients.get(msg.client_id)
        if existing is not None:
            if existing.key_version != msg.key_version or existing.public_key != msg.public_key:
                return ("error", build_hs_error(msg.client_id, msg.key_version, MD_TS_ERR_PUBKEY_MISMATCH))
            return ("authorized", build_hs_server_hello(msg.client_id, msg.key_version, self.public_key_bytes))

        if not self.require_button_auth or self.auto_authorize_unknown:
            self.clients[msg.client_id] = _AuthorizedClientEntry(
                client_id=msg.client_id,
                key_version=msg.key_version,
                public_key=msg.public_key,
                status=AUTH_STATUS_PERMANENT,
            )
            return ("authorized", build_hs_server_hello(msg.client_id, msg.key_version, self.public_key_bytes))

        if self.pending_button_client is not None:
            p_cid, p_kv, p_pub = self.pending_button_client
            if (p_cid, p_kv, p_pub) != (msg.client_id, msg.key_version, msg.public_key):
                return ("busy", build_hs_pending_button(msg.client_id, msg.key_version, status=0x03))
        else:
            self.pending_button_client = (msg.client_id, msg.key_version, msg.public_key)

        return ("pending_button", build_hs_pending_button(msg.client_id, msg.key_version, status=0x01))

    def lookup_for_session_start(
        self,
        msg: MdTsHandshakeMessage,
        *,
        require_permanent: bool,
    ) -> tuple[_AuthorizedClientEntry | None, bytes | None]:
        """Validate `msg_type = 4` against the authorization table (`status in (1,2)` for BLE, `status == 2` for Zenoh)."""
        entry = self.clients.get(msg.client_id)
        if entry is None:
            return None, build_hs_error(msg.client_id, msg.key_version, MD_TS_ERR_UNAUTHORIZED)
        if entry.key_version != msg.key_version:
            return None, build_hs_error(msg.client_id, msg.key_version, MD_TS_ERR_PUBKEY_MISMATCH)
        if require_permanent and entry.status != AUTH_STATUS_PERMANENT:
            return None, build_hs_error(msg.client_id, msg.key_version, MD_TS_ERR_PENDING_NOT_FINALIZED)
        return entry, None

    def promote_permanent(self, client_id: bytes) -> None:
        entry = self.clients.get(client_id)
        if entry is not None:
            entry.status = AUTH_STATUS_PERMANENT
