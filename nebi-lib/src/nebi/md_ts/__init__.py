"""Byte-accurate `MD-TS` (`MD-TS-HS` + `MD-TS` AEAD) encrypted transport for Nebi BLE & Zenoh.

1. Shared Identity & Authorization Store:
   - The robot's BLE peer (GATT `0000FFF0`) and its Zenoh-over-UCP peer (`:9007`) share one static X25519
     keypair and one table of authorized client keys.
   - Authorizing `nebi`'s static X25519 identity once via BLE + 3-5s physical button hold permanently
     authorizes `nebi` (`status = 2`) for BOTH BLE (`0x55`/`0x56`/`flags=0x04`) and Zenoh (`app/security/session`
     + `MdTsZenohEnvelope`) across SoftAP (`192.168.7.1`) and Home Wi-Fi LAN (`STA_CONNECTED`).

2. Cryptographic Primitives:
   - Static ECDH: X25519 (`shared_secret = X25519(local_static_priv, peer_static_pub)`).
   - Transcript Hash: `SHA-256` over 168 (`0xA8`) bytes prefixed with `b"MD-TS-HS\\x00\\x01\\x01"`.
   - HKDF Salt: `SHA-256(client_nonce_32B || server_nonce_32B)` (64 bytes).
   - Directional Session Keys (`dir=0` C->S, `dir=1` S->C):
     `HKDF-SHA256(ikm=shared_secret, salt=salt, info=b"MD-TS-KEY\\x00\\x01\\x01" + bytes([transport_type, dir]) + transcript_hash, length=32)`.
   - AEAD Cipher: `AES-256-GCM` with 12-byte IV `b"\\x00"*8 + u32_be(seq)` and 16-byte tag:
     - `seq = 0` is reserved exclusively for handshake confirmation (`msg_type = 6` / `msg_type = 7`).
     - Application frames MUST use `seq >= 1` (`1, 2, 3, ...`), protected by a 1024-packet sliding replay window.

Package layout: `wire` (constants, envelopes, MD-TS-HS messages, AAD), `session` (key schedule + `MdTsSession`),
`identity` (nebi's persistent client identity + paired robot keys), `server` (robot-side handshake state
machine). The simulated robot authority (`MdTsServerAuthority`) lives in `nebi.sim.md_ts`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from nebi.md_ts.identity import (
    DEFAULT_IDENTITY_STORE,
    MdTsClientIdentity,
    MdTsIdentityStore,
    default_identity_path,
    write_private_file_atomic,
)
from nebi.md_ts.server import (
    MdTsAuthority,
    MdTsAuthorizedClient,
    MdTsConfirmResult,
    MdTsServerHandshake,
)
from nebi.md_ts.session import (
    MdTsSession,
    compute_transcript_hash,
    derive_md_ts_session_keys,
    generate_x25519_keypair,
)
from nebi.md_ts.wire import (
    MD_TS_BLE_MAX_PLAINTEXT,
    MD_TS_DIR_C2S,
    MD_TS_DIR_S2C,
    MD_TS_ERR_AUTH_TABLE_FULL,
    MD_TS_ERR_INTERNAL,
    MD_TS_ERR_PENDING_NOT_FINALIZED,
    MD_TS_ERR_PUBKEY_MISMATCH,
    MD_TS_ERR_SESSION_BUSY,
    MD_TS_ERR_TIMEOUT,
    MD_TS_ERR_UNAUTHORIZED,
    MD_TS_ERROR_NAMES,
    MD_TS_HS_CLIENT_CONFIRM,
    MD_TS_HS_CLIENT_HELLO,
    MD_TS_HS_ERROR,
    MD_TS_HS_HEADER_LEN,
    MD_TS_HS_PENDING_BUTTON,
    MD_TS_HS_SERVER_CONFIRM,
    MD_TS_HS_SERVER_HELLO,
    MD_TS_HS_SESSION_CHALLENGE,
    MD_TS_HS_SESSION_START,
    MD_TS_RECORD_OVERHEAD,
    MD_TS_REPLAY_WINDOW_SIZE,
    MD_TS_ROLE_CLIENT,
    MD_TS_ROLE_SERVER,
    MD_TS_TRANSPORT_BLE,
    MD_TS_TRANSPORT_ZENOH,
    MD_TS_VERSION,
    ZENOH_APP_SECURITY_KEYEXPR,
    ZENOH_DEVICE_SECURITY_KEYEXPR,
    ZENOH_SECURITY_SESSION_TOPIC,
    MdTsHandshakeMessage,
    _random_nonzero_bytes,
    _validate_nonzero,
    build_ble_aad,
    build_hs_client_hello,
    build_hs_confirm,
    build_hs_error,
    build_hs_pending_button,
    build_hs_server_hello,
    build_hs_session_challenge,
    build_hs_session_start,
    build_zenoh_aad,
    decode_protobuf_varint,
    decode_zenoh_envelope,
    encode_protobuf_varint,
    encode_zenoh_enc_envelope,
    encode_zenoh_hs_envelope,
    pack_hs_header,
    parse_hs_message,
)

if TYPE_CHECKING:
    from nebi.sim.md_ts import MdTsServerAuthority as MdTsServerAuthority

__all__ = [
    "DEFAULT_IDENTITY_STORE",
    "MD_TS_BLE_MAX_PLAINTEXT",
    "MD_TS_DIR_C2S",
    "MD_TS_DIR_S2C",
    "MD_TS_ERROR_NAMES",
    "MD_TS_ERR_AUTH_TABLE_FULL",
    "MD_TS_ERR_INTERNAL",
    "MD_TS_ERR_PENDING_NOT_FINALIZED",
    "MD_TS_ERR_PUBKEY_MISMATCH",
    "MD_TS_ERR_SESSION_BUSY",
    "MD_TS_ERR_TIMEOUT",
    "MD_TS_ERR_UNAUTHORIZED",
    "MD_TS_HS_CLIENT_CONFIRM",
    "MD_TS_HS_CLIENT_HELLO",
    "MD_TS_HS_ERROR",
    "MD_TS_HS_HEADER_LEN",
    "MD_TS_HS_PENDING_BUTTON",
    "MD_TS_HS_SERVER_CONFIRM",
    "MD_TS_HS_SERVER_HELLO",
    "MD_TS_HS_SESSION_CHALLENGE",
    "MD_TS_HS_SESSION_START",
    "MD_TS_RECORD_OVERHEAD",
    "MD_TS_REPLAY_WINDOW_SIZE",
    "MD_TS_ROLE_CLIENT",
    "MD_TS_ROLE_SERVER",
    "MD_TS_TRANSPORT_BLE",
    "MD_TS_TRANSPORT_ZENOH",
    "MD_TS_VERSION",
    "ZENOH_APP_SECURITY_KEYEXPR",
    "ZENOH_DEVICE_SECURITY_KEYEXPR",
    "ZENOH_SECURITY_SESSION_TOPIC",
    "MdTsAuthority",
    "MdTsAuthorizedClient",
    "MdTsClientIdentity",
    "MdTsConfirmResult",
    "MdTsHandshakeMessage",
    "MdTsIdentityStore",
    "MdTsServerAuthority",
    "MdTsServerHandshake",
    "MdTsSession",
    "_random_nonzero_bytes",  # private helpers kept importable for older callers
    "_validate_nonzero",
    "build_ble_aad",
    "build_hs_client_hello",
    "build_hs_confirm",
    "build_hs_error",
    "build_hs_pending_button",
    "build_hs_server_hello",
    "build_hs_session_challenge",
    "build_hs_session_start",
    "build_zenoh_aad",
    "compute_transcript_hash",
    "decode_protobuf_varint",
    "decode_zenoh_envelope",
    "default_identity_path",
    "derive_md_ts_session_keys",
    "encode_protobuf_varint",
    "encode_zenoh_enc_envelope",
    "encode_zenoh_hs_envelope",
    "generate_x25519_keypair",
    "pack_hs_header",
    "parse_hs_message",
    "write_private_file_atomic",
]


def __getattr__(name: str) -> Any:
    # `MdTsServerAuthority` is simulator infrastructure (nebi.sim.md_ts), resolved lazily.
    if name == "MdTsServerAuthority":
        from nebi.sim.md_ts import MdTsServerAuthority

        return MdTsServerAuthority
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
