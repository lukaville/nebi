"""MD-TS cryptography: transcript hash, X25519 + HKDF key schedule and the stateful AES-256-GCM `MdTsSession`."""

from __future__ import annotations

import hashlib
import hmac
import struct

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from nebi.md_ts.wire import (
    MD_TS_BLE_MAX_PLAINTEXT,
    MD_TS_DIR_C2S,
    MD_TS_DIR_S2C,
    MD_TS_HS_CLIENT_CONFIRM,
    MD_TS_HS_SERVER_CONFIRM,
    MD_TS_RECORD_OVERHEAD,
    MD_TS_REPLAY_WINDOW_SIZE,
    MD_TS_ROLE_CLIENT,
    MD_TS_ROLE_SERVER,
    MD_TS_TRANSPORT_BLE,
    MD_TS_TRANSPORT_ZENOH,
    MD_TS_VERSION,
    ZENOH_APP_SECURITY_KEYEXPR,
    ZENOH_DEVICE_SECURITY_KEYEXPR,
    MdTsHandshakeMessage,
    _validate_nonzero,
    build_ble_aad,
    build_hs_confirm,
    build_zenoh_aad,
    encode_zenoh_enc_envelope,
    parse_hs_message,
)

# BLE V0 frame bytes that enter the AEAD AAD. They mirror `nebi.ble` (which imports this package, so the
# constants cannot be imported from there): SOF, the MD-TS flag, the handshake opcodes, and the frame overhead
# around a record (4B header + 1B CRC8 + 32B record overhead = 37).
_BLE_SOF = 0x55
_BLE_FLAG_MD_TS = 0x04
_BLE_CMD_MD_TS_CLIENT = 0x55
_BLE_CMD_MD_TS_SERVER = 0x56
_BLE_FRAME_OVERHEAD = 5
_BLE_RECORD_FRAME_OVERHEAD = _BLE_FRAME_OVERHEAD + MD_TS_RECORD_OVERHEAD  # 37


def generate_x25519_keypair() -> tuple[bytes, bytes]:
    """Fresh static X25519 key pair as raw `(private_32B, public_32B)`."""
    priv = X25519PrivateKey.generate()
    priv_b = priv.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_b = priv.public_key().public_bytes(encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)
    return priv_b, pub_b


def compute_transcript_hash(
    *,
    transport_type: int,
    client_id: bytes,
    key_version: int,
    client_static_pub: bytes,
    robot_static_pub: bytes,
    client_nonce: bytes,
    server_nonce: bytes,
    channel_id: bytes,
) -> bytes:
    """Compute the 32-byte `SHA-256` transcript hash over the exact 168-byte (`0xA8`) buffer."""
    if transport_type not in (MD_TS_TRANSPORT_BLE, MD_TS_TRANSPORT_ZENOH):
        raise ValueError(f"Invalid MD-TS transport_type {transport_type}")
    preimage = (
        b"MD-TS-HS\x00"
        + MD_TS_VERSION
        + bytes([transport_type])
        + _validate_nonzero(client_id, 16, "client_id")
        + struct.pack(">I", key_version)
        + _validate_nonzero(client_static_pub, 32, "client_static_pub")
        + _validate_nonzero(robot_static_pub, 32, "robot_static_pub")
        + _validate_nonzero(client_nonce, 32, "client_nonce")
        + _validate_nonzero(server_nonce, 32, "server_nonce")
        + _validate_nonzero(channel_id, 8, "channel_id")
    )
    assert len(preimage) == 168
    return hashlib.sha256(preimage).digest()


def derive_md_ts_session_keys(
    *,
    local_private_key_bytes: bytes,
    peer_public_key_bytes: bytes,
    transport_type: int,
    client_id: bytes,
    key_version: int,
    client_static_pub: bytes,
    robot_static_pub: bytes,
    client_nonce: bytes,
    server_nonce: bytes,
    channel_id: bytes,
) -> tuple[bytes, bytes, bytes]:
    """Perform X25519 + SHA-256 + HKDF-SHA256, returning `(transcript_hash, key_c2s, key_s2c)`."""
    priv = X25519PrivateKey.from_private_bytes(_validate_nonzero(local_private_key_bytes, 32, "local_private_key"))
    pub = X25519PublicKey.from_public_bytes(_validate_nonzero(peer_public_key_bytes, 32, "peer_public_key"))
    shared_secret = priv.exchange(pub)
    if not any(shared_secret):
        raise ValueError("Rejected all-zero X25519 shared secret")

    transcript_hash = compute_transcript_hash(
        transport_type=transport_type,
        client_id=client_id,
        key_version=key_version,
        client_static_pub=client_static_pub,
        robot_static_pub=robot_static_pub,
        client_nonce=client_nonce,
        server_nonce=server_nonce,
        channel_id=channel_id,
    )
    salt = hashlib.sha256(client_nonce + server_nonce).digest()

    keys: list[bytes] = []
    for direction in (MD_TS_DIR_C2S, MD_TS_DIR_S2C):
        info = b"MD-TS-KEY\x00\x01\x01" + bytes([transport_type, direction]) + transcript_hash
        assert len(info) == 46
        hkdf = HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=info)
        keys.append(hkdf.derive(shared_secret))

    return transcript_hash, keys[0], keys[1]


