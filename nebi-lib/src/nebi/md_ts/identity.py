"""nebi's persistent MD-TS client identity (static X25519 key) and the store of paired robots' public keys."""

from __future__ import annotations

import json
import logging
import os
import secrets
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nebi.exceptions import NebiError, NebiIdentityError
from nebi.md_ts.session import generate_x25519_keypair
from nebi.md_ts.wire import _random_nonzero_bytes, _validate_nonzero

logger = logging.getLogger(__name__)


@dataclass
class MdTsClientIdentity:
    """Persistent static X25519 client identity for `nebi` (Route A: authorized via robot button press)."""

    client_id: bytes
    key_version: int
    private_key_bytes: bytes
    public_key_bytes: bytes

    @classmethod
    def generate(cls, key_version: int = 1) -> MdTsClientIdentity:
        priv_b, pub_b = generate_x25519_keypair()
        return cls(
            client_id=_random_nonzero_bytes(16),
            key_version=key_version,
            private_key_bytes=priv_b,
            public_key_bytes=pub_b,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "client_id_hex": self.client_id.hex(),
            "key_version": self.key_version,
            "private_key_hex": self.private_key_bytes.hex(),
            "public_key_hex": self.public_key_bytes.hex(),
        }


def default_identity_path() -> Path:
    override = os.environ.get("PYNEBI_IDENTITY_PATH", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "pynebi" / "identity.json"


def write_private_file_atomic(path: Path, text: str) -> None:
    """Write `text` to `path` atomically, never exposing the content with permissions wider than `0o600`."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class MdTsIdentityStore:
    """Manages `nebi`'s persistent client X25519 identity and paired robots' static X25519 public keys."""

    def __init__(self, path: str | Path | None = None, *, in_memory: bool = False) -> None:
        self.in_memory = in_memory
        self.path: Path | None = None if in_memory else (Path(path).expanduser() if path else default_identity_path())
        self._identity: MdTsClientIdentity | None = None
        self._default_robot_pub_hex: str = ""
        self._known_robots: dict[str, dict[str, Any]] = {}
        self._loaded = False

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        if self.in_memory or self.path is None:
            self._identity = MdTsClientIdentity.generate()
            self._loaded = True
            return
        if not self.path.exists():
            # First run only: generate a fresh identity. A file that exists but cannot be parsed is never
            # replaced silently (that would orphan the robot-side authorization); see `_read_file`.
            self._identity = MdTsClientIdentity.generate()
            self._loaded = True
            self._save()
            return
        ident, default_hex, known = self._read_file()
        self._identity = ident
        self._default_robot_pub_hex = default_hex
        self._known_robots = known
        self._loaded = True

    def _read_file(self) -> tuple[MdTsClientIdentity, str, dict[str, dict[str, Any]]]:
        """Parse the identity file; on damage, back it up and raise `NebiError("IDENTITY_CORRUPT")`."""
        assert self.path is not None
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            cid = bytes.fromhex(raw["client_id_hex"])
            kv = int(raw.get("key_version", 1))
            priv_b = bytes.fromhex(raw["private_key_hex"])
            pub_b = bytes.fromhex(raw["public_key_hex"])
            ident = MdTsClientIdentity(
                client_id=_validate_nonzero(cid, 16, "client_id"),
                key_version=kv,
                private_key_bytes=_validate_nonzero(priv_b, 32, "private_key"),
                public_key_bytes=_validate_nonzero(pub_b, 32, "public_key"),
            )
            default_hex = str(raw.get("default_robot_public_key_hex", "") or "")
            known_raw = raw.get("known_robots", {})
            known = {str(k): dict(v) for k, v in known_raw.items() if isinstance(v, dict)} if isinstance(known_raw, dict) else {}
        except Exception as exc:
            backup = self.path.with_name(f"{self.path.name}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}")
            backed_up = ""
            try:
                shutil.copy2(self.path, backup)
                os.chmod(backup, 0o600)
                backed_up = str(backup)
            except OSError:
                pass
            raise NebiIdentityError(
                "IDENTITY_CORRUPT",
                f"nebi identity file {self.path} is damaged and cannot be loaded ({type(exc).__name__}).",
                stage="identity",
                recoverable=False,
                hint=(
                    "The file was left untouched"
                    + (f" (a copy was saved to {backed_up})" if backed_up else "")
                    + ". Restore it from a backup, or delete it to generate a new identity and re-pair over "
                    "Bluetooth LE (`nebi pair-ble`, holding Nebi's button for 3-5 s)."
                ),
                details={"path": str(self.path), "backup_path": backed_up},
            ) from exc
        return ident, default_hex, known

    def _save(self) -> None:
        if self.in_memory or self.path is None or self._identity is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        # Another process (cockpit server, CLI, agent daemon) may share this file: merge robot keys it saved
        # since we loaded instead of writing back a stale snapshot. Our own entries win on key conflicts only
        # when they are newer.
        if self.path.exists():
            try:
                disk_ident, disk_default, disk_known = self._read_file()
            except NebiError:
                disk_ident, disk_default, disk_known = None, "", {}
            if disk_ident is not None and disk_ident.client_id == self._identity.client_id:
                for key, rec in disk_known.items():
                    mine = self._known_robots.get(key)
                    if mine is None or float(rec.get("updated_at", 0) or 0) > float(mine.get("updated_at", 0) or 0):
                        self._known_robots[key] = rec
                if not self._default_robot_pub_hex:
                    self._default_robot_pub_hex = disk_default
        payload = {
            **self._identity.to_dict(),
            "default_robot_public_key_hex": self._default_robot_pub_hex,
            "known_robots": self._known_robots,
        }
        write_private_file_atomic(self.path, json.dumps(payload, indent=2) + "\n")

    @property
    def identity(self) -> MdTsClientIdentity:
        self._ensure_loaded()
        assert self._identity is not None
        return self._identity

    def save_robot_public_key(
        self,
        robot_public_key: bytes,
        *,
        ble_address: str = "",
        ssid: str = "",
        device_name: str = "",
        host: str = "",
    ) -> None:
        """Persist a robot's 32-byte static X25519 public key discovered during BLE `msg_type = 2`."""
        self._ensure_loaded()
        pub_b = _validate_nonzero(robot_public_key, 32, "robot_public_key")
        pub_hex = pub_b.hex()
        self._default_robot_pub_hex = pub_hex
        entry = {
            "public_key_hex": pub_hex,
            "ble_address": ble_address,
            "ssid": ssid,
            "device_name": device_name,
            "host": host,
            "updated_at": round(time.time(), 3),
        }
        if ble_address:
            self._known_robots[f"ble:{ble_address.lower()}"] = entry
        if ssid:
            self._known_robots[f"ssid:{ssid}"] = entry
        if device_name:
            self._known_robots[f"name:{device_name}"] = entry
        if host and host not in ("127.0.0.1", "localhost"):
            self._known_robots[f"host:{host}"] = entry
        self._save()

    def lookup_robot_public_key(
        self,
        *,
        ble_address: str = "",
        ssid: str = "",
        device_name: str = "",
        host: str = "",
        allow_default: bool = True,
    ) -> bytes | None:
        """Look up a previously paired robot's 32-byte static X25519 public key."""
        self._ensure_loaded()
        candidates: list[str] = []
        if ble_address:
            candidates.append(f"ble:{ble_address.lower()}")
        if ssid:
            candidates.append(f"ssid:{ssid}")
        if device_name:
            candidates.append(f"name:{device_name}")
        if host:
            candidates.append(f"host:{host}")

        for key in candidates:
            rec = self._known_robots.get(key)
            if isinstance(rec, dict) and rec.get("public_key_hex"):
                try:
                    return _validate_nonzero(bytes.fromhex(rec["public_key_hex"]), 32, "robot_public_key")
                except ValueError:
                    continue

        if allow_default and self._default_robot_pub_hex:
            try:
                return _validate_nonzero(bytes.fromhex(self._default_robot_pub_hex), 32, "default_robot_public_key")
            except ValueError:
                return None
        return None

    def summary_dict(self) -> dict[str, Any]:
        ident = self.identity
        return {
            "client_id_hex": ident.client_id.hex(),
            "key_version": ident.key_version,
            "public_key_hex": ident.public_key_bytes.hex(),
            "has_paired_robot_key": bool(self._default_robot_pub_hex),
            "default_robot_public_key_hex": self._default_robot_pub_hex,
            "known_robot_keys": sorted(self._known_robots.keys()),
            "identity_path": str(self.path) if self.path else ":memory:",
        }


DEFAULT_IDENTITY_STORE = MdTsIdentityStore()
