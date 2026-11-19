"""V0 + `MD-TS` encrypted BLE GATT client for Nebi (GATT service `0000FFF0`, served by the robot's network service).

GATT layout:
  - Primary Service UUID: `0000FFF0-0000-1000-8000-00805F9B34FB` (alt `0100FFF0-...`)
  - Write Characteristic (`char0`):  `0000FFF1-0000-1000-8000-00805F9B34FB`
  - Read Characteristic (`char1`):   `0000FFF2-0000-1000-8000-00805F9B34FB`
  - Notify Characteristic (`char2`): `0000FFF3-0000-1000-8000-00805F9B34FB`
  - Manufacturer Data Company ID: `0xFFFE` (`65534`), LocalName `Beni-<suffix>` / `BeniLite-<suffix>` (firmware-defined)

V0 BLE stream frame layout:
  - `byte[0]`: `0x55` (SOF magic)
  - `byte[1]`: `total_len` (`u8`, total frame bytes including header and CRC8; `5..255`)
  - `byte[2]`: `flags` (`0x00` = plaintext, `0x04` = `MD-TS` AEAD encrypted / legacy V0 response; bits `0xFB` must be zero)
  - `byte[3]`: `cmd_id` (`u8`)
  - `byte[4 .. total_len - 2]`: `payload` (`0..250` bytes; max inner `MD-TS` plaintext = `218` bytes)
  - `byte[total_len - 1]`: `crc8` (CRC-8/AUTOSAR: `poly=0x2F`, `init=0x99`, `xor_out=0x00`)

`cmd_id` opcodes:
  - `0x50` (`'P'`) -> `0x51` (`'Q'`): Query SoftAP Wi-Fi credentials (`[u8 ssid_len][ssid][u8 psk_len][psk]`)
  - `0x53` (`'S'`) -> `0x54` (`'T'`): Sync 2-char regulatory country code (e.g. `b"US"` -> `0x00` OK)
  - `0x55` (`'U'`): Client -> Robot `MD-TS-HS` handshake (`msg_type` `1, 4` with `flags=0x00`; `msg_type` `6` with `flags=0x04`)
  - `0x56` (`'V'`): Robot -> Client `MD-TS-HS` handshake (`msg_type` `2, 3, 5, 0x7F` with `flags=0x00`; `msg_type` `7` with `flags=0x04`)
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any, Protocol

from nebi.exceptions import NebiBleError
from nebi.md_ts import (
    DEFAULT_IDENTITY_STORE,
    MD_TS_HS_ERROR,
    MD_TS_HS_PENDING_BUTTON,
    MD_TS_HS_SERVER_HELLO,
    MD_TS_HS_SESSION_CHALLENGE,
    MD_TS_ROLE_CLIENT,
    MD_TS_TRANSPORT_BLE,
    MdTsClientIdentity,
    MdTsHandshakeMessage,
    MdTsIdentityStore,
    MdTsSession,
    _random_nonzero_bytes,
    build_hs_client_hello,
    build_hs_session_start,
    parse_hs_message,
)
from nebi.types import SOFTAP_HOST

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

BLE_SECURITY_MODES = ("auto", "md-ts", "plaintext")
SOFTAP_IP = SOFTAP_HOST  # backwards-compatible name


def __getattr__(name: str) -> Any:
    # `MockBlePeripheral` is simulator infrastructure (nebi.sim.ble), resolved lazily.
    if name == "MockBlePeripheral":
        from nebi.sim.ble import MockBlePeripheral

        return MockBlePeripheral
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


async def _call_maybe_async(cb: Callable[[], Any] | None) -> None:
    if cb is None:
        return
    res = cb()
    if isinstance(res, Awaitable):
        await res

NEBI_BLE_SERVICE_UUID = "0000fff0-0000-1000-8000-00805f9b34fb"
NEBI_BLE_ALT_SERVICE_UUID = "0100fff0-0000-1000-8000-00805f9b34fb"
NEBI_BLE_CHAR_WRITE_UUID = "0000fff1-0000-1000-8000-00805f9b34fb"
NEBI_BLE_CHAR_READ_UUID = "0000fff2-0000-1000-8000-00805f9b34fb"
NEBI_BLE_CHAR_NOTIFY_UUID = "0000fff3-0000-1000-8000-00805f9b34fb"
NEBI_BLE_COMPANY_ID = 0xFFFE
# Device identifiers set by the robot firmware (it advertises LocalName "Beni-<suffix>" / "BeniLite-<suffix>").
# They must match the hardware exactly, so they keep the vendor's spelling.
ROBOT_BLE_NAME_PREFIXES = ("Beni-", "BeniLite-")
ROBOT_BLE_NAME_STEM = "beni"

BLE_SOF = 0x55
BLE_FLAG_REQUEST = 0x00
BLE_FLAG_RESPONSE = 0x04
BLE_FLAG_MD_TS = 0x04

BLE_CMD_WIFI_QUERY = 0x50
BLE_CMD_WIFI_INFO = 0x51
BLE_CMD_SET_COUNTRY = 0x53
BLE_CMD_SET_COUNTRY_ACK = 0x54
BLE_CMD_MD_TS_CLIENT = 0x55
BLE_CMD_MD_TS_SERVER = 0x56
# Backward-compatible aliases
BLE_CMD_MD_TS_HS = BLE_CMD_MD_TS_CLIENT
BLE_CMD_MD_TS_DATA = BLE_CMD_MD_TS_SERVER


def crc8_autosar(data: bytes | bytearray | memoryview, init: int = 0x99) -> int:
    """Compute CRC-8/AUTOSAR (`poly=0x2F`, `init=0x99`, `xor_out=0x00`)."""
    crc = init & 0xFF
    for b in data:
        crc ^= b & 0xFF
        for _ in range(8):
            crc = ((crc << 1) ^ 0x2F) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


@dataclass(frozen=True, slots=True)
class BleFrame:
    """Decoded V0 BLE frame from the robot's network service."""

    cmd_id: int
    payload: bytes = b""
    flags: int = BLE_FLAG_REQUEST

    def encode(self) -> bytes:
        return encode_ble_frame(self.cmd_id, self.payload, self.flags)


