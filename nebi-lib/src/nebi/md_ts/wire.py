"""MD-TS wire format: constants, Protobuf varint / `MdTsZenohEnvelope`, `MD-TS-HS` messages and AEAD AAD layouts.

Pure byte-level code (no crypto state); see `nebi.md_ts` for the protocol overview.
"""

from __future__ import annotations

import secrets
import struct
from dataclasses import dataclass

MD_TS_VERSION = b"\x01\x01"

MD_TS_ROLE_CLIENT = 0
MD_TS_ROLE_SERVER = 1

MD_TS_TRANSPORT_BLE = 1
MD_TS_TRANSPORT_ZENOH = 2

MD_TS_DIR_C2S = 0
MD_TS_DIR_S2C = 1

# MD-TS-HS message types
MD_TS_HS_CLIENT_HELLO = 1       # BLE C->S (55B): hdr(1) + client_static_pub(32B)
MD_TS_HS_SERVER_HELLO = 2       # BLE S->C (55B): hdr(2) + robot_static_pub(32B)
MD_TS_HS_PENDING_BUTTON = 3     # BLE S->C (24B): hdr(3) + status_u8(1B) (0x01=wait button, 0x03=busy)
MD_TS_HS_SESSION_START = 4      # BLE/Zenoh C->S (55B): hdr(4) + client_nonce(32B)
MD_TS_HS_SESSION_CHALLENGE = 5  # BLE/Zenoh S->C (63B): hdr(5) + server_nonce(32B) + channel_id(8B)
MD_TS_HS_CLIENT_CONFIRM = 6     # BLE/Zenoh C->S (55B pt -> 87B AEAD seq=0, dir=0): hdr(6) + transcript_hash(32B)
MD_TS_HS_SERVER_CONFIRM = 7     # BLE/Zenoh S->C (55B pt -> 87B AEAD seq=0, dir=1): hdr(7) + transcript_hash(32B)
MD_TS_HS_ERROR = 0x7F           # BLE/Zenoh (25B): hdr(0x7F) + u16_be(error_code)

# Handshake error codes (same on the BLE and Zenoh peers)
MD_TS_ERR_UNAUTHORIZED = 0x0003
MD_TS_ERR_PUBKEY_MISMATCH = 0x0004
MD_TS_ERR_SESSION_BUSY = 0x0005
MD_TS_ERR_INTERNAL = 0x0007
MD_TS_ERR_PENDING_NOT_FINALIZED = 0x0008
MD_TS_ERR_TIMEOUT = 0x0009
MD_TS_ERR_AUTH_TABLE_FULL = 0x000A

MD_TS_ERROR_NAMES: dict[int, str] = {
    MD_TS_ERR_UNAUTHORIZED: "UNAUTHORIZED_CLIENT",
    MD_TS_ERR_PUBKEY_MISMATCH: "PUBLIC_KEY_MISMATCH",
    MD_TS_ERR_SESSION_BUSY: "HANDSHAKE_SESSION_BUSY",
    MD_TS_ERR_INTERNAL: "INTERNAL_SECURITY_ERROR",
    MD_TS_ERR_PENDING_NOT_FINALIZED: "BUTTON_AUTH_NOT_FINALIZED",
    MD_TS_ERR_TIMEOUT: "HANDSHAKE_TIMEOUT",
    MD_TS_ERR_AUTH_TABLE_FULL: "AUTHORIZATION_TABLE_FULL",
}

MD_TS_HS_HEADER_LEN = 23
MD_TS_RECORD_OVERHEAD = 32  # 8B channel_id + 8B seq + 16B GCM tag
MD_TS_BLE_MAX_PLAINTEXT = 218  # 255 max V0 frame - (4B ble_hdr + 32B record overhead + 1B crc8)
MD_TS_REPLAY_WINDOW_SIZE = 1024

ZENOH_SECURITY_SESSION_TOPIC = "/security/session"
ZENOH_APP_SECURITY_KEYEXPR = "app/security/session"
ZENOH_DEVICE_SECURITY_KEYEXPR = "device/security/session"


