"""Unit and integration tests for BLE GATT framing, Multi-Flow Connection, Provisioning, Watchdog, and Errors."""

from __future__ import annotations

import asyncio

import pytest

from nebi import (
    AsyncNebiClient,
    BleStreamParser,
    ConnectionFlow,
    ConnectionStage,
    MockBlePeripheral,
    NebiBleClient,
    NebiBleError,
    NebiHandshakeError,
    NebiWifiProvisionError,
    crc8_autosar,
    create_bound_udp_socket,
    discover_robots,
    encode_ble_frame,
    list_network_interfaces,
)
from nebi.ble import BLE_CMD_SET_COUNTRY, BLE_CMD_WIFI_INFO, BLE_FLAG_RESPONSE, encode_wifi_info_payload
from nebi.simulator import MockNebiRobot


def test_crc8_autosar_and_ble_stream_reassembly() -> None:
    # Verify polynomial 0x2F against the first CRC-8/AUTOSAR table entries [0x00, 0x2f, 0x5e, 0x71] (with init=0x00)
    assert crc8_autosar(b"\x00", init=0x00) == 0x00
    assert crc8_autosar(b"\x01", init=0x00) == 0x2F
    assert crc8_autosar(b"\x02", init=0x00) == 0x5E
    assert crc8_autosar(b"\x03", init=0x00) == 0x71

    payload = encode_wifi_info_payload("Nebi-A9F2", "wpa2secret99")
    raw_rsp = encode_ble_frame(BLE_CMD_WIFI_INFO, payload, flags=BLE_FLAG_RESPONSE)
    req_cc = encode_ble_frame(BLE_CMD_SET_COUNTRY, b"US")

    # Inject leading garbage, false 0x55 with invalid flags (0x08), and 7-byte ATT MTU fragmentation
    corrupt_prefix = b"\x00\xff\x55\x08\x08\x50\x11"
    stream = corrupt_prefix + raw_rsp + req_cc

    parser = BleStreamParser()
    decoded = []
    for off in range(0, len(stream), 7):
        decoded.extend(parser.feed(stream[off : off + 7]))

    assert len(decoded) == 2
    assert decoded[0].cmd_id == BLE_CMD_WIFI_INFO
    assert decoded[0].flags == BLE_FLAG_RESPONSE
    assert decoded[0].payload == payload
    assert decoded[1].cmd_id == BLE_CMD_SET_COUNTRY
    assert decoded[1].payload == b"US"


@pytest.mark.asyncio
async def test_ble_5s_button_authorization_and_encrypted_rejection() -> None:
    periph = MockBlePeripheral(
        ssid="Nebi-LZ110-42",
        password="supersecret",
        require_button_auth=True,
        att_mtu=23,  # forces 20-byte GATT notification chunking
    )

    # 1. Without pressing the 5s button, bootstrap times out with BLE_AUTH_REQUIRED
    awaiting_called = False

    def _on_wait() -> None:
        nonlocal awaiting_called
        awaiting_called = True

    async with NebiBleClient(mock_peripheral=periph) as ble:
        with pytest.raises(NebiBleError) as exc_info:
            await ble.bootstrap_wifi_credentials(wait_for_button_sec=0.35, on_awaiting_button=_on_wait)
        assert exc_info.value.code == "BLE_AUTH_REQUIRED"
        assert exc_info.value.stage == ConnectionStage.BLE_AWAITING_BUTTON_AUTH.value
        assert awaiting_called is True

    # 2. Simulate user pressing the physical button 0.2s into the wait window -> succeeds!
    async def _press_soon() -> None:
        await asyncio.sleep(0.2)
        periph.press_pairing_button()

    press_task = asyncio.create_task(_press_soon())
    async with NebiBleClient(mock_peripheral=periph) as ble:
        creds = await ble.bootstrap_wifi_credentials(country_code="JP", wait_for_button_sec=2.0)
        assert creds.ssid == "Nebi-LZ110-42"
        assert creds.password == "supersecret"
        assert periph.country_code == "JP"
    await press_task

    # 3. Encrypted MD-TS-HS challenge: plaintext-only mode surfaces BLE_SECURITY_HANDSHAKE_REQUIRED cleanly,
    #    whereas default security_mode="auto" completes the full X25519 + AES-256-GCM MD-TS session!
    enc_periph = MockBlePeripheral(encrypted_handshake_required=True)
    in_mem_store = NebiBleClient(mock_peripheral=enc_periph).identity_store
    async with NebiBleClient(mock_peripheral=enc_periph, identity_store=in_mem_store, security_mode="plaintext") as ble:
        with pytest.raises(NebiBleError) as exc_enc:
            await ble.bootstrap_wifi_credentials(wait_for_button_sec=0.5)
        assert exc_enc.value.code == "BLE_SECURITY_HANDSHAKE_REQUIRED"

    async with NebiBleClient(mock_peripheral=enc_periph, identity_store=in_mem_store) as ble:
        creds_enc = await ble.bootstrap_wifi_credentials(country_code="US", wait_for_button_sec=1.0)
        assert creds_enc.md_ts_established is True
        assert len(creds_enc.robot_public_key_hex) == 64
        assert creds_enc.ssid == "Nebi-SIM001"


