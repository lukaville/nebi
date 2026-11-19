"""Safety invariants of the joystick path: nothing stale resumes, the bit-59 latch never flips by accident."""
from __future__ import annotations

import asyncio
import json
import struct
import time

import pytest

from nebi.client import AsyncNebiClient
from nebi.domains import motion as motion_mod
from nebi.domains.motion import MotionController
from nebi.exceptions import NebiError
from nebi.transport import ucp as ucp_mod
from nebi.transport.ucp import UCP_JOYSTICK_NEUTRAL_WORD, UcpEndpointSession, joystick_hold_word
from nebi.types import JoystickState, RobotTelemetrySnapshot, _clamp

BIT59 = 1 << 59


def _sent_words(sent: list[bytes]) -> list[int]:
    # 31-byte 0x1051 packets: 12 B header + 8 B word + 11 B SACK
    return [struct.unpack_from("<Q", d, 12)[0] & 0x7FFF_FFFF_FFFF_FFFF for d in sent if len(d) == 31]


def test_heartbeat_holds_after_stale_command(monkeypatch):
    sent: list[bytes] = []
    s = UcpEndpointSession(send_datagram=sent.append)
    driving = JoystickState(ly=0.8, rx=-0.5, jump_trigger=True).pack() | BIT59
    s.send_joystick_word(driving)
    assert _sent_words(sent)[-1] == driving & 0x7FFF_FFFF_FFFF_FFFF  # fresh: sent as is
    monkeypatch.setattr(ucp_mod, "JOYSTICK_HOLD_AFTER_S", 0.0)
    time.sleep(0.01)
    s.send_heartbeat_probe()
    held = JoystickState.unpack(_sent_words(sent)[-1])
    assert held.ly == 0.0 and held.rx == 0.0 and not held.jump_trigger  # stale: sticks neutral, trigger cleared
    assert held.stand_up_fold  # latched bit preserved: expiry never creates a stand/fold edge
    assert joystick_hold_word(driving) & BIT59


def test_new_session_reloads_latch_and_never_resumes(tmp_path, monkeypatch):
    f = tmp_path / "latch.json"
    monkeypatch.setenv("PYNEBI_LATCH_FILE", str(f))
    f.write_text(json.dumps({"robots": {"10.0.0.9": {"stand_up_fold": False, "saved_at": time.time()}}}))
    s = UcpEndpointSession(send_datagram=lambda b: None, latch_key="10.0.0.9")
    assert not s.joystick_word & BIT59
    s.send_joystick_word(JoystickState(ly=1.0).pack())
    # meanwhile another process folded the robot and saved latch=1
    f.write_text(json.dumps({"robots": {"10.0.0.9": {"stand_up_fold": True, "saved_at": time.time()}}}))
    s.prepare_new_session()
    st = JoystickState.unpack(s.joystick_word)
    assert st.stand_up_fold and st.ly == 0.0


class _FakeUcp:
    def __init__(self) -> None:
        self.words: list[int] = []

    def send_joystick_word(self, w: int) -> None:
        self.words.append(w)


class _ClosedBus:
    is_open = False


def _controller(tel: RobotTelemetrySnapshot | None = None):
    ucp = _FakeUcp()
    m = MotionController(_ClosedBus(), get_ucp_session=lambda: ucp, get_telemetry=(lambda: tel) if tel else None)
    return m, ucp


def test_stick_axes_expire(monkeypatch):
    m, ucp = _controller()
    m.set_axes(ly=0.7, rx=0.3, height=0.9)
    assert JoystickState.unpack(ucp.words[-1]).ly == pytest.approx(0.7, abs=0.01)
    monkeypatch.setattr(motion_mod, "AXIS_TTL_S", 0.0)
    time.sleep(0.01)
    m.send_joystick_once()
    st = JoystickState.unpack(ucp.words[-1])
    assert st.ly == 0.0 and st.rx == 0.0
    assert st.height == pytest.approx(0.9, abs=0.02)  # level settings are not velocities: kept


def test_custom_state_cannot_drop_the_latch():
    m, ucp = _controller()
    m.joystick.stand_up_fold = True
    m._latch_reconciled = True
    m.send_joystick_once(JoystickState(ly=0.2))  # caller-built state with stand_up_fold=False
    assert ucp.words[-1] & BIT59


