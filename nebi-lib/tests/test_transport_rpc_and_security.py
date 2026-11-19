"""RPC correlation, publish-once semantics, identity-file safety, BLE handshake robustness and secret hygiene."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

from nebi.ble import (
    BLE_CMD_MD_TS_SERVER,
    BLE_FLAG_MD_TS,
    BLE_FLAG_REQUEST,
    BleFrame,
    MockBlePeripheral,
    NebiBleClient,
    encode_ble_frame,
)
from nebi.catalog.registry import CATALOG
from nebi.exceptions import (
    NebiBleError,
    NebiError,
    NebiLinkLostError,
    NebiNotConnectedError,
    NebiRpcTimeoutError,
)
from nebi.md_ts import (
    MdTsIdentityStore,
    MdTsServerAuthority,
)
from nebi.network import discovery as net_discovery
from nebi.network import wifi as net_wifi
from nebi.transport.zenoh_ucp import ZenohTopicBus

REQ = "/cmd/vt/reset_conn_req"
RSP = "/cmd/vt/reset_conn_rsp"


class _FakeSession:
    def __init__(self) -> None:
        self.puts: list[tuple[str, bytes]] = []

    def put(self, key_expr: str, payload: bytes, **_kw: Any) -> None:
        self.puts.append((key_expr, bytes(payload)))


def _fake_bus() -> tuple[ZenohTopicBus, _FakeSession]:
    bus = ZenohTopicBus(role="app")
    sess = _FakeSession()
    bus._session = sess  # type: ignore[assignment]
    bus._loop = asyncio.get_running_loop()
    return bus, sess


def _deliver(bus: ZenohTopicBus, topic: str, seq: int | None, ret_code: int) -> None:
    cls = CATALOG.get_proto_class(topic)
    assert cls is not None
    msg = cls()
    if seq is not None:
        msg.header.seq_num = seq
    msg.data.ret_code = ret_code
    bus._dispatch_decoded("device" + topic, topic, msg.SerializeToString())


def _req_seq(raw: bytes) -> int:
    cls = CATALOG.get_proto_class(REQ)
    assert cls is not None
    return int(cls.FromString(raw).header.seq_num)


@pytest.mark.asyncio
async def test_transport_concurrent_rpcs_same_reply_topic_without_seq_echo() -> None:
    bus, sess = _fake_bus()
    t1 = asyncio.create_task(bus.request(REQ, RSP, timeout=2.0))
    t2 = asyncio.create_task(bus.request(REQ, RSP, timeout=2.0))
    await asyncio.sleep(0.05)
    assert len(sess.puts) == 2
    # Robot answers without echoing header.seq_num: FIFO delivery, both callers get an answer.
    _deliver(bus, RSP, None, 11)
    _deliver(bus, RSP, None, 22)
    r1, r2 = await asyncio.gather(t1, t2)
    assert (r1.data.ret_code, r2.data.ret_code) == (11, 22)
    assert bus._pending_rpcs == {}


@pytest.mark.asyncio
async def test_transport_concurrent_rpcs_matched_by_echoed_seq() -> None:
    bus, sess = _fake_bus()
    t1 = asyncio.create_task(bus.request(REQ, RSP, timeout=2.0))
    t2 = asyncio.create_task(bus.request(REQ, RSP, timeout=2.0))
    await asyncio.sleep(0.05)
    s1, s2 = (_req_seq(p) for _k, p in sess.puts)
    # Replies arrive out of order but echo the request seq: each goes to its own caller.
    _deliver(bus, RSP, s2, 2)
    _deliver(bus, RSP, s1, 1)
    r1, r2 = await asyncio.gather(t1, t2)
    assert (r1.data.ret_code, r2.data.ret_code) == (1, 2)


@pytest.mark.asyncio
async def test_transport_stale_seq_reply_does_not_resolve_other_request() -> None:
    """Regression (H1): a late reply echoing an expired request's seq must not answer a newer request."""
    bus, sess = _fake_bus()
    with pytest.raises(NebiRpcTimeoutError):
        await bus.request(REQ, RSP, timeout=0.05)
    stale_seq = _req_seq(sess.puts[0][1])
    t2 = asyncio.create_task(bus.request(REQ, RSP, timeout=2.0))
    await asyncio.sleep(0.02)
    _deliver(bus, RSP, stale_seq, 99)  # late answer to the first (timed-out) request
    await asyncio.sleep(0.02)
    assert not t2.done()
    _deliver(bus, RSP, _req_seq(sess.puts[1][1]), 5)
    assert (await t2).data.ret_code == 5