@pytest.mark.asyncio
async def test_split_socket_binding_discovery_and_ble_bootstrap_flow() -> None:
    ifaces = list_network_interfaces(include_loopback=True)
    assert any(i.ipv4.startswith("127.") for i in ifaces)

    sock = create_bound_udp_socket("127.0.0.1", 19999, local_ip="127.0.0.1")
    sock.close()

    sim = MockNebiRobot(ucp_port=0, http_port=0, video_fps=12)
    await sim.start()
    try:
        sim.reset_ble_auth(require_button=True)
        found = await discover_robots(include_ble=True, include_lan=False, simulator=sim, timeout=0.8)
        flows = {r.flow for r in found}
        assert "simulator" in flows
        assert "ble_bootstrap" in flows

        stages: list[str] = []

        def _on_stage(st: ConnectionStage, _detail: str, _err: object) -> None:
            stages.append(st.value)
            if st == ConnectionStage.BLE_AWAITING_BUTTON_AUTH:
                # Press 5s physical button as soon as client enters BLE_AWAITING_BUTTON_AUTH!
                asyncio.get_running_loop().call_later(0.1, sim.press_pairing_button)

        client = await AsyncNebiClient.connect_flow(
            ConnectionFlow.BLE_BOOTSTRAP,
            ble_address=sim.ble_peripheral.address,
            simulator=sim,
            wait_for_button_sec=3.0,
            on_stage=_on_stage,
        )
        try:
            assert client.connected
            assert client.bootstrapped_ap_credentials is not None
            assert client.bootstrapped_ap_credentials.ssid == "Nebi-SIM001"
            assert ConnectionStage.BLE_AWAITING_BUTTON_AUTH.value in stages
            assert ConnectionStage.UCP_HANDSHAKING.value in stages
            assert ConnectionStage.ZENOH_TUNNELING.value in stages
            assert ConnectionStage.CONNECTED.value in stages

            # Test transactional Home Wi-Fi provisioning failure (WRONG_KEY -> watchdog SoftAP rollback)
            with pytest.raises(NebiWifiProvisionError) as prov_err:
                await client.provision_home_wifi_and_handoff("Studio-5G", "wrong")
            assert prov_err.value.code == "WIFI_PROVISION_WRONG_KEY"
            assert client.connected  # Watchdog rolled back safely; AP link remains alive!
            assert sim.wifi_mode == 0

            # Test transactional Home Wi-Fi provisioning success (`AP -> STA`)
            ok_res = await client.provision_home_wifi_and_handoff("Studio-5G", "goodpassword123")
            assert ok_res["status"] == "succeeded"
            assert client.telemetry.wifi_mode == "STA"
            assert client.telemetry.wifi_ssid == "Studio-5G"
            assert sim.ble_peripheral.advertising is False

            # Switch back to SoftAP
            await client.system.switch_wifi_to_ap()
            await asyncio.sleep(0.15)
            assert sim.wifi_mode == 0
            assert sim.ble_peripheral.advertising is True
        finally:
            await client.close()
    finally:
        await sim.stop()


@pytest.mark.asyncio
async def test_ucp_watchdog_safety_stop_and_auto_reconnect() -> None:
    sim = MockNebiRobot(ucp_port=0, http_port=0, video_fps=15)
    await sim.start()
    try:
        kw = sim.client_kwargs()
        client = AsyncNebiClient(
            **kw,
            local_ip="127.0.0.1",
            heartbeat_timeout=0.75,
            degraded_silence_sec=0.25,
            auto_reconnect=True,
        )
        seen_stages: list[str] = []
        client.add_stage_listener(lambda st, _msg, _err: seen_stages.append(st.value))
        await client.connect()
        try:
            # Command forward motion before link drop
            client.motion.set_axes(ly=0.85, rx=0.35)
            assert client.motion.state.ly == pytest.approx(0.85, abs=0.01)

            # Drop UDP link for 1.05s (> 0.75s heartbeat_timeout)
            sim.simulate_link_drop(duration_sec=1.05)

            # Wait for degraded -> link loss -> safety zero -> reconnect -> connected
            for _ in range(35):
                await asyncio.sleep(0.1)
                if ConnectionStage.RECONNECTING.value in seen_stages and client.connected:
                    break

            # Joystick axes MUST have been immediately zeroed for safety upon link loss
            assert client.motion.state.ly == 0.0
            assert client.motion.state.rx == 0.0
            assert ConnectionStage.DEGRADED.value in seen_stages
            assert ConnectionStage.RECONNECTING.value in seen_stages
            assert client.connected
            pong = await client.system.ping(b"recovered")
            assert pong["echo"] == "recovered"
        finally:
            await client.close()
    finally:
        await sim.stop()


@pytest.mark.asyncio
async def test_unreachable_host_raises_structured_handshake_error() -> None:
    # Bind ephemeral closed UDP port on 127.0.0.1 to guarantee immediate UCP handshake timeout
    client = AsyncNebiClient(
        host="127.0.0.1",
        ucp_port=59998,
        config_port=59997,
        flow="lan_sta",
        auto_reconnect=False,
    )
    with pytest.raises(NebiHandshakeError) as exc_info:
        await client.connect(timeout=0.6)
    assert exc_info.value.code == "UCP_HANDSHAKE_TIMEOUT"
    assert exc_info.value.stage == ConnectionStage.UCP_HANDSHAKING.value
    assert exc_info.value.hint != ""
    await client.close()