def _random_nonzero_bytes(length: int) -> bytes:
    while True:
        raw = secrets.token_bytes(length)
        if any(raw):
            return raw


def _validate_nonzero(buf: bytes, expected_len: int, label: str) -> bytes:
    b = bytes(buf)
    if len(b) != expected_len:
        raise ValueError(f"{label} must be {expected_len} bytes (got {len(b)})")
    if not any(b):
        raise ValueError(f"{label} must not be all-zero")
    return b


def encode_protobuf_varint(value: int) -> bytes:
    """Encode a non-negative integer as a Protobuf base-128 varint."""
    if value < 0:
        raise ValueError("Varint must be non-negative")
    out = bytearray()
    v = int(value)
    while v >= 0x80:
        out.append((v & 0x7F) | 0x80)
        v >>= 7
    out.append(v & 0x7F)
    return bytes(out)


def decode_protobuf_varint(buf: bytes | memoryview, offset: int = 0) -> tuple[int, int]:
    """Decode a Protobuf varint starting at `offset`, returning `(value, next_offset)`."""
    result = 0
    shift = 0
    pos = offset
    n = len(buf)
    while pos < n:
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7
        if shift >= 64:
            raise ValueError("Varint overflow")
    raise ValueError("Truncated protobuf varint")


def encode_zenoh_hs_envelope(hs_bytes: bytes) -> bytes:
    """Wrap plaintext `MD-TS-HS` (`msg_type` 4, 5, 0x7F) in `MdTsZenohEnvelope` (field 5 `bytes`)."""
    payload = bytes(hs_bytes)
    return b"\x2a" + encode_protobuf_varint(len(payload)) + payload


def encode_zenoh_enc_envelope(record_bytes: bytes) -> bytes:
    """Wrap an `MD-TS` AEAD record (`msg_type` 6, 7, or encrypted topic data) in `MdTsZenohEnvelope` (`field 3 = 1, field 4 = record`)."""
    rec = bytes(record_bytes)
    return b"\x18\x01\x22" + encode_protobuf_varint(len(rec)) + rec


def decode_zenoh_envelope(raw: bytes) -> tuple[str, bytes] | None:
    """Attempt to parse `raw` as an `MdTsZenohEnvelope`.

    Returns:
      - `("handshake", hs_bytes)` when `field 3 == 0` and `field 5` (`0x2A`) is non-empty
      - `("encrypted", record_bytes)` when `field 3 == 1` (`0x18 0x01`) and `field 4` (`0x22`) is non-empty
      - `None` if `raw` is a normal plaintext Nebi Protobuf message (`field 1 = WireHeader`, `field 2 = Data`)
        or malformed.
    """
    if not raw:
        return None
    pos = 0
    n = len(raw)
    f3_mode = 0
    f4_record: bytes | None = None
    f5_hs: bytes | None = None
    saw_envelope_field = False

    try:
        while pos < n:
            tag, pos = decode_protobuf_varint(raw, pos)
            field_num = tag >> 3
            wire_type = tag & 0x07
            if field_num in (1, 2):
                # Normal Nebi Protobuf messages always start with WireHeader (1) & Data (2).
                return None
            if field_num == 3 and wire_type == 0:
                f3_mode, pos = decode_protobuf_varint(raw, pos)
                saw_envelope_field = True
            elif field_num == 4 and wire_type == 2:
                ln, pos = decode_protobuf_varint(raw, pos)
                if pos + ln > n:
                    return None
                f4_record = bytes(raw[pos : pos + ln])
                pos += ln
                saw_envelope_field = True
            elif field_num == 5 and wire_type == 2:
                ln, pos = decode_protobuf_varint(raw, pos)
                if pos + ln > n:
                    return None
                f5_hs = bytes(raw[pos : pos + ln])
                pos += ln
                saw_envelope_field = True
            else:
                return None
    except ValueError:
        return None

    if not saw_envelope_field:
        return None
    if f3_mode == 1 and f4_record:
        return ("encrypted", f4_record)
    if f3_mode == 0 and f5_hs:
        return ("handshake", f5_hs)
    return None


