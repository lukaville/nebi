"""Tests for the `MD-TS` (`MD-TS-HS` + AES-256-GCM) BLE & Zenoh protocol and Home Wi-Fi onboarding."""

from __future__ import annotations

import asyncio
import stat
from pathlib import Path

import pytest

from nebi.ble import MockBlePeripheral, NebiBleClient
from nebi.catalog.registry import CATALOG
from nebi.client import AsyncNebiClient
from nebi.exceptions import NebiBleError, NebiHandshakeError
from nebi.md_ts import (
    MD_TS_DIR_C2S,
    MD_TS_ROLE_CLIENT,
    MD_TS_ROLE_SERVER,
    MD_TS_TRANSPORT_BLE,
    MD_TS_TRANSPORT_ZENOH,
    MdTsClientIdentity,
    MdTsIdentityStore,
    MdTsSession,
    build_ble_aad,
    build_hs_client_hello,
    build_zenoh_aad,
    compute_transcript_hash,
    decode_zenoh_envelope,
    derive_md_ts_session_keys,
    encode_zenoh_hs_envelope,
)
from nebi.simulator import MockNebiRobot
from nebi.types import ConnectionStage


def test_md_ts_crypto_transcript_aad_replay_and_zenoh_envelope(tmp_path: Path) -> None:
    # 1. Persistent identity store creates 0o600 file and round-trips paired robot public keys
    store_file = tmp_path / "identity.json"
    store = MdTsIdentityStore(store_file)
    ident = store.identity
    assert len(ident.client_id) == 16
    assert len(ident.public_key_bytes) == 32
    assert store_file.exists()
    mode = stat.S_IMODE(store_file.stat().st_mode)
    assert mode == 0o600

    robot_ident = MdTsClientIdentity.generate()
    store.save_robot_public_key(
        robot_ident.public_key_bytes,
        ble_address="AA:BB:CC:11:22:33",
        ssid="Nebi-LZ110-77",
        host="192.0.2.20",
    )

    reloaded = MdTsIdentityStore(store_file)
    assert reloaded.identity.client_id == ident.client_id
    assert reloaded.identity.private_key_bytes == ident.private_key_bytes
    assert reloaded.lookup_robot_public_key(ble_address="aa:bb:cc:11:22:33") == robot_ident.public_key_bytes
    assert reloaded.lookup_robot_public_key(ssid="Nebi-LZ110-77") == robot_ident.public_key_bytes
    assert reloaded.lookup_robot_public_key(host="192.0.2.20") == robot_ident.public_key_bytes

    # 2. Exact AAD byte lengths (`35B` BLE vs `55 + len(keyexpr)` Zenoh) & 168B transcript hash
    channel_id = bytes.fromhex("0102030405060708")
    c_nonce = bytes(range(1, 33))
    s_nonce = bytes(range(101, 133))
    ble_aad = build_ble_aad(MD_TS_DIR_C2S, channel_id, 1, bytes([0x55, 42, 0x04, 0x50]))
    assert len(ble_aad) == 35
    assert ble_aad[:11] == b"MD-TS-AAD\x00\x01"

    keyexpr = "app/cmd/nm_wifi_connect_req"
    zenoh_aad = build_zenoh_aad(MD_TS_DIR_C2S, channel_id, 1, keyexpr)
    assert len(zenoh_aad) == 55 + len(keyexpr)
    assert zenoh_aad[:11] == b"MD-TS-AAD\x00\x01"

    th_ble = compute_transcript_hash(
        transport_type=MD_TS_TRANSPORT_BLE,
        client_id=ident.client_id,
        key_version=ident.key_version,
        client_static_pub=ident.public_key_bytes,
        robot_static_pub=robot_ident.public_key_bytes,
        client_nonce=c_nonce,
        server_nonce=s_nonce,
        channel_id=channel_id,
    )
    th_zenoh = compute_transcript_hash(
        transport_type=MD_TS_TRANSPORT_ZENOH,
        client_id=ident.client_id,
        key_version=ident.key_version,
        client_static_pub=ident.public_key_bytes,
        robot_static_pub=robot_ident.public_key_bytes,
        client_nonce=c_nonce,
        server_nonce=s_nonce,
        channel_id=channel_id,
    )
    assert len(th_ble) == 32
    assert th_ble != th_zenoh  # Transport separation (`transport_type=1` vs `2`)

    # 3. BLE & Zenoh session pairing, `seq=0` handshake confirmation, `seq>=1` app traffic, and anti-replay
    cli_ble = MdTsSession.from_handshake(
        role=MD_TS_ROLE_CLIENT,
        transport_type=MD_TS_TRANSPORT_BLE,
        local_private_key_bytes=ident.private_key_bytes,
        peer_public_key_bytes=robot_ident.public_key_bytes,
        client_id=ident.client_id,
        key_version=ident.key_version,
        client_static_pub=ident.public_key_bytes,
        robot_static_pub=robot_ident.public_key_bytes,
        client_nonce=c_nonce,
        server_nonce=s_nonce,
        channel_id=channel_id,
    )
    srv_ble = MdTsSession.from_handshake(
        role=MD_TS_ROLE_SERVER,
        transport_type=MD_TS_TRANSPORT_BLE,
        local_private_key_bytes=robot_ident.private_key_bytes,
        peer_public_key_bytes=ident.public_key_bytes,
        client_id=ident.client_id,
        key_version=ident.key_version,
        client_static_pub=ident.public_key_bytes,
        robot_static_pub=robot_ident.public_key_bytes,
        client_nonce=c_nonce,
        server_nonce=s_nonce,
        channel_id=channel_id,
    )
    _, k_c2s, k_s2c = derive_md_ts_session_keys(
        local_private_key_bytes=ident.private_key_bytes,
        peer_public_key_bytes=robot_ident.public_key_bytes,
        transport_type=MD_TS_TRANSPORT_BLE,
        client_id=ident.client_id,
        key_version=ident.key_version,
        client_static_pub=ident.public_key_bytes,
        robot_static_pub=robot_ident.public_key_bytes,
        client_nonce=c_nonce,
        server_nonce=s_nonce,
        channel_id=channel_id,
    )
    assert k_c2s != k_s2c

    c_conf = cli_ble.seal_handshake_confirm(client_id=ident.client_id, key_version=ident.key_version)
    assert len(c_conf) == 87
    srv_ble.open_handshake_confirm(c_conf, expected_client_id=ident.client_id, expected_key_version=ident.key_version)
    assert srv_ble.handshake_confirmed is True

    s_conf = srv_ble.seal_handshake_confirm(client_id=ident.client_id, key_version=ident.key_version)
    assert len(s_conf) == 87
    cli_ble.open_handshake_confirm(s_conf, expected_client_id=ident.client_id, expected_key_version=ident.key_version)
    assert cli_ble.handshake_confirmed is True

    # Application record at `seq = 1`
    hdr1, rec1 = cli_ble.seal_ble_payload(0x50, b"")
    assert hdr1[0] == 0x55 and hdr1[2] == 0x04 and hdr1[3] == 0x50
    assert srv_ble.open_ble_payload(0x50, 0x04, rec1) == b""

    # Replaying `rec1` must be rejected by the 1024-packet sliding window
    with pytest.raises(ValueError, match="Replayed"):
        srv_ble.open_ble_payload(0x50, 0x04, rec1)

    # Wrong BLE cmd_id in AAD must fail AES-256-GCM tag verification
    hdr2, rec2 = cli_ble.seal_ble_payload(0x53, b"US")
    assert hdr2[3] == 0x53
    with pytest.raises(ValueError, match="AES-256-GCM"):
        srv_ble.open_ble_payload(0x50, 0x04, rec2)

    # 4. `MdTsZenohEnvelope` round-trip & zero false-positives against standard `WireHeader` Protobufs
    ping_cls = CATALOG.get_proto_class("/cmd/common/ping_req")
    assert ping_cls is not None
    ping_msg = ping_cls()
    ping_msg.header.seq_num = 42
    ping_msg.data.ping_data = b"hello-nebi"
    normal_pb = ping_msg.SerializeToString()
    assert decode_zenoh_envelope(normal_pb) is None

    hs_bytes = build_hs_client_hello(ident.client_id, ident.key_version, ident.public_key_bytes)
    hs_env = decode_zenoh_envelope(encode_zenoh_hs_envelope(hs_bytes))
    assert hs_env == ("handshake", hs_bytes)

    cli_z = MdTsSession.from_handshake(
        role=MD_TS_ROLE_CLIENT,
        transport_type=MD_TS_TRANSPORT_ZENOH,
        local_private_key_bytes=ident.private_key_bytes,
        peer_public_key_bytes=robot_ident.public_key_bytes,
        client_id=ident.client_id,
        key_version=ident.key_version,
        client_static_pub=ident.public_key_bytes,
        robot_static_pub=robot_ident.public_key_bytes,
        client_nonce=c_nonce,
        server_nonce=s_nonce,
        channel_id=channel_id,
    )
    srv_z = MdTsSession.from_handshake(
        role=MD_TS_ROLE_SERVER,
        transport_type=MD_TS_TRANSPORT_ZENOH,
        local_private_key_bytes=robot_ident.private_key_bytes,
        peer_public_key_bytes=ident.public_key_bytes,
        client_id=ident.client_id,
        key_version=ident.key_version,
        client_static_pub=ident.public_key_bytes,
        robot_static_pub=robot_ident.public_key_bytes,
        client_nonce=c_nonce,
        server_nonce=s_nonce,
        channel_id=channel_id,
    )
    enc_wire = cli_z.seal_zenoh_payload(keyexpr, normal_pb)
    parsed_env = decode_zenoh_envelope(enc_wire)
    assert parsed_env is not None and parsed_env[0] == "encrypted"
    assert srv_z.open_zenoh_record(keyexpr, parsed_env[1]) == normal_pb

    # Wrong Zenoh keyexpr in AAD must fail AES-256-GCM tag verification
    enc_wire2 = decode_zenoh_envelope(cli_z.seal_zenoh_payload(keyexpr, normal_pb))
    assert enc_wire2 is not None
    with pytest.raises(ValueError, match="AES-256-GCM"):
        srv_z.open_zenoh_record("app/cmd/other_topic", enc_wire2[1])


