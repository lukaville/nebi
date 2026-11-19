"""The UCP transport's heartbeat word is sent before the motion layer takes over: it must decode as neutral."""
from nebi.latch import BIT59
from nebi.transport.ucp import UCP_JOYSTICK_NEUTRAL_WORD, UcpEndpointSession
from nebi.types import JoystickState


def test_transport_heartbeat_word_is_neutral():
    st = JoystickState.unpack(UCP_JOYSTICK_NEUTRAL_WORD)
    assert (st.lx, st.ly, st.rx, st.ry, st.roll) == (0.0, 0.0, 0.0, 0.0, 0.0)
    assert round(st.height * 31) == 16 and not st.stand_up_fold and not st.estop and not st.jump_trigger
    assert UCP_JOYSTICK_NEUTRAL_WORD == 0x0102_0FBE_FA1F_43E8


def test_session_heartbeat_carries_latch(tmp_path, monkeypatch):
    monkeypatch.setenv("PYNEBI_LATCH_FILE", str(tmp_path / "latch.json"))
    from nebi.latch import save_latch
    save_latch("10.0.0.5", True)
    s = UcpEndpointSession(lambda b: None, latch_key="10.0.0.5")
    assert s.joystick_word & BIT59
    assert round(JoystickState.unpack(s.joystick_word & ~BIT59).height * 31) == 16


def test_latch_unknown_host_falls_back_to_latest(tmp_path, monkeypatch):
    import json
    import time

    from nebi.latch import load_latch
    f = tmp_path / "latch.json"
    f.write_text(json.dumps({"robots": {"192.0.2.10": {"stand_up_fold": True, "saved_at": time.time()},
                                        "10.0.0.5": {"stand_up_fold": False, "saved_at": 1.0}}}))
    monkeypatch.setenv("PYNEBI_LATCH_FILE", str(f))
    assert load_latch("192.168.7.1") is True
    assert load_latch("10.0.0.5") is False
    f.write_text(json.dumps({"robots": {}}))
    assert load_latch("192.168.7.1") is False