def test_neutral_works_offline():
    m = MotionController(_ClosedBus(), get_ucp_session=lambda: None)
    m.set_axes(lx=0.5, ly=0.5, rx=0.5, ry=0.5)
    m.neutral()
    j = m.joystick
    assert (j.lx, j.ly, j.rx, j.ry) == (0.0, 0.0, 0.0, 0.0)


def test_stand_refuses_without_fresh_posture():
    tel = RobotTelemetrySnapshot()  # robot_state_ts = 0: nothing received
    m, _ = _controller(tel)
    m.joystick.stand_up_fold = False
    with pytest.raises(NebiError) as ei:
        asyncio.run(m.stand())
    assert ei.value.code == "POSTURE_UNKNOWN"


def test_telemetry_defaults_are_not_fabricated():
    t = RobotTelemetrySnapshot()
    assert t.posture_name == "UNKNOWN" and t.robot_state == -1 and t.wifi_ssid == "" and t.serial_number == ""


def test_clamp_rejects_non_finite():
    assert _clamp(float("nan"), -1.0, 1.0) == 0.0
    assert _clamp(float("inf"), -1.0, 1.0) == 0.0
    JoystickState(ly=float("nan"), height=float("nan")).pack()  # must not raise


def test_controllers_survive_transport_rebuild():
    c = AsyncNebiClient("127.0.0.1", auto_reconnect=False)
    m, cam, sysc = c.motion, c.camera, c.system
    m.set_axes(height=0.8)
    c._build_transports()
    assert c.motion is m and c.camera is cam and c.system is sysc
    assert c.motion._bus is c.bus and c.camera._bus is c.bus and c.system._config_http is c.config_http
    assert c.motion.joystick.height == pytest.approx(0.8)


def test_link_loss_mid_handshake_does_not_spawn_reconnect():
    async def run() -> None:
        c = AsyncNebiClient("127.0.0.1", auto_reconnect=True)
        c.motion.set_axes(ly=0.6)
        c._connected = False  # still handshaking
        c._on_ucp_link_lost("silence")
        assert c._reconnect_task is None
        assert c.motion.joystick.ly == 0.0
        await c.close()

    asyncio.run(run())


def test_unreliable_8_byte_zenoh_frame_refused():
    s = UcpEndpointSession(send_datagram=lambda b: None)
    with pytest.raises(ValueError):
        s.send_zenoh_frame(b"\xa7" + b"\x00" * 7, reliable=False)


def test_neutral_word_constant_unchanged():
    assert UCP_JOYSTICK_NEUTRAL_WORD == 0x0102_0FBE_FA1F_43E8


def test_estop_and_neutral_zero_every_axis():
    """Regression: emergency_stop / neutral must also centre ry and roll, not just lx/ly/rx."""
    m, ucp = _controller()
    m.set_axes(lx=0.4, ly=0.5, rx=0.6, ry=0.7, roll=0.8)
    asyncio.run(m.emergency_stop())
    for w in ucp.words[-4:]:  # the estop pulse frames
        st = JoystickState.unpack(w)
        assert (st.lx, st.ly, st.rx, st.ry, st.roll) == (0.0, 0.0, 0.0, 0.0, 0.0)
    j = m.joystick
    assert (j.lx, j.ly, j.rx, j.ry, j.roll) == (0.0, 0.0, 0.0, 0.0, 0.0)
    m.set_axes(ry=0.5, roll=-0.5)
    m.neutral()
    st = JoystickState.unpack(ucp.words[-1])
    assert (st.ry, st.roll) == (0.0, 0.0)


def test_joystick_stream_survives_send_errors(caplog):
    """Regression: a non-cancel exception in one send must not silently end the 50 Hz stream."""

    class _FlakyUcp(_FakeUcp):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        def send_joystick_word(self, w: int) -> None:
            self.calls += 1
            if self.calls <= 3:
                raise OSError("transient send failure")
            super().send_joystick_word(w)

    ucp = _FlakyUcp()
    m = MotionController(_ClosedBus(), get_ucp_session=lambda: ucp)

    async def run() -> None:
        await m.start_joystick_stream(hz=100)
        await asyncio.sleep(0.2)
        assert m._stream_task is not None and not m._stream_task.done()
        await m.stop_joystick_stream()

    asyncio.run(run())
    assert len(ucp.words) >= 3  # kept sending after the failures
    assert "joystick stream send failed" in caplog.text
