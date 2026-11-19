"""The robot drops /data/audio/playback_data frames that carry `header` (verified on the robot 2026-11-17)."""
from nebi.catalog.registry import CATALOG
from nebi.intercom import PLAYBACK_TOPIC, build_playback_fields
from nebi.transport.zenoh_ucp import ZenohTopicBus


class _FakeSession:
    def __init__(self) -> None:
        self.puts: list[tuple[str, bytes]] = []

    def put(self, key, payload, **_kw) -> None:
        self.puts.append((str(key), bytes(payload)))


def _bus() -> ZenohTopicBus:
    bus = ZenohTopicBus(role="app")
    bus._session = _FakeSession()
    bus.md_ts_required = False
    return bus


def test_playback_frames_have_no_header_but_explicit_encoding_type():
    bus = _bus()
    bus.publish(PLAYBACK_TOPIC, **build_playback_fields(b"\x78" * 40, 0))
    key, raw = bus._session.puts[-1]
    assert key.endswith("/data/audio/playback_data")
    msg = CATALOG.get_proto_class(PLAYBACK_TOPIC)()
    msg.ParseFromString(raw)
    assert not msg.HasField("header")
    assert msg.data.HasField("encoding_type") and msg.data.encoding_type == 0
    assert msg.data.data_size == len(msg.data.data) == 40


def test_other_topics_keep_header_seq():
    bus = _bus()
    bus.publish("/cmd/audio/op_req", operation_type=0)
    _, raw = bus._session.puts[-1]
    msg = CATALOG.get_proto_class("/cmd/audio/op_req")()
    msg.ParseFromString(raw)
    assert msg.header.seq_num > 0


def test_led_and_hms_action_response_are_headerless():
    """The robot's codecs for these inbound topics reject a header (not yet confirmed on hardware)."""
    bus = _bus()
    bus.publish("/cmd/led/ctrl_oneway", seq=1, timestamp_us=1, rgbw_data=[0] * 14)
    bus.publish("/cmd/hms/action_response", command_id=5, action_id=1, result_code=0, dispatcher_instance_id=7,
                delivery_store_id=bytes(16))
    for (key, raw), topic in zip(bus._session.puts[-2:], ("/cmd/led/ctrl_oneway", "/cmd/hms/action_response")):
        assert key.endswith(topic)
        msg = CATALOG.get_proto_class(topic)()
        msg.ParseFromString(raw)
        assert not msg.HasField("header")