@pytest.mark.asyncio
async def test_ble_md_ts_button_hold_and_permanent_authorization_reconnect(tmp_path: Path) -> None:
    store = MdTsIdentityStore(tmp_path / "id.json")
    periph = MockBlePeripheral(
        ssid="Nebi-LZ110-99",
        password="wpa2-secret-value",
        require_button_auth=True,
        encrypted_handshake_required=True,
    )

    # 1. Without button hold, `msg_type = 1 -> 3 (PENDING_BUTTON)` times out with `BLE_AUTH_REQUIRED`
    seen_wait = False

    def _on_wait() -> None:
        nonlocal seen_wait
        seen_wait = True

    async with NebiBleClient(mock_peripheral=periph, identity_store=store) as ble:
        with pytest.raises(NebiBleError) as exc_info:
            await ble.bootstrap_wifi_credentials(wait_for_button_sec=0.25, on_awaiting_button=_on_wait)
        assert exc_info.value.code == "BLE_AUTH_REQUIRED"
        assert seen_wait is True

    # 2. Simulate physical 3-5s button hold -> unsolicited `msg_type = 2` -> `4 -> 5 -> 6 -> 7` -> encrypted `0x53/0x50`!
    async def _press_button() -> None:
        await asyncio.sleep(0.15)
        periph.press_pairing_button()

    press_task = asyncio.create_task(_press_button())
    async with NebiBleClient(mock_peripheral=periph, identity_store=store) as ble:
        creds = await ble.bootstrap_wifi_credentials(country_code="DE", wait_for_button_sec=2.0)
        assert creds.md_ts_established is True
        assert creds.ssid == "Nebi-LZ110-99"
        assert creds.password == "wpa2-secret-value"
        assert periph.country_code == "DE"
        assert bytes.fromhex(creds.robot_public_key_hex) == periph.md_ts_authority.public_key_bytes
    await press_task

    # 3. Second BLE session reuses permanently authorized `status = 2` X25519 key WITHOUT needing button hold!
    second_wait = False

    def _on_wait_second() -> None:
        nonlocal second_wait
        second_wait = True

    async with NebiBleClient(mock_peripheral=periph, identity_store=store) as ble2:
        creds2 = await ble2.bootstrap_wifi_credentials(country_code="US", wait_for_button_sec=0.3, on_awaiting_button=_on_wait_second)
        assert second_wait is False
        assert creds2.md_ts_established is True
        assert creds2.ssid == "Nebi-LZ110-99"