@dataclass(frozen=True, slots=True)
class MdTsHandshakeMessage:
    """Decoded `MD-TS-HS` message (`23B` common header + type-specific body)."""

    msg_type: int
    client_id: bytes
    key_version: int
    public_key: bytes = b""
    pending_status: int = 0
    nonce: bytes = b""
    channel_id: bytes = b""
    transcript_hash: bytes = b""
    error_code: int = 0

    @property
    def error_name(self) -> str:
        return MD_TS_ERROR_NAMES.get(self.error_code, f"MD_TS_ERR_0x{self.error_code:04X}")


def pack_hs_header(msg_type: int, client_id: bytes, key_version: int) -> bytes:
    cid = _validate_nonzero(client_id, 16, "client_id")
    if key_version <= 0 or key_version > 0xFFFFFFFF:
        raise ValueError(f"Invalid key_version {key_version}")
    return MD_TS_VERSION + bytes([msg_type & 0xFF]) + cid + struct.pack(">I", key_version)


def build_hs_client_hello(client_id: bytes, key_version: int, client_static_pub: bytes) -> bytes:
    pub = _validate_nonzero(client_static_pub, 32, "client_static_pub")
    return pack_hs_header(MD_TS_HS_CLIENT_HELLO, client_id, key_version) + pub


def build_hs_server_hello(client_id: bytes, key_version: int, robot_static_pub: bytes) -> bytes:
    pub = _validate_nonzero(robot_static_pub, 32, "robot_static_pub")
    return pack_hs_header(MD_TS_HS_SERVER_HELLO, client_id, key_version) + pub


def build_hs_pending_button(client_id: bytes, key_version: int, status: int = 0x01) -> bytes:
    return pack_hs_header(MD_TS_HS_PENDING_BUTTON, client_id, key_version) + bytes([status & 0xFF])


def build_hs_session_start(client_id: bytes, key_version: int, client_nonce: bytes) -> bytes:
    nonce = _validate_nonzero(client_nonce, 32, "client_nonce")
    return pack_hs_header(MD_TS_HS_SESSION_START, client_id, key_version) + nonce


def build_hs_session_challenge(
    client_id: bytes,
    key_version: int,
    server_nonce: bytes,
    channel_id: bytes,
) -> bytes:
    snonce = _validate_nonzero(server_nonce, 32, "server_nonce")
    cid = _validate_nonzero(channel_id, 8, "channel_id")
    return pack_hs_header(MD_TS_HS_SESSION_CHALLENGE, client_id, key_version) + snonce + cid


def build_hs_confirm(msg_type: int, client_id: bytes, key_version: int, transcript_hash: bytes) -> bytes:
    if msg_type not in (MD_TS_HS_CLIENT_CONFIRM, MD_TS_HS_SERVER_CONFIRM):
        raise ValueError(f"Invalid confirm msg_type {msg_type}")
    th = _validate_nonzero(transcript_hash, 32, "transcript_hash")
    return pack_hs_header(msg_type, client_id, key_version) + th


def build_hs_error(client_id: bytes, key_version: int, error_code: int) -> bytes:
    cid = bytes(client_id) if len(client_id) == 16 and any(client_id) else (b"\x01" + b"\x00" * 15)
    kv = key_version if 0 < key_version <= 0xFFFFFFFF else 1
    return pack_hs_header(MD_TS_HS_ERROR, cid, kv) + struct.pack(">H", error_code & 0xFFFF)


