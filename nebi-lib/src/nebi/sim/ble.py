"""In-process simulated robot network-service BLE GATT peripheral (`0000FFF0..FFF3`) for tests and the simulator."""

from __future__ import annotations

import json
from collections.abc import Callable

from nebi.ble import (
    BLE_CMD_MD_TS_CLIENT,
    BLE_CMD_MD_TS_SERVER,
    BLE_CMD_SET_COUNTRY,
    BLE_CMD_SET_COUNTRY_ACK,
    BLE_CMD_WIFI_INFO,
    BLE_CMD_WIFI_QUERY,
    BLE_FLAG_MD_TS,
    BLE_FLAG_REQUEST,
    BLE_FLAG_RESPONSE,
    BLE_SOF,
    BleFrame,
    BleRobotAdvertisement,
    BleStreamParser,
    encode_ble_frame,
    encode_wifi_info_payload,
)
from nebi.md_ts import (
    MD_TS_ERR_UNAUTHORIZED,
    MD_TS_HS_CLIENT_HELLO,
    MD_TS_HS_SESSION_START,
    MD_TS_TRANSPORT_BLE,
    MdTsServerHandshake,
    MdTsSession,
    build_hs_error,
    build_hs_server_hello,
    parse_hs_message,
)
from nebi.sim.md_ts import MdTsServerAuthority

CONNECTION_STATUS_CONNECTING = 6  # reported after the robot hands out SoftAP credentials (0x51)