@pytest.mark.asyncio
async def test_transport_close_fails_inflight_request_with_link_closed() -> None:
    """Regression (H3): closing the bus must not surface as CancelledError in the caller."""
    bus, _sess = _fake_bus()
    t = asyncio.create_task(bus.request(REQ, RSP, timeout=2.0))
    await asyncio.sleep(0.02)
    bus._session = None  # nothing real to close
    await bus.close()
    with pytest.raises(NebiLinkLostError) as exc_info:
        await t
    assert exc_info.value.code == "LINK_CLOSED" and exc_info.value.hint
    assert not t.cancelled()


@pytest.mark.asyncio
async def test_transport_request_published_once_and_typed_timeout() -> None:
    bus, sess = _fake_bus()
    with pytest.raises(NebiRpcTimeoutError) as exc_info:
        await bus.request(REQ, RSP, timeout=1.2)
    assert exc_info.value.code == "RPC_TIMEOUT"
    assert isinstance(exc_info.value, asyncio.TimeoutError)  # old `except asyncio.TimeoutError` keeps working
    assert len(sess.puts) == 1  # no blind republish of a possibly non-idempotent command
    assert bus._pending_rpcs == {}


@pytest.mark.asyncio
async def test_transport_closed_bus_raises_not_connected() -> None:
    bus = ZenohTopicBus(role="app")
    with pytest.raises(NebiNotConnectedError):
        await bus.request(REQ, RSP, timeout=0.1)
    with pytest.raises(RuntimeError):  # still a RuntimeError for legacy callers
        bus.publish(REQ)


@pytest.mark.asyncio
async def test_transport_listener_exception_does_not_break_dispatch(caplog: pytest.LogCaptureFixture) -> None:
    bus, _sess = _fake_bus()
    seen: list[int] = []

    def _bad(_t: str, _m: Any, _s: int) -> None:
        raise RuntimeError("boom")

    async def _async_bad(_t: str, _m: Any, _s: int) -> None:
        raise RuntimeError("async boom")

    bus.subscribe_topic(RSP, _bad)
    bus.subscribe_topic(RSP, _async_bad)
    bus.subscribe_all(lambda _t, m, _s: seen.append(m.data.ret_code))
    fut = asyncio.create_task(bus.request(REQ, RSP, timeout=1.0))
    await asyncio.sleep(0.02)
    _deliver(bus, RSP, None, 7)
    assert (await fut).data.ret_code == 7
    await asyncio.sleep(0.02)
    assert seen == [7]
    assert not bus._bg_tasks  # finished background task was released
    assert "boom" in caplog.text


def test_transport_corrupt_identity_not_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "identity.json"
    path.write_text('{"client_id_hex": "zz', encoding="utf-8")
    with pytest.raises(NebiError) as exc_info:
        _ = MdTsIdentityStore(path).identity
    assert exc_info.value.code == "IDENTITY_CORRUPT"
    assert path.read_text(encoding="utf-8") == '{"client_id_hex": "zz'
    backups = list(tmp_path.glob("identity.json.corrupt-*"))
    assert len(backups) == 1 and backups[0].read_text(encoding="utf-8") == '{"client_id_hex": "zz'


def test_transport_identity_file_private_and_merged_across_stores(tmp_path: Path) -> None:
    path = tmp_path / "identity.json"
    a = MdTsIdentityStore(path)
    cid = a.identity.client_id
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    b = MdTsIdentityStore(path)
    assert b.identity.client_id == cid
    key_a, key_b = bytes(range(1, 33)), bytes(range(2, 34))
    a.save_robot_public_key(key_a, ble_address="AA:AA", device_name="Nebi-A")
    b.save_robot_public_key(key_b, ble_address="BB:BB", device_name="Nebi-B")  # b's snapshot predates a's save
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert {"ble:aa:aa", "ble:bb:bb", "name:Nebi-A", "name:Nebi-B"} <= set(raw["known_robots"])
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert not list(tmp_path.glob("*.tmp"))


class _StaleDupPeripheral(MockBlePeripheral):
    """Emits a stale duplicate msg_type=5 (plaintext) right before the real msg_type=7."""

    def _handle_md_ts_client_frame(self, frame: BleFrame) -> None:
        dup5 = self.pending_session_challenge()
        if frame.flags == BLE_FLAG_MD_TS and dup5 is not None:
            self._emit_notification(encode_ble_frame(BLE_CMD_MD_TS_SERVER, dup5, flags=BLE_FLAG_REQUEST))
            self._emit_notification(encode_ble_frame(BLE_CMD_MD_TS_SERVER, b"\x01garbage", flags=BLE_FLAG_REQUEST))
        super()._handle_md_ts_client_frame(frame)


@pytest.mark.asyncio
async def test_transport_ble_handshake_ignores_stale_plaintext_frames() -> None:
    periph = _StaleDupPeripheral(encrypted_handshake_required=True)
    async with NebiBleClient(mock_peripheral=periph) as ble:
        assert await ble.perform_md_ts_handshake(wait_for_button_sec=1.0) is True
        assert ble.md_ts_active