@pytest.mark.asyncio
async def test_retail_robot_ble_pairing_zenoh_md_ts_and_home_wifi_onboarding(tmp_path: Path) -> None:
    """End-to-end simulation of a retail `LZ110` robot enforcing `MD-TS` on both BLE and Zenoh-over-UCP."""
    sim = MockNebiRobot(
        ucp_port=0,
        http_port=0,
        video_fps=10,
        md_ts_enabled=True,
        md_ts_zenoh_required=True,
    )
    await sim.start()
    try:
        sim.reset_ble_auth(require_button=True)
        unpaired_store = MdTsIdentityStore(tmp_path / "unpaired.json")
        # Give client the robot's static pubkey without authorizing `unpaired_store` on the robot -> must fail with ZENOH_AUTH_REQUIRED!
        unpaired_store.save_robot_public_key(sim.md_ts_authority.public_key_bytes, host="127.0.0.1")

        kw = sim.client_kwargs()
        unauth_client = AsyncNebiClient(
            **kw,
            identity_store=unpaired_store,
            security_mode="md-ts",
            auto_reconnect=False,
        )
        with pytest.raises(NebiHandshakeError) as exc_unauth:
            await unauth_client.connect(timeout=2.0)
        assert exc_unauth.value.code == "ZENOH_AUTH_REQUIRED"
        await unauth_client.close()

        # Now run one-shot `pair_ble_and_provision_home_wifi` with simulated 3-5s button hold!
        paired_store = MdTsIdentityStore(tmp_path / "paired.json")
        stages: list[str] = []

        def _on_stage(st: ConnectionStage, _detail: str, _err: object) -> None:
            stages.append(st.value)
            if st == ConnectionStage.BLE_AWAITING_BUTTON_AUTH:
                asyncio.get_running_loop().call_later(0.1, sim.press_pairing_button)

        summary, client = await AsyncNebiClient.pair_ble_and_provision_home_wifi(
            "HomeFiber-5G",
            "home-wpa2-passphrase",
            ble_address=sim.ble_peripheral.address,
            identity_store=paired_store,
            simulator=sim,
            wait_for_button_sec=3.0,
            on_stage=_on_stage,
        )
        assert client is not None
        try:
            assert summary["ssid"] == "HomeFiber-5G"
            assert summary["md_ts_active"] is True
            assert client.connected is True
            assert client.bus.md_ts_active is True
            assert ConnectionStage.BLE_AWAITING_BUTTON_AUTH.value in stages
            assert ConnectionStage.SECURITY_HANDSHAKING.value in stages
            assert ConnectionStage.WIFI_PROVISIONING.value in stages

            # Verify RPCs & telemetry flow seamlessly inside encrypted `MdTsZenohEnvelope` frames
            pong = await client.system.ping(b"md_ts_hello_16")
            assert pong["echo"] == "md_ts_hello_16"
            assert sim.wifi_mode == 1  # 1 = STA
            assert sim.wifi_ssid == "HomeFiber-5G"
            assert client.telemetry.link.md_ts_active is True
        finally:
            await client.close()
    finally:
        await sim.stop()