class MockBlePeripheral:
    """In-process simulated robot network-service BlueZ GATT peripheral (`0000FFF0..FFF3`).

    Supports:
      - Full `MD-TS` encrypted BLE handshake (`0x55` -> `0x56`, `msg_type` `1 -> 3 -> 2 -> 4 -> 5 -> 6 -> 7`)
        and `flags=0x04` AES-256-GCM frames, plus legacy V0 plaintext fallback when `md_ts_enabled=False`
      - Chunked ATT notifications (`att_mtu - 3` bytes), like the robot
      - 3-5 second physical button authorization gate (`require_button_auth` + `press_pairing_button()`,
        pushing an unsolicited `msg_type = 2` like the robot)
      - Country code sync (`0x53` -> `0x54`) and SoftAP SSID + WPA2 PSK query (`0x50` -> `0x51`)
    """

    def __init__(
        self,
        address: str = "SIM-BLE-LZ110-01",
        name: str = "Nebi-SIM001",
        ssid: str = "Nebi-SIM001",
        password: str = "nebi2026sim",
        ap_ip: str = "127.0.0.1",
        *,
        require_button_auth: bool = False,
        authorized: bool = True,
        encrypted_handshake_required: bool = False,
        md_ts_enabled: bool = True,
        md_ts_authority: MdTsServerAuthority | None = None,
        att_mtu: int = 23,
    ) -> None:
        self.address = address
        self.name = name
        self.ssid = ssid
        self.password = password
        self.ap_ip = ap_ip
        self.require_button_auth = require_button_auth
        self.authorized = authorized if not require_button_auth else False
        self.encrypted_handshake_required = encrypted_handshake_required
        self.md_ts_enabled = md_ts_enabled or encrypted_handshake_required
        self.md_ts_authority = md_ts_authority or MdTsServerAuthority(
            require_button_auth=require_button_auth,
            auto_authorize_unknown=not require_button_auth,
        )
        self.country_code = "US"
        self.att_mtu = max(23, att_mtu)
        self.advertising = True
        self.connection_status = 0  # CONNECTION_STATUS_CONNECTING after 0x51

        self._rx_parser = BleStreamParser()
        self._notify_callbacks: list[Callable[[bytes], None]] = []

        # Per-session MD-TS server state
        self._handshake = MdTsServerHandshake(MD_TS_TRANSPORT_BLE)
        self._active_session: MdTsSession | None = None

    def press_pairing_button(self) -> None:
        """Simulate holding Nebi's physical button for 3-5 seconds to authorize the staged BLE client."""
        self.authorized = True
        approved = self.md_ts_authority.approve_pending_button()
        if approved is not None and self.md_ts_enabled:
            # Like the robot: push an unsolicited msg_type=2 notification immediately!
            hs2 = build_hs_server_hello(
                approved.client_id,
                approved.key_version,
                self.md_ts_authority.public_key_bytes,
            )
            self._emit_notification(encode_ble_frame(BLE_CMD_MD_TS_SERVER, hs2, flags=BLE_FLAG_REQUEST))

    def reset_authorization(self, require_button: bool = True) -> None:
        """Reset BLE authorization so the next BLE bootstrap requires the physical button press."""
        self.require_button_auth = require_button
        self.authorized = not require_button
        self._handshake.reset()
        self._active_session = None
        self.md_ts_authority.reset(require_button_auth=require_button)

    def pending_session_challenge(self) -> bytes | None:
        """The MD-TS SESSION_CHALLENGE awaiting the client's confirm, if any (test hook)."""
        return self._handshake.pending_challenge()

    def advertisement(self) -> BleRobotAdvertisement:
        mfg = bytes([0x01, 0x10, 0x01 if not self.authorized else 0x02])
        return BleRobotAdvertisement(
            address=self.address,
            name=self.name,
            rssi=-44,
            manufacturer_hex=mfg.hex(),
            pairing_mode=not self.authorized,
            is_simulated=True,
        )

    def subscribe_notify(self, cb: Callable[[bytes], None]) -> Callable[[], None]:
        self._notify_callbacks.append(cb)

        def _unsubscribe() -> None:
            if cb in self._notify_callbacks:
                self._notify_callbacks.remove(cb)
            if not self._notify_callbacks:
                # Match the robot's GATT disconnect cleanup: clear staged/active BLE session state
                self.md_ts_authority.pending_button_client = None
                self._handshake.reset()
                self._active_session = None
                self._rx_parser = BleStreamParser()

        return _unsubscribe

    def read_char1(self) -> bytes:
        return json.dumps(
            {
                "name": self.name,
                "authorized": self.authorized,
                "country": self.country_code,
                "ap_ip": self.ap_ip,
            }
        ).encode("utf-8")

    def write_char0(self, data: bytes) -> None:
        for frame in self._rx_parser.feed(data):
            self._handle_frame(frame)

    def _emit_notification(self, raw_frame: bytes) -> None:
        chunk_sz = max(20, self.att_mtu - 3)
        for off in range(0, len(raw_frame), chunk_sz):
            piece = raw_frame[off : off + chunk_sz]
            for cb in tuple(self._notify_callbacks):
                cb(piece)

    def _handle_md_ts_client_frame(self, frame: BleFrame) -> None:
        if frame.flags == BLE_FLAG_REQUEST:
            try:
                msg = parse_hs_message(frame.payload)
            except ValueError:
                return

            if msg.msg_type == MD_TS_HS_CLIENT_HELLO:
                state, rsp_hs = self.md_ts_authority.handle_ble_client_hello(msg)
                if state == "authorized":
                    self.authorized = True
                self._emit_notification(encode_ble_frame(BLE_CMD_MD_TS_SERVER, rsp_hs, flags=BLE_FLAG_REQUEST))
                return

            if msg.msg_type == MD_TS_HS_SESSION_START:
                entry, err_hs = self.md_ts_authority.lookup_for_session_start(msg, require_permanent=False)
                if err_hs is not None or entry is None:
                    self._emit_notification(
                        encode_ble_frame(
                            BLE_CMD_MD_TS_SERVER,
                            err_hs or build_hs_error(msg.client_id, msg.key_version, MD_TS_ERR_UNAUTHORIZED),
                            flags=BLE_FLAG_REQUEST,
                        )
                    )
                    return

                hs5 = self._handshake.challenge(msg, entry.public_key, self.md_ts_authority)
                self._emit_notification(encode_ble_frame(BLE_CMD_MD_TS_SERVER, hs5, flags=BLE_FLAG_REQUEST))
                return

        elif frame.flags == BLE_FLAG_MD_TS:
            ble_hdr = bytes([BLE_SOF, 5 + len(frame.payload), BLE_FLAG_MD_TS, BLE_CMD_MD_TS_CLIENT])
            result = self._handshake.confirm(frame.payload, ble_hdr=ble_hdr, ble_cmd_id=BLE_CMD_MD_TS_SERVER)
            if result is None:
                return
            if result.session is None:
                self._emit_notification(encode_ble_frame(BLE_CMD_MD_TS_SERVER, result.reply, flags=BLE_FLAG_REQUEST))
                return
            self.md_ts_authority.promote_permanent(result.client_id)
            self.authorized = True
            self._active_session = result.session
            self._emit_notification(encode_ble_frame(BLE_CMD_MD_TS_SERVER, result.reply, flags=BLE_FLAG_MD_TS))

    def _dispatch_inner_command(self, cmd_id: int, pt_payload: bytes) -> tuple[int, bytes] | None:
        if cmd_id == BLE_CMD_SET_COUNTRY:
            if len(pt_payload) >= 2:
                self.country_code = pt_payload[:2].decode("ascii", errors="replace").upper()
                status = b"\x00"
            else:
                status = b"\x01"
            return (BLE_CMD_SET_COUNTRY_ACK, status)

        if cmd_id == BLE_CMD_WIFI_QUERY:
            if self.require_button_auth and not self.authorized:
                return None
            payload = encode_wifi_info_payload(self.ssid, self.password)
            self.connection_status = CONNECTION_STATUS_CONNECTING
            return (BLE_CMD_WIFI_INFO, payload)

        return None

    def _handle_frame(self, frame: BleFrame) -> None:
        if frame.cmd_id == BLE_CMD_MD_TS_CLIENT:
            if self.md_ts_enabled:
                self._handle_md_ts_client_frame(frame)
            return

        if frame.flags == BLE_FLAG_MD_TS:
            if self._active_session is None:
                return
            try:
                pt = self._active_session.open_ble_payload(frame.cmd_id, frame.flags, frame.payload)
            except ValueError:
                return
            res = self._dispatch_inner_command(frame.cmd_id, pt)
            if res is not None:
                rsp_cmd, rsp_pt = res
                _, rec = self._active_session.seal_ble_payload(rsp_cmd, rsp_pt)
                self._emit_notification(encode_ble_frame(rsp_cmd, rec, flags=BLE_FLAG_MD_TS))
            return

        # Plaintext request (flags == 0x00)
        if self.encrypted_handshake_required:
            err = build_hs_error(b"\x01" + b"\x00" * 15, 1, MD_TS_ERR_UNAUTHORIZED)
            self._emit_notification(encode_ble_frame(BLE_CMD_MD_TS_SERVER, err, flags=BLE_FLAG_REQUEST))
            return

        res = self._dispatch_inner_command(frame.cmd_id, frame.payload)
        if res is not None:
            rsp_cmd, rsp_pt = res
            self._emit_notification(encode_ble_frame(rsp_cmd, rsp_pt, flags=BLE_FLAG_RESPONSE))