@pytest.mark.asyncio
async def test_transport_ble_robot_key_change_requires_explicit_repair() -> None:
    store = MdTsIdentityStore(in_memory=True)
    periph1 = MockBlePeripheral(encrypted_handshake_required=True)
    async with NebiBleClient(mock_peripheral=periph1, identity_store=store) as ble:
        assert await ble.perform_md_ts_handshake() is True

    # Same BLE address / name, different robot static key (factory reset or impersonation).
    periph2 = MockBlePeripheral(encrypted_handshake_required=True, md_ts_authority=MdTsServerAuthority())
    async with NebiBleClient(mock_peripheral=periph2, identity_store=store) as ble:
        with pytest.raises(NebiBleError) as exc_info:
            await ble.perform_md_ts_handshake()
        assert exc_info.value.code == "BLE_ROBOT_KEY_CHANGED"
    async with NebiBleClient(mock_peripheral=periph2, identity_store=store, allow_robot_key_change=True) as ble:
        assert await ble.perform_md_ts_handshake() is True
    assert store.lookup_robot_public_key(ble_address=periph2.address) == periph2.md_ts_authority.public_key_bytes


class _FakeProc:
    def __init__(self, out: bytes, rc: int, hang: bool = False) -> None:
        self._out, self.returncode, self._hang = out, rc, hang
        self.killed = False
        self.stdin_data: bytes | None = None

    async def communicate(self, data: bytes | None = None) -> tuple[bytes, bytes]:
        self.stdin_data = data
        if self._hang:
            await asyncio.sleep(3600)
        return self._out, b""

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        return -9


@pytest.mark.asyncio
async def test_transport_wifi_join_error_never_contains_password(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "hunter2-wifi-pass"
    calls: list[tuple[Any, ...]] = []
    proc = _FakeProc(f"Error: could not join network with key {secret}".encode(), 1)

    async def _fake_exec(*args: Any, **_kw: Any) -> _FakeProc:
        calls.append(args)
        return proc

    monkeypatch.setattr(net_wifi.sys, "platform", "linux")
    monkeypatch.setattr(net_wifi.asyncio, "create_subprocess_exec", _fake_exec)
    with pytest.raises(NebiError) as exc_info:
        await net_wifi.join_os_wifi("HomeNet", secret, interface="wlan0")
    dumped = json.dumps(exc_info.value.to_dict()) + str(exc_info.value)
    assert secret not in dumped
    assert all(secret not in str(a) for a in calls[0])  # nmcli gets the password over stdin, not argv
    assert proc.stdin_data == (secret + "\n").encode()


@pytest.mark.asyncio
async def test_transport_wifi_join_timeout_kills_child(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = _FakeProc(b"", 0, hang=True)

    async def _fake_exec(*_a: Any, **_kw: Any) -> _FakeProc:
        return proc

    real_wait_for = asyncio.wait_for

    async def _fast_wait_for(aw: Any, timeout: float) -> Any:
        return await real_wait_for(aw, timeout=min(timeout, 0.05))

    monkeypatch.setattr(net_wifi.sys, "platform", "linux")
    monkeypatch.setattr(net_wifi.asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(net_wifi.asyncio, "wait_for", _fast_wait_for)
    with pytest.raises(NebiError) as exc_info:
        await net_wifi.join_os_wifi("HomeNet", "pw12345678")
    assert exc_info.value.code == "OS_WIFI_JOIN_FAILED"
    assert proc.killed


def test_transport_ssid_lookup_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    n = {"calls": 0}

    def _q(_iface: str | None) -> str | None:
        n["calls"] += 1
        return "Net"

    net_wifi._invalidate_ssid_cache()
    monkeypatch.setattr(net_wifi, "_query_current_wifi_ssid", _q)
    assert net_wifi.get_current_wifi_ssid("en9") == "Net"
    assert net_wifi.get_current_wifi_ssid("en9") == "Net"
    assert n["calls"] == 1
    net_wifi._invalidate_ssid_cache()


@pytest.mark.skipif(sys.platform == "win32", reason="posix only")
@pytest.mark.asyncio
async def test_transport_probe_without_ucp_never_sends_udp(monkeypatch: pytest.MonkeyPatch) -> None:
    def _no_socket(*_a: Any, **_kw: Any) -> Any:
        raise AssertionError("UCP probe must not open a socket when ucp_probe=False")

    monkeypatch.setattr(net_discovery, "create_bound_udp_socket", _no_socket)
    # TEST-NET-1 address: never routed, HTTP probe fails fast.
    assert await net_discovery.probe_nebi_endpoint("192.0.2.1", timeout=0.1, ucp_probe=False) is None