class MdTsSession:
    """Stateful `MD-TS` AEAD channel for BLE (`transport_type=1`) or Zenoh (`transport_type=2`)."""

    def __init__(
        self,
        *,
        role: int,
        transport_type: int,
        channel_id: bytes,
        transcript_hash: bytes,
        key_c2s: bytes,
        key_s2c: bytes,
    ) -> None:
        if role not in (MD_TS_ROLE_CLIENT, MD_TS_ROLE_SERVER):
            raise ValueError(f"Invalid role {role}")
        if transport_type not in (MD_TS_TRANSPORT_BLE, MD_TS_TRANSPORT_ZENOH):
            raise ValueError(f"Invalid transport_type {transport_type}")
        self.role = role
        self.transport_type = transport_type
        self.channel_id = _validate_nonzero(channel_id, 8, "channel_id")
        self.transcript_hash = _validate_nonzero(transcript_hash, 32, "transcript_hash")

        if role == MD_TS_ROLE_CLIENT:
            self.tx_dir = MD_TS_DIR_C2S
            self.rx_dir = MD_TS_DIR_S2C
            self._tx_aead = AESGCM(key_c2s)
            self._rx_aead = AESGCM(key_s2c)
        else:
            self.tx_dir = MD_TS_DIR_S2C
            self.rx_dir = MD_TS_DIR_C2S
            self._tx_aead = AESGCM(key_s2c)
            self._rx_aead = AESGCM(key_c2s)

        self.tx_seq: int = 1
        self._rx_max_seq: int = 0
        self._rx_bitmap: int = 0  # 1024-bit integer sliding anti-replay window
        self.handshake_confirmed: bool = False

    @classmethod
    def from_handshake(
        cls,
        *,
        role: int,
        transport_type: int,
        local_private_key_bytes: bytes,
        peer_public_key_bytes: bytes,
        client_id: bytes,
        key_version: int,
        client_static_pub: bytes,
        robot_static_pub: bytes,
        client_nonce: bytes,
        server_nonce: bytes,
        channel_id: bytes,
    ) -> MdTsSession:
        th, k0, k1 = derive_md_ts_session_keys(
            local_private_key_bytes=local_private_key_bytes,
            peer_public_key_bytes=peer_public_key_bytes,
            transport_type=transport_type,
            client_id=client_id,
            key_version=key_version,
            client_static_pub=client_static_pub,
            robot_static_pub=robot_static_pub,
            client_nonce=client_nonce,
            server_nonce=server_nonce,
            channel_id=channel_id,
        )
        return cls(
            role=role,
            transport_type=transport_type,
            channel_id=channel_id,
            transcript_hash=th,
            key_c2s=k0,
            key_s2c=k1,
        )

    def _ble_handshake_cmd(self, *, tx: bool) -> int:
        """BLE opcode of a handshake frame we send (`tx`) or receive: 0x55 client->robot, 0x56 robot->client."""
        client_sends = (self.role == MD_TS_ROLE_CLIENT) == tx
        return _BLE_CMD_MD_TS_CLIENT if client_sends else _BLE_CMD_MD_TS_SERVER

    @staticmethod
    def _iv_for_seq(seq: int) -> bytes:
        if seq < 0 or seq > 0xFFFFFFFF:
            raise ValueError(f"MD-TS sequence number {seq} exceeds 32-bit IV bound")
        return b"\x00" * 8 + struct.pack(">I", seq)

    def _check_replay(self, seq: int) -> None:
        if seq <= 0 or seq > 0xFFFFFFFF:
            raise ValueError(f"Invalid application sequence number {seq}")
        if self._rx_max_seq == 0:
            return
        if seq > self._rx_max_seq:
            return
        diff = self._rx_max_seq - seq
        if diff >= MD_TS_REPLAY_WINDOW_SIZE:
            raise ValueError(f"Packet seq={seq} outside 1024-packet anti-replay window (max={self._rx_max_seq})")
        if (self._rx_bitmap >> diff) & 1:
            raise ValueError(f"Replayed packet seq={seq} rejected")

    def _commit_replay(self, seq: int) -> None:
        if self._rx_max_seq == 0:
            self._rx_max_seq = seq
            self._rx_bitmap = 1
            return
        if seq > self._rx_max_seq:
            shift = seq - self._rx_max_seq
            if shift >= MD_TS_REPLAY_WINDOW_SIZE:
                self._rx_bitmap = 1
            else:
                mask = (1 << MD_TS_REPLAY_WINDOW_SIZE) - 1
                self._rx_bitmap = (((self._rx_bitmap << shift) | 1) & mask)
            self._rx_max_seq = seq
        else:
            diff = self._rx_max_seq - seq
            self._rx_bitmap |= 1 << diff

    # --- Handshake Confirmation (`seq == 0`) ---

    def seal_handshake_confirm(
        self,
        *,
        client_id: bytes,
        key_version: int,
        ble_cmd_id: int | None = None,
        zenoh_keyexpr: str | None = None,
    ) -> bytes:
        """Encrypt `msg_type = 6` (client) or `msg_type = 7` (server) at `seq = 0` (`87B` record)."""
        msg_type = MD_TS_HS_CLIENT_CONFIRM if self.role == MD_TS_ROLE_CLIENT else MD_TS_HS_SERVER_CONFIRM
        pt = build_hs_confirm(msg_type, client_id, key_version, self.transcript_hash)
        seq = 0
        if self.transport_type == MD_TS_TRANSPORT_BLE:
            cmd = ble_cmd_id if ble_cmd_id is not None else self._ble_handshake_cmd(tx=True)
            ble_hdr = bytes([_BLE_SOF, len(pt) + _BLE_RECORD_FRAME_OVERHEAD, _BLE_FLAG_MD_TS, cmd & 0xFF])
            aad = build_ble_aad(self.tx_dir, self.channel_id, seq, ble_hdr)
        else:
            ke = zenoh_keyexpr or (
                ZENOH_APP_SECURITY_KEYEXPR if self.role == MD_TS_ROLE_CLIENT else ZENOH_DEVICE_SECURITY_KEYEXPR
            )
            aad = build_zenoh_aad(self.tx_dir, self.channel_id, seq, ke)
        ct_tag = self._tx_aead.encrypt(self._iv_for_seq(seq), pt, aad)
        return self.channel_id + struct.pack(">Q", seq) + ct_tag

    def open_handshake_confirm(
        self,
        record: bytes,
        *,
        expected_client_id: bytes,
        expected_key_version: int,
        ble_hdr: bytes | None = None,
        zenoh_keyexpr: str | None = None,
    ) -> MdTsHandshakeMessage:
        """Decrypt and verify `msg_type = 6` or `msg_type = 7` at `seq == 0`."""
        rec = bytes(record)
        if len(rec) < MD_TS_RECORD_OVERHEAD:
            raise ValueError(f"Truncated MD-TS confirm record ({len(rec)} bytes)")
        ch_id = rec[:8]
        (seq,) = struct.unpack_from(">Q", rec, 8)
        if ch_id != self.channel_id:
            raise ValueError("Channel ID mismatch on MD-TS handshake confirm")
        if seq != 0:
            raise ValueError(f"Handshake confirm must use seq=0 (got {seq})")

        if self.transport_type == MD_TS_TRANSPORT_BLE:
            if ble_hdr is None:
                cmd = self._ble_handshake_cmd(tx=False)
                ble_hdr = bytes([_BLE_SOF, (len(rec) - MD_TS_RECORD_OVERHEAD) + _BLE_RECORD_FRAME_OVERHEAD, _BLE_FLAG_MD_TS, cmd])
            aad = build_ble_aad(self.rx_dir, self.channel_id, seq, ble_hdr)
        else:
            ke = zenoh_keyexpr or (
                ZENOH_DEVICE_SECURITY_KEYEXPR if self.role == MD_TS_ROLE_CLIENT else ZENOH_APP_SECURITY_KEYEXPR
            )
            aad = build_zenoh_aad(self.rx_dir, self.channel_id, seq, ke)

        try:
            pt = self._rx_aead.decrypt(self._iv_for_seq(seq), rec[16:], aad)
        except InvalidTag as exc:
            raise ValueError("AES-256-GCM tag verification failed on handshake confirm") from exc

        parsed = parse_hs_message(pt)
        expected_type = MD_TS_HS_SERVER_CONFIRM if self.role == MD_TS_ROLE_CLIENT else MD_TS_HS_CLIENT_CONFIRM
        if parsed.msg_type != expected_type:
            raise ValueError(f"Unexpected confirm msg_type={parsed.msg_type} (expected {expected_type})")
        if parsed.client_id != expected_client_id or parsed.key_version != expected_key_version:
            raise ValueError("Client identity mismatch inside handshake confirm")
        if not hmac.compare_digest(parsed.transcript_hash, self.transcript_hash):
            raise ValueError("Transcript hash mismatch inside handshake confirm")

        self.handshake_confirmed = True
        return parsed

    # --- Application Records (`seq >= 1`) ---

    def seal_ble_payload(self, cmd_id: int, plaintext: bytes) -> tuple[bytes, bytes]:
        """Encrypt an application BLE payload, returning `(ble_hdr_4B, record_bytes)`."""
        if self.transport_type != MD_TS_TRANSPORT_BLE:
            raise RuntimeError("seal_ble_payload called on non-BLE session")
        pt = bytes(plaintext)
        if len(pt) > MD_TS_BLE_MAX_PLAINTEXT:
            raise ValueError(f"BLE MD-TS plaintext too large ({len(pt)} > {MD_TS_BLE_MAX_PLAINTEXT})")
        seq = self.tx_seq
        if seq <= 0 or seq > 0xFFFFFFFF:
            raise OverflowError("MD-TS TX sequence counter exhausted")
        self.tx_seq += 1

        ble_hdr = bytes([_BLE_SOF, len(pt) + _BLE_RECORD_FRAME_OVERHEAD, _BLE_FLAG_MD_TS, cmd_id & 0xFF])
        aad = build_ble_aad(self.tx_dir, self.channel_id, seq, ble_hdr)
        ct_tag = self._tx_aead.encrypt(self._iv_for_seq(seq), pt, aad)
        return ble_hdr, self.channel_id + struct.pack(">Q", seq) + ct_tag

    def open_ble_payload(self, cmd_id: int, flags: int, record: bytes) -> bytes:
        """Authenticate and decrypt an incoming `flags=0x04` BLE application frame payload."""
        if self.transport_type != MD_TS_TRANSPORT_BLE:
            raise RuntimeError("open_ble_payload called on non-BLE session")
        rec = bytes(record)
        if len(rec) < MD_TS_RECORD_OVERHEAD:
            raise ValueError(f"Truncated BLE MD-TS record ({len(rec)} bytes)")
        ch_id = rec[:8]
        (seq,) = struct.unpack_from(">Q", rec, 8)
        if ch_id != self.channel_id:
            raise ValueError("BLE MD-TS channel_id mismatch")
        self._check_replay(seq)

        total_len = _BLE_FRAME_OVERHEAD + len(rec)
        ble_hdr = bytes([_BLE_SOF, total_len & 0xFF, flags & 0xFF, cmd_id & 0xFF])
        aad = build_ble_aad(self.rx_dir, self.channel_id, seq, ble_hdr)
        try:
            pt = self._rx_aead.decrypt(self._iv_for_seq(seq), rec[16:], aad)
        except InvalidTag as exc:
            raise ValueError("BLE MD-TS AES-256-GCM authentication failed") from exc
        self._commit_replay(seq)
        return pt

    def seal_zenoh_payload(
        self,
        keyexpr: str,
        plaintext: bytes,
        encoding: str = "application/protobuf",
    ) -> bytes:
        """Encrypt a Zenoh topic payload and wrap it in `MdTsZenohEnvelope` (`0x18 0x01 0x22 ...`)."""
        if self.transport_type != MD_TS_TRANSPORT_ZENOH:
            raise RuntimeError("seal_zenoh_payload called on non-Zenoh session")
        pt = bytes(plaintext)
        seq = self.tx_seq
        if seq <= 0 or seq > 0xFFFFFFFF:
            raise OverflowError("MD-TS TX sequence counter exhausted")
        self.tx_seq += 1

        aad = build_zenoh_aad(self.tx_dir, self.channel_id, seq, keyexpr, encoding=encoding)
        ct_tag = self._tx_aead.encrypt(self._iv_for_seq(seq), pt, aad)
        rec = self.channel_id + struct.pack(">Q", seq) + ct_tag
        return encode_zenoh_enc_envelope(rec)

    def open_zenoh_record(
        self,
        keyexpr: str,
        record: bytes,
        encoding: str = "application/protobuf",
    ) -> bytes:
        """Authenticate and decrypt a Zenoh `field 4` AEAD record."""
        if self.transport_type != MD_TS_TRANSPORT_ZENOH:
            raise RuntimeError("open_zenoh_record called on non-Zenoh session")
        rec = bytes(record)
        if len(rec) < MD_TS_RECORD_OVERHEAD:
            raise ValueError(f"Truncated Zenoh MD-TS record ({len(rec)} bytes)")
        ch_id = rec[:8]
        (seq,) = struct.unpack_from(">Q", rec, 8)
        if ch_id != self.channel_id:
            raise ValueError("Zenoh MD-TS channel_id mismatch")
        self._check_replay(seq)

        aad = build_zenoh_aad(self.rx_dir, self.channel_id, seq, keyexpr, encoding=encoding)
        try:
            pt = self._rx_aead.decrypt(self._iv_for_seq(seq), rec[16:], aad)
        except InvalidTag as exc:
            raise ValueError(f"Zenoh MD-TS AES-256-GCM authentication failed on {keyexpr}") from exc
        self._commit_replay(seq)
        return pt