def parse_hs_message(raw: bytes) -> MdTsHandshakeMessage:
    """Parse and validate a raw `MD-TS-HS` message."""
    data = bytes(raw)
    if len(data) < MD_TS_HS_HEADER_LEN:
        raise ValueError(f"Truncated MD-TS-HS message ({len(data)} bytes < 23)")
    if data[:2] != MD_TS_VERSION:
        raise ValueError(f"Unsupported MD-TS-HS version {data[:2].hex()}")
    msg_type = data[2]
    client_id = data[3:19]
    (key_version,) = struct.unpack_from(">I", data, 19)
    if not any(client_id) or key_version == 0:
        raise ValueError("Invalid zero client_id or key_version in MD-TS-HS header")
    body = data[23:]

    if msg_type in (MD_TS_HS_CLIENT_HELLO, MD_TS_HS_SERVER_HELLO):
        if len(body) != 32 or not any(body):
            raise ValueError(f"Invalid public key body in msg_type={msg_type}")
        return MdTsHandshakeMessage(msg_type=msg_type, client_id=client_id, key_version=key_version, public_key=body)

    if msg_type == MD_TS_HS_PENDING_BUTTON:
        if len(body) != 1:
            raise ValueError(f"Invalid pending button body length {len(body)}")
        return MdTsHandshakeMessage(
            msg_type=msg_type,
            client_id=client_id,
            key_version=key_version,
            pending_status=body[0],
        )

    if msg_type == MD_TS_HS_SESSION_START:
        if len(body) != 32 or not any(body):
            raise ValueError("Invalid client_nonce in msg_type=4")
        return MdTsHandshakeMessage(msg_type=msg_type, client_id=client_id, key_version=key_version, nonce=body)

    if msg_type == MD_TS_HS_SESSION_CHALLENGE:
        if len(body) != 40:
            raise ValueError(f"Invalid msg_type=5 body length {len(body)} (expected 40)")
        snonce, ch_id = body[:32], body[32:40]
        if not any(snonce) or not any(ch_id):
            raise ValueError("Invalid zero server_nonce or channel_id in msg_type=5")
        return MdTsHandshakeMessage(
            msg_type=msg_type,
            client_id=client_id,
            key_version=key_version,
            nonce=snonce,
            channel_id=ch_id,
        )

    if msg_type in (MD_TS_HS_CLIENT_CONFIRM, MD_TS_HS_SERVER_CONFIRM):
        if len(body) != 32 or not any(body):
            raise ValueError(f"Invalid transcript_hash in msg_type={msg_type}")
        return MdTsHandshakeMessage(
            msg_type=msg_type,
            client_id=client_id,
            key_version=key_version,
            transcript_hash=body,
        )

    if msg_type == MD_TS_HS_ERROR:
        if len(body) != 2:
            raise ValueError(f"Invalid error body length {len(body)}")
        (err_code,) = struct.unpack_from(">H", body, 0)
        return MdTsHandshakeMessage(
            msg_type=msg_type,
            client_id=client_id,
            key_version=key_version,
            error_code=err_code,
        )

    raise ValueError(f"Unknown MD-TS-HS msg_type 0x{msg_type:02X}")


def build_ble_aad(direction: int, channel_id: bytes, seq: int, ble_hdr: bytes) -> bytes:
    """Construct the exact 35-byte (`0x23`) BLE AAD."""
    if len(ble_hdr) != 4:
        raise ValueError("ble_hdr must be 4 bytes")
    aad = (
        b"MD-TS-AAD\x00\x01\x01\x01"
        + bytes([direction & 0xFF])
        + b"\x01"
        + bytes(channel_id)
        + struct.pack(">Q", seq)
        + bytes(ble_hdr)
    )
    assert len(aad) == 35
    return aad


def build_zenoh_aad(
    direction: int,
    channel_id: bytes,
    seq: int,
    keyexpr: str,
    encoding: str = "application/protobuf",
) -> bytes:
    """Construct the exact `55 + len(keyexpr)` Zenoh AAD."""
    ke_bytes = keyexpr.lstrip("/").encode("ascii")
    enc_bytes = encoding.encode("ascii")
    return (
        b"MD-TS-AAD\x00\x01\x01\x02"
        + bytes([direction & 0xFF])
        + b"\x01"
        + bytes(channel_id)
        + struct.pack(">Q", seq)
        + struct.pack(">H", len(ke_bytes))
        + ke_bytes
        + struct.pack(">H", len(enc_bytes))
        + enc_bytes
    )