def encode_ble_frame(cmd_id: int, payload: bytes = b"", flags: int = BLE_FLAG_REQUEST) -> bytes:
    """Pack a `0x55` V0 BLE frame with trailing CRC-8/AUTOSAR."""
    if flags & 0xFB:
        raise ValueError(f"Invalid V0 BLE flags byte {hex(flags)} (only 0x00 or 0x04 permitted)")
    total_len = 5 + len(payload)
    if total_len > 255:
        raise ValueError(f"V0 BLE payload too large ({len(payload)} bytes; max 250)")
    body = bytes([BLE_SOF, total_len, flags & 0xFF, cmd_id & 0xFF]) + bytes(payload)
    return body + bytes([crc8_autosar(body)])


class BleStreamParser:
    """Streaming V0 frame reassembler (resyncs on SOF, drops frames with a bad length, flags or CRC)."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def reset(self) -> None:
        self._buf.clear()

    def feed(self, chunk: bytes) -> list[BleFrame]:
        if chunk:
            self._buf.extend(chunk)
        frames: list[BleFrame] = []

        while len(self._buf) >= 5:
            if self._buf[0] != BLE_SOF:
                sof_pos = self._buf.find(bytes([BLE_SOF]), 1)
                if sof_pos < 0:
                    self._buf.clear()
                    break
                del self._buf[:sof_pos]
                continue

            total_len = self._buf[1]
            flags = self._buf[2]
            if total_len < 5 or (flags & 0xFB) != 0:
                # Corrupt header; advance past false SOF and rescan
                del self._buf[:1]
                continue

            if len(self._buf) < total_len:
                # Wait for remaining ATT notification chunks
                break

            candidate = bytes(self._buf[:total_len])
            expected_crc = crc8_autosar(candidate[:-1])
            if candidate[-1] != expected_crc:
                del self._buf[:1]
                continue

            del self._buf[:total_len]
            frames.append(
                BleFrame(
                    cmd_id=candidate[3],
                    payload=candidate[4:-1],
                    flags=flags,
                )
            )

        return frames


def decode_wifi_info_payload(payload: bytes) -> tuple[str, str]:
    """Decode the `0x51` response `[u8 ssid_len][ssid][u8 psk_len][psk]`."""
    if len(payload) < 2:
        raise NebiBleError(
            "BLE_PROTOCOL_ERROR",
            f"Truncated 0x51 Wi-Fi credentials payload ({len(payload)} bytes)",
            stage="ble_fetching_wifi_info",
            recoverable=True,
        )
    ssid_len = payload[0]
    if 1 + ssid_len + 1 > len(payload):
        raise NebiBleError(
            "BLE_PROTOCOL_ERROR",
            "Malformed SSID length in 0x51 Wi-Fi credentials payload",
            stage="ble_fetching_wifi_info",
            recoverable=True,
        )
    ssid = payload[1 : 1 + ssid_len].decode("utf-8", errors="replace")
    psk_off = 1 + ssid_len
    psk_len = payload[psk_off]
    if psk_off + 1 + psk_len > len(payload):
        raise NebiBleError(
            "BLE_PROTOCOL_ERROR",
            "Malformed PSK length in 0x51 Wi-Fi credentials payload",
            stage="ble_fetching_wifi_info",
            recoverable=True,
        )
    psk = payload[psk_off + 1 : psk_off + 1 + psk_len].decode("utf-8", errors="replace")
    return ssid, psk


def encode_wifi_info_payload(ssid: str, password: str) -> bytes:
    sb = ssid.encode("utf-8")
    pb = password.encode("utf-8")
    if 2 + len(sb) + len(pb) > 218:
        raise ValueError("SSID + PSK exceeds the robot's 218-byte limit")
    return bytes([len(sb)]) + sb + bytes([len(pb)]) + pb


@dataclass
class BleRobotAdvertisement:
    """Parsed Nebi BLE advertisement (`0000FFF0` + Company ID `0xFFFE`)."""

    address: str
    name: str
    rssi: int = -55
    manufacturer_hex: str = ""
    pairing_mode: bool = False
    is_simulated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SoftApCredentials:
    """SoftAP credentials retrieved via BLE `0x50` -> `0x51`."""

    ssid: str
    password: str
    ap_ip: str = SOFTAP_HOST
    country_code: str = "US"
    device_name: str = ""
    ble_address: str = ""
    md_ts_established: bool = False
    robot_public_key_hex: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def is_nebi_advertisement(
    local_name: str | None,
    service_uuids: list[str] | tuple[str, ...],
    manufacturer_data: dict[int, bytes] | None = None,
) -> bool:
    name = (local_name or "").strip()
    uuids_lower = {u.lower() for u in service_uuids}
    if NEBI_BLE_SERVICE_UUID in uuids_lower or NEBI_BLE_ALT_SERVICE_UUID in uuids_lower:
        return True
    if name.startswith(ROBOT_BLE_NAME_PREFIXES):
        return True
    return bool(manufacturer_data and NEBI_BLE_COMPANY_ID in manufacturer_data and name.lower().startswith(ROBOT_BLE_NAME_STEM))


class BlePeripheralLink(Protocol):
    """An in-process GATT peripheral that `NebiBleClient` can talk to instead of a real radio (the simulator's
    `nebi.sim.ble.MockBlePeripheral` implements it)."""

    address: str
    name: str
    ap_ip: str
    advertising: bool

    def advertisement(self) -> BleRobotAdvertisement: ...

    def subscribe_notify(self, cb: Callable[[bytes], None]) -> Callable[[], None]: ...

    def write_char0(self, data: bytes) -> None: ...


class NebiBleClient:
    """Async BLE GATT client for Nebi discovery, `MD-TS` X25519 button pairing, and SoftAP credential bootstrap."""

    def __init__(
        self,
        mock_peripheral: BlePeripheralLink | None = None,
        *,
        identity_store: MdTsIdentityStore | None = None,
        security_mode: str = "auto",  # "auto" | "md-ts" | "plaintext"
        allow_robot_key_change: bool = False,
    ) -> None:
        """`allow_robot_key_change=True` is the explicit re-pair switch: accept (and store) a robot static key
        that differs from the one previously recorded for this BLE address / device name. Without it a changed
        key raises `NebiBleError("BLE_ROBOT_KEY_CHANGED")` (trust-on-first-use check)."""
        if security_mode not in BLE_SECURITY_MODES:
            raise ValueError(f"Invalid BLE security_mode {security_mode!r}")
        self._mock = mock_peripheral
        self.security_mode = security_mode
        self.allow_robot_key_change = allow_robot_key_change
        self.identity_store = identity_store or (
            MdTsIdentityStore(in_memory=True) if mock_peripheral is not None else DEFAULT_IDENTITY_STORE
        )
        self._bleak_client: Any = None
        self._parser = BleStreamParser()
        self._rx_queue: asyncio.Queue[BleFrame] = asyncio.Queue()
        self._unsub_mock: Callable[[], None] | None = None
        self._md_ts_session: MdTsSession | None = None
        self.robot_public_key: bytes = b""
        self.connected_address: str = ""
        self.connected_name: str = ""
        self.rx_bytes_total: int = 0

    @property
    def _using_mock(self) -> bool:
        """True while connected to the in-process peripheral instead of a real radio."""
        return self._mock is not None and self._unsub_mock is not None

    @property
    def is_connected(self) -> bool:
        if self._using_mock:
            return True
        return bool(self._bleak_client is not None and getattr(self._bleak_client, "is_connected", False))

    @property
    def md_ts_active(self) -> bool:
        return self._md_ts_session is not None and self._md_ts_session.handshake_confirmed

    @classmethod
    async def scan(
        cls,
        *,
        timeout: float = 2.5,
        mock_peripheral: BlePeripheralLink | None = None,
    ) -> list[BleRobotAdvertisement]:
        """Scan for nearby Nebi BLE advertisements (`0000FFF0` / `0xFFFE`) plus optional simulator peripheral."""
        found: list[BleRobotAdvertisement] = []
        if mock_peripheral is not None and mock_peripheral.advertising:
            found.append(mock_peripheral.advertisement())

        try:
            from bleak import BleakScanner  # optional dependency, imported lazily
        except ImportError:
            return found

        try:
            discovered = await BleakScanner.discover(timeout=timeout, return_adv=True)
            for _addr, (dev, adv) in discovered.items():
                mfg = getattr(adv, "manufacturer_data", {}) or {}
                uuids = getattr(adv, "service_uuids", []) or []
                lname = getattr(adv, "local_name", None) or getattr(dev, "name", None)
                if is_nebi_advertisement(lname, uuids, mfg):
                    mfg_bytes = mfg.get(NEBI_BLE_COMPANY_ID, b"")
                    found.append(
                        BleRobotAdvertisement(
                            address=str(dev.address),
                            name=str(lname or "Nebi"),
                            rssi=int(getattr(adv, "rssi", -60) or -60),
                            manufacturer_hex=bytes(mfg_bytes).hex(),
                            pairing_mode=bool(len(mfg_bytes) >= 3 and mfg_bytes[2] == 1),
                            is_simulated=False,
                        )
                    )
        except Exception as exc:
            logger.debug("BLE scan unavailable or skipped: %s", exc)

        return found

    def _on_notify_bytes(self, data: bytes | bytearray) -> None:
        self.rx_bytes_total += len(data)
        for frame in self._parser.feed(bytes(data)):
            self._rx_queue.put_nowait(frame)

    async def connect(self, address: str | None = None, *, timeout: float = 6.0) -> None:
        self._parser.reset()
        self._md_ts_session = None
        self.robot_public_key = b""
        self.rx_bytes_total = 0
        while not self._rx_queue.empty():
            self._rx_queue.get_nowait()

        if self._mock is not None and (address is None or address == self._mock.address):
            self._unsub_mock = self._mock.subscribe_notify(self._on_notify_bytes)
            self.connected_address = self._mock.address
            self.connected_name = self._mock.name
            return

        try:
            from bleak import BleakClient  # optional dependency, imported lazily
        except ImportError as exc:
            raise NebiBleError(
                "BLE_ADAPTER_UNAVAILABLE",
                "bleak is not installed; cannot open BLE GATT connection.",
                stage="ble_connecting",
                recoverable=False,
                hint="Run `pip install bleak` inside the nebi virtual environment.",
            ) from exc

        target = address
        if not target:
            ads = await self.scan(timeout=min(3.0, timeout))
            real_ads = [a for a in ads if not a.is_simulated]
            if not real_ads:
                raise NebiBleError(
                    "BLE_DEVICE_NOT_FOUND",
                    "No Nebi robot BLE advertisement (`0000FFF0`) found nearby.",
                    stage="discovering",
                    recoverable=True,
                    hint="Ensure Nebi is powered on, disconnect the mobile app if connected over SoftAP (:9007), and check Bluetooth.",
                )
            best = self._pick_advertisement(real_ads)
            target = best.address
            self.connected_name = best.name

        client = None
        try:
            client = BleakClient(target, timeout=timeout)
            await client.connect()
            await client.start_notify(NEBI_BLE_CHAR_NOTIFY_UUID, lambda _c, b: self._on_notify_bytes(bytes(b)))
            self._bleak_client = client
            self.connected_address = str(target)
        except Exception as exc:
            if client is not None:
                # Connected but notify setup failed: don't leave a dangling GATT link holding the robot.
                try:
                    await client.disconnect()
                except Exception as disc_exc:
                    logger.debug("BLE disconnect after failed setup also failed: %r", disc_exc)
            raise NebiBleError(
                "BLE_CONNECT_FAILED",
                f"Failed to connect to Nebi BLE GATT service ({target}): {exc}",
                stage="ble_connecting",
                recoverable=True,
                hint="Move closer to Nebi, check macOS/Linux Bluetooth permissions, and ensure no phone app holds an active UCP link.",
            ) from exc

    def _is_known_robot(self, ad: BleRobotAdvertisement) -> bool:
        try:
            return (
                self.identity_store.lookup_robot_public_key(
                    ble_address=ad.address, device_name=ad.name, allow_default=False
                )
                is not None
            )
        except Exception as exc:  # a damaged identity store must not break robot selection
            logger.debug("Identity lookup failed while ranking BLE advertisements: %r", exc)
            return False

    def _pick_advertisement(self, ads: list[BleRobotAdvertisement]) -> BleRobotAdvertisement:
        """Prefer robots already paired with this identity, then the strongest signal."""
        ranked = sorted(ads, key=lambda a: (self._is_known_robot(a), a.rssi), reverse=True)
        if len(ranked) > 1:
            logger.info(
                "Multiple Nebi robots advertising; choosing %s (%s, rssi %d, known=%s)",
                ranked[0].name,
                ranked[0].address,
                ranked[0].rssi,
                self._is_known_robot(ranked[0]),
            )
        return ranked[0]

    def _drain_rx(self) -> int:
        """Drop queued notifications (stale duplicates produced by our own idempotent retries)."""
        dropped = 0
        while not self._rx_queue.empty():
            self._rx_queue.get_nowait()
            dropped += 1
        return dropped

    @staticmethod
    def _parse_hs(payload: bytes, *, stage: str) -> MdTsHandshakeMessage:
        try:
            return parse_hs_message(payload)
        except ValueError as exc:
            raise NebiBleError(
                "BLE_PROTOCOL_ERROR",
                f"Malformed BLE MD-TS handshake message: {exc}",
                stage=stage,
                recoverable=True,
            ) from exc

    def _check_robot_key(self, robot_pub: bytes) -> None:
        """Trust-on-first-use: refuse a robot key that differs from the stored one unless re-pairing."""
        if not (self.connected_address or self.connected_name):
            return
        stored = self.identity_store.lookup_robot_public_key(
            ble_address=self.connected_address,
            device_name=self.connected_name,
            allow_default=False,
        )
        if stored is None or stored == robot_pub:
            return
        if self.allow_robot_key_change:
            logger.warning(
                "Robot %s (%s) presented a new static key; replacing the stored key (re-pair requested).",
                self.connected_name,
                self.connected_address,
            )
            return
        raise NebiBleError(
            "BLE_ROBOT_KEY_CHANGED",
            f"Robot {self.connected_name or self.connected_address} presented a different static public key "
            "than the one recorded when it was paired.",
            stage="ble_connecting",
            recoverable=False,
            hint=(
                "This can mean the robot was factory-reset/replaced, or another device is impersonating it. "
                "If you trust it, re-pair explicitly (NebiBleClient(allow_robot_key_change=True))."
            ),
            details={
                "ble_address": self.connected_address,
                "device_name": self.connected_name,
                "stored_public_key_hex": stored.hex(),
                "presented_public_key_hex": robot_pub.hex(),
            },
        )

    async def close(self) -> None:
        self._md_ts_session = None
        if self._unsub_mock is not None:
            self._unsub_mock()
            self._unsub_mock = None
        if self._bleak_client is not None:
            try:
                await self._bleak_client.stop_notify(NEBI_BLE_CHAR_NOTIFY_UUID)
            except Exception as exc:  # link may already be gone; still try to disconnect
                logger.debug("BLE stop_notify failed: %r", exc)
            try:
                await self._bleak_client.disconnect()
            except Exception as exc:
                logger.debug("BLE disconnect failed: %r", exc)
            self._bleak_client = None

    async def __aenter__(self) -> NebiBleClient:
        await self.connect()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.close()

    async def _write_raw_frame(self, cmd_id: int, payload: bytes = b"", *, flags: int = BLE_FLAG_REQUEST) -> None:
        raw = encode_ble_frame(cmd_id, payload, flags=flags)
        if self._mock is not None and self._using_mock:
            self._mock.write_char0(raw)
            return
        if self._bleak_client is None:
            raise NebiBleError("BLE_NOT_CONNECTED", "BLE GATT client is not connected.", stage="ble_connecting")
        await self._bleak_client.write_gatt_char(NEBI_BLE_CHAR_WRITE_UUID, raw, response=False)

    async def _write_frame(self, cmd_id: int, payload: bytes = b"") -> None:
        if self._md_ts_session is not None:
            _, rec = self._md_ts_session.seal_ble_payload(cmd_id, payload)
            await self._write_raw_frame(cmd_id, rec, flags=BLE_FLAG_MD_TS)
        else:
            await self._write_raw_frame(cmd_id, payload, flags=BLE_FLAG_REQUEST)

    async def _wait_for_raw_cmd(
        self,
        expected_cmd: int,
        timeout: float,
        *,
        expected_flags: int | None = None,
    ) -> BleFrame:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            rem = deadline - asyncio.get_running_loop().time()
            if rem <= 0:
                raise asyncio.TimeoutError()
            frame = await asyncio.wait_for(self._rx_queue.get(), timeout=rem)
            if expected_cmd not in (BLE_CMD_MD_TS_CLIENT, BLE_CMD_MD_TS_SERVER) and frame.cmd_id in (
                BLE_CMD_MD_TS_CLIENT,
                BLE_CMD_MD_TS_SERVER,
            ):
                raise NebiBleError(
                    "BLE_SECURITY_HANDSHAKE_REQUIRED",
                    "Robot requested encrypted MD-TS BLE handshake before accepting commands.",
                    stage="ble_fetching_wifi_info",
                    recoverable=True,
                    hint="Use security_mode='auto' or 'md-ts' on NebiBleClient to complete the MD-TS X25519 handshake.",
                )
            if frame.cmd_id == expected_cmd and (expected_flags is None or frame.flags == expected_flags):
                return frame

    async def _wait_for_cmd(self, expected_cmd: int, timeout: float) -> BleFrame:
        if self._md_ts_session is None:
            return await self._wait_for_raw_cmd(expected_cmd, timeout)
        frame = await self._wait_for_raw_cmd(expected_cmd, timeout, expected_flags=BLE_FLAG_MD_TS)
        try:
            pt = self._md_ts_session.open_ble_payload(frame.cmd_id, frame.flags, frame.payload)
        except ValueError as exc:
            raise NebiBleError(
                "BLE_DECRYPT_FAILED",
                f"Failed to authenticate/decrypt BLE MD-TS response 0x{expected_cmd:02X}: {exc}",
                stage="ble_fetching_wifi_info",
                recoverable=True,
            ) from exc
        return BleFrame(cmd_id=frame.cmd_id, payload=pt, flags=frame.flags)

    async def perform_md_ts_handshake(
        self,
        *,
        wait_for_button_sec: float = 30.0,
        on_awaiting_button: Callable[[], Any] | None = None,
        initial_probe_timeout: float = 1.2,
    ) -> bool:
        """Execute `MD-TS-HS` over BLE (`0x55` -> `0x56`, `msg_type = 1 -> [3 -> button] -> 2 -> 4 -> 5 -> 6 -> 7`).

        Returns `True` if an encrypted `MD-TS` session is established, or `False` in `"auto"` mode
        if the peripheral ignores `0x55` (allowing fallback to legacy V0 plaintext).
        """
        if self.security_mode == "plaintext":
            return False
        if self.md_ts_active:
            return True

        ident = self.identity_store.identity
        hello_bytes = build_hs_client_hello(ident.client_id, ident.key_version, ident.public_key_bytes)
        await self._write_raw_frame(BLE_CMD_MD_TS_CLIENT, hello_bytes, flags=BLE_FLAG_REQUEST)

        probe_timeout = 0.25 if self._using_mock else initial_probe_timeout
        try:
            rsp1 = await self._wait_for_raw_cmd(BLE_CMD_MD_TS_SERVER, timeout=probe_timeout, expected_flags=BLE_FLAG_REQUEST)
        except asyncio.TimeoutError:
            if self.security_mode == "auto":
                return False
            raise NebiBleError(
                "BLE_HANDSHAKE_TIMEOUT",
                "Timed out waiting for BLE MD-TS SERVER_HELLO (0x56).",
                stage="ble_connecting",
                recoverable=True,
                hint="Move closer to Nebi, ensure no phone app holds an active connection, and retry.",
            ) from None

        hs_srv_hello = await self._resolve_server_hello(
            self._parse_hs(rsp1.payload, stage="ble_connecting"),
            hello_bytes,
            wait_for_button_sec=wait_for_button_sec,
            on_awaiting_button=on_awaiting_button,
        )

        robot_pub = hs_srv_hello.public_key
        self._check_robot_key(robot_pub)
        self.robot_public_key = robot_pub
        self.identity_store.save_robot_public_key(
            robot_pub,
            ble_address=self.connected_address,
            device_name=self.connected_name,
        )

        client_nonce = _random_nonzero_bytes(32)
        hs5 = await self._session_start(ident, client_nonce)
        session = MdTsSession.from_handshake(
            role=MD_TS_ROLE_CLIENT,
            transport_type=MD_TS_TRANSPORT_BLE,
            local_private_key_bytes=ident.private_key_bytes,
            peer_public_key_bytes=robot_pub,
            client_id=ident.client_id,
            key_version=ident.key_version,
            client_static_pub=ident.public_key_bytes,
            robot_static_pub=robot_pub,
            client_nonce=client_nonce,
            server_nonce=hs5.nonce,
            channel_id=hs5.channel_id,
        )
        await self._client_confirm(session, ident)
        self._md_ts_session = session
        return True

    async def _resolve_server_hello(
        self,
        hs1: MdTsHandshakeMessage,
        hello_bytes: bytes,
        *,
        wait_for_button_sec: float,
        on_awaiting_button: Callable[[], Any] | None,
    ) -> MdTsHandshakeMessage:
        """Turn the answer to CLIENT_HELLO into a SERVER_HELLO, waiting for the button press if required."""
        if hs1.msg_type == MD_TS_HS_SERVER_HELLO:
            return hs1
        if hs1.msg_type == MD_TS_HS_ERROR:
            raise NebiBleError(
                "BLE_HANDSHAKE_REJECTED",
                f"Robot rejected BLE MD-TS client hello ({hs1.error_name}).",
                stage="ble_connecting",
                recoverable=True,
                hint="If identity keys changed, hold Nebi's button to re-authorize or check ~/.config/pynebi/identity.json.",
                details={"error_code": hs1.error_code, "error_name": hs1.error_name},
            )
        if hs1.msg_type != MD_TS_HS_PENDING_BUTTON:
            raise NebiBleError(
                "BLE_PROTOCOL_ERROR",
                f"Unexpected MD-TS msg_type={hs1.msg_type} after CLIENT_HELLO.",
                stage="ble_connecting",
                recoverable=True,
            )
        if hs1.pending_status == 0x03:
            raise NebiBleError(
                "BLE_AUTH_BUSY",
                "Nebi is currently awaiting physical button authorization for another BLE client.",
                stage="ble_awaiting_button_auth",
                recoverable=True,
                hint="Close the official mobile app on other nearby devices, wait a few seconds, and retry.",
            )

        await _call_maybe_async(on_awaiting_button)
        if wait_for_button_sec <= 0:
            raise NebiBleError(
                "BLE_AUTH_REQUIRED",
                "BLE client identity is awaiting the 3-5 second physical button hold on Nebi.",
                stage="ble_awaiting_button_auth",
                recoverable=True,
                hint="Hold Nebi's physical button for 3-5 seconds until it chimes, then retry.",
            )
        return await self._await_button_server_hello(hello_bytes, wait_for_button_sec)

    async def _await_button_server_hello(self, hello_bytes: bytes, wait_for_button_sec: float) -> MdTsHandshakeMessage:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait_for_button_sec
        while loop.time() < deadline:
            rem = deadline - loop.time()
            if rem <= 0:
                break
            try:
                # The robot pushes an unsolicited msg_type=2 as soon as the button is held; we also re-send the
                # identical msg_type=1 every 1.0 s as an idempotent fallback.
                nxt = await self._wait_for_raw_cmd(BLE_CMD_MD_TS_SERVER, timeout=min(1.0, rem), expected_flags=BLE_FLAG_REQUEST)
            except asyncio.TimeoutError:
                if loop.time() < deadline:
                    await self._write_raw_frame(BLE_CMD_MD_TS_CLIENT, hello_bytes, flags=BLE_FLAG_REQUEST)
                continue
            try:
                parsed = parse_hs_message(nxt.payload)
            except ValueError as exc:
                logger.debug("Skipping malformed BLE handshake frame while awaiting button: %s", exc)
                continue
            if parsed.msg_type == MD_TS_HS_SERVER_HELLO:
                return parsed
            if parsed.msg_type == MD_TS_HS_ERROR:
                raise NebiBleError(
                    "BLE_HANDSHAKE_REJECTED",
                    f"Robot aborted button authorization ({parsed.error_name}).",
                    stage="ble_awaiting_button_auth",
                    recoverable=True,
                )
        raise NebiBleError(
            "BLE_AUTH_REQUIRED",
            "Timed out waiting for the 3-5 second physical button press on Nebi.",
            stage="ble_awaiting_button_auth",
            recoverable=True,
            hint="Hold Nebi's physical button for 3-5 seconds until the pairing chime plays (or click 'Simulate 5s Button' in Simulator mode).",
        )

    async def _session_start(self, ident: MdTsClientIdentity, client_nonce: bytes) -> MdTsHandshakeMessage:
        """SESSION_START (msg_type=4) -> SESSION_CHALLENGE (msg_type=5), up to 3 sends of 1.5 s each."""
        hs4_bytes = build_hs_session_start(ident.client_id, ident.key_version, client_nonce)
        loop = asyncio.get_running_loop()
        # Duplicate msg_type=2 frames (answers to our idempotent hello retries) may still be queued.
        self._drain_rx()
        for _ in range(3):
            await self._write_raw_frame(BLE_CMD_MD_TS_CLIENT, hs4_bytes, flags=BLE_FLAG_REQUEST)
            attempt_deadline = loop.time() + 1.5
            try:
                while True:
                    rem5 = attempt_deadline - loop.time()
                    if rem5 <= 0:
                        raise asyncio.TimeoutError()
                    rsp5 = await self._wait_for_raw_cmd(BLE_CMD_MD_TS_SERVER, timeout=rem5, expected_flags=BLE_FLAG_REQUEST)
                    try:
                        cand5 = parse_hs_message(rsp5.payload)
                    except ValueError as exc:
                        logger.debug("Skipping malformed BLE handshake frame while awaiting msg_type=5: %s", exc)
                        continue
                    if cand5.msg_type == MD_TS_HS_ERROR:
                        raise NebiBleError(
                            "BLE_HANDSHAKE_REJECTED",
                            f"Robot rejected BLE SESSION_START ({cand5.error_name}).",
                            stage="ble_connecting",
                            recoverable=True,
                        )
                    if cand5.msg_type == MD_TS_HS_SESSION_CHALLENGE:
                        return cand5
                    # anything else (stale msg_type=2/3) is ignored without consuming a resend
            except asyncio.TimeoutError:
                continue
        raise NebiBleError(
            "BLE_HANDSHAKE_TIMEOUT",
            "Timed out waiting for BLE MD-TS SESSION_CHALLENGE (msg_type=5).",
            stage="ble_connecting",
            recoverable=True,
        )

    async def _client_confirm(self, session: MdTsSession, ident: MdTsClientIdentity) -> None:
        """Encrypted CLIENT_CONFIRM (msg_type=6, seq=0, flags=0x04) -> verified SERVER_CONFIRM (msg_type=7)."""
        confirm_rec = session.seal_handshake_confirm(
            client_id=ident.client_id,
            key_version=ident.key_version,
            ble_cmd_id=BLE_CMD_MD_TS_CLIENT,
        )
        # Stale duplicate msg_type=5 / 2 frames (plaintext, flags=0) must not be mistaken for a rejection.
        self._drain_rx()
        await self._write_raw_frame(BLE_CMD_MD_TS_CLIENT, confirm_rec, flags=BLE_FLAG_MD_TS)

        loop = asyncio.get_running_loop()
        deadline7 = loop.time() + 2.0
        while True:
            rem7 = deadline7 - loop.time()
            try:
                if rem7 <= 0:
                    raise asyncio.TimeoutError()
                rsp7 = await self._wait_for_raw_cmd(BLE_CMD_MD_TS_SERVER, timeout=rem7)
            except asyncio.TimeoutError as exc:
                raise NebiBleError(
                    "BLE_HANDSHAKE_TIMEOUT",
                    "Timed out waiting for BLE MD-TS SERVER_CONFIRM_ACK (msg_type=7).",
                    stage="ble_connecting",
                    recoverable=True,
                ) from exc
            if rsp7.flags != BLE_FLAG_REQUEST:
                break
            try:
                plain7 = parse_hs_message(rsp7.payload)
            except ValueError as exc:
                logger.debug("Skipping malformed plaintext BLE frame while awaiting msg_type=7: %s", exc)
                continue
            if plain7.msg_type == MD_TS_HS_ERROR:
                raise NebiBleError(
                    "BLE_HANDSHAKE_REJECTED",
                    f"Robot rejected BLE CLIENT_CONFIRM ({plain7.error_name}).",
                    stage="ble_connecting",
                    recoverable=True,
                    details={"error_code": plain7.error_code, "error_name": plain7.error_name},
                )
            logger.debug("Ignoring stale BLE handshake frame msg_type=%d while awaiting msg_type=7", plain7.msg_type)

        ble_hdr7 = bytes([BLE_SOF, 5 + len(rsp7.payload), rsp7.flags, BLE_CMD_MD_TS_SERVER])
        try:
            session.open_handshake_confirm(
                rsp7.payload,
                expected_client_id=ident.client_id,
                expected_key_version=ident.key_version,
                ble_hdr=ble_hdr7,
            )
        except ValueError as exc:
            raise NebiBleError(
                "BLE_HANDSHAKE_REJECTED",
                f"BLE SERVER_CONFIRM_ACK verification failed: {exc}",
                stage="ble_connecting",
                recoverable=True,
            ) from exc

    async def sync_country_code(self, country_code: str = "US", timeout: float = 1.5) -> bool:
        """Send `0x53` regulatory country code and await `0x54` status (`0x00` == OK)."""
        cc = (country_code or "US").strip().upper()[:2].encode("ascii", errors="replace")
        if len(cc) != 2:
            cc = b"US"
        await self._write_frame(BLE_CMD_SET_COUNTRY, cc)
        try:
            rsp = await self._wait_for_cmd(BLE_CMD_SET_COUNTRY_ACK, timeout=timeout)
            return len(rsp.payload) >= 1 and rsp.payload[0] == 0x00
        except asyncio.TimeoutError:
            return False

    async def query_wifi_credentials_once(self, timeout: float = 0.8) -> SoftApCredentials:
        """Send a single `0x50` SoftAP query and await `0x51`."""
        await self._write_frame(BLE_CMD_WIFI_QUERY, b"")
        rsp = await self._wait_for_cmd(BLE_CMD_WIFI_INFO, timeout=timeout)
        ssid, psk = decode_wifi_info_payload(rsp.payload)
        ap_ip = self._mock.ap_ip if (self._mock is not None and self._using_mock) else SOFTAP_IP
        if self.robot_public_key:
            self.identity_store.save_robot_public_key(
                self.robot_public_key,
                ble_address=self.connected_address,
                ssid=ssid,
                device_name=self.connected_name or ssid,
                host=ap_ip,
            )
        return SoftApCredentials(
            ssid=ssid,
            password=psk,
            ap_ip=ap_ip,
            device_name=self.connected_name or ssid,
            ble_address=self.connected_address,
            md_ts_established=self.md_ts_active,
            robot_public_key_hex=self.robot_public_key.hex(),
        )

    async def bootstrap_wifi_credentials(
        self,
        *,
        country_code: str = "US",
        wait_for_button_sec: float = 20.0,
        poll_interval_sec: float = 0.45,
        on_awaiting_button: Callable[[], Any] | None = None,
    ) -> SoftApCredentials:
        """Establish `MD-TS` encrypted BLE session (with 3-5s button pairing if needed), sync country code (`0x53`),
        and fetch SoftAP credentials (`0x50` -> `0x51`).
        """
        if self.security_mode in ("auto", "md-ts") and not self.md_ts_active:
            await self.perform_md_ts_handshake(
                wait_for_button_sec=wait_for_button_sec,
                on_awaiting_button=on_awaiting_button,
            )

        await self.sync_country_code(country_code, timeout=3.5)

        # Retry 0x50 -> 0x51 for up to ~6s when MD-TS is active (on real AIC8800 hardware, 0x53 country sync
        # may briefly restart hostapd before 0x50 replies with 0x51).
        query_attempts = 5 if self.md_ts_active else 1
        for attempt in range(query_attempts):
            try:
                creds = await self.query_wifi_credentials_once(timeout=1.5 if self.md_ts_active else 0.8)
                creds.country_code = country_code
                return creds
            except asyncio.TimeoutError:
                if attempt + 1 < query_attempts:
                    await asyncio.sleep(0.35)

        # Legacy V0 plaintext button-gated fallback (when peripheral has md_ts_enabled=False)
        if not self.md_ts_active:
            await _call_maybe_async(on_awaiting_button)

            if wait_for_button_sec <= 0:
                raise NebiBleError(
                    "BLE_AUTH_REQUIRED",
                    "BLE client is not yet authorized on Nebi (`0x50` withheld).",
                    stage="ble_awaiting_button_auth",
                    recoverable=True,
                    hint="Hold Nebi's physical button for 3-5 seconds until the pairing chime plays, then retry.",
                )

            deadline = asyncio.get_running_loop().time() + wait_for_button_sec
            while asyncio.get_running_loop().time() < deadline:
                try:
                    creds = await self.query_wifi_credentials_once(timeout=min(0.6, poll_interval_sec))
                    creds.country_code = country_code
                    return creds
                except asyncio.TimeoutError:
                    await asyncio.sleep(0.15)

        if self.rx_bytes_total == 0 and self._unsub_mock is None:
            raise NebiBleError(
                "BLE_NO_RESPONSE",
                "Nebi accepted the BLE connection but never sent any GATT notifications.",
                stage="ble_fetching_wifi_info",
                recoverable=True,
                hint="Disconnect the official mobile app if it is currently connected to the robot, move closer, and retry.",
            )
        raise NebiBleError(
            "BLE_AUTH_REQUIRED",
            "Timed out waiting for SoftAP Wi-Fi credentials over BLE.",
            stage="ble_awaiting_button_auth",
            recoverable=True,
            hint="Hold Nebi's physical button for 3-5 seconds (or click 'Simulate 5s Button' in Simulator mode) and retry.",
        )
