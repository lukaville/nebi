"""Eye LEDs: colours, expressions, hold-until-release and release (= stop sending frames)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from nebi.domains.led import EXPRESSIONS, LED_COUNT, LedController, pack_rgbw, resolve_expression
from nebi.exceptions import NebiError


class _Bus:
    def __init__(self) -> None:
        self.frames: list[list[int]] = []

    def publish(self, topic: str, **fields: Any) -> int:
        assert topic == "/cmd/led/ctrl_oneway"
        assert len(fields["rgbw_data"]) == LED_COUNT
        self.frames.append(list(fields["rgbw_data"]))
        return int(fields["seq"])


def _eyes() -> tuple[LedController, _Bus]:
    bus = _Bus()
    return LedController(bus), bus  # type: ignore[arg-type]


async def test_color_holds_until_release_then_stops_sending() -> None:
    eyes, bus = _eyes()
    st = await eyes.set_color(0, 0, 255, hold_sec=None)
    assert st["ok"] and st["mode"] == "color" and st["color"] == "#0000ff"
    assert st["until_release"] and st["seconds_left"] is None
    assert bus.frames[0] == [pack_rgbw(0, 0, 255)] * LED_COUNT
    await asyncio.sleep(0.5)
    assert len(bus.frames) >= 3  # re-sent at ~5 Hz
    st = await eyes.release()
    assert st["mode"] == "firmware" and not st["until_release"]
    n = len(bus.frames)
    await asyncio.sleep(0.3)
    assert len(bus.frames) == n  # released: nothing more is sent


async def test_timed_hold_expires_back_to_firmware() -> None:
    eyes, bus = _eyes()
    st = await eyes.set_color(255, 0, 0, hold_sec=0.3, brightness=0.5)
    assert 0 < st["seconds_left"] <= 0.3
    assert bus.frames[0][0] == pack_rgbw(128, 0, 0)
    await asyncio.sleep(0.5)
    assert eyes.eyes_status()["mode"] == "firmware"


async def test_single_frame_and_black_hold() -> None:
    eyes, bus = _eyes()
    await eyes.set_color(1, 2, 3, hold_sec=0)
    assert len(bus.frames) == 1 and eyes.eyes_status()["mode"] == "firmware"
    st = await eyes.set_color(0, 0, 0, hold_sec=5)  # "off" = dark eyes, held
    assert st["mode"] == "color"
    await eyes.stop()


async def test_expression_loops_and_until_release() -> None:
    eyes, bus = _eyes()
    st = await eyes.play_expression("Love", loops=0)
    assert st["expression"] == "heart" and st["until_release"]
    await asyncio.sleep(0.2)
    assert len(bus.frames) >= 3
    await eyes.release()
    st = await eyes.play_expression("happy.csv", loops=1)
    assert st["mode"] == "expression" and st["seconds_left"] > 1.0
    await eyes.stop()


def test_list_expressions() -> None:
    names = [e["name"] for e in LedController.list_expressions()]
    assert names == list(EXPRESSIONS) and "happy" in names
    assert resolve_expression("music") == "music_pulse"


@pytest.mark.parametrize(
    ("call", "code"),
    [
        (lambda e: e.set_color(256, 0, 0), "LED_BAD_COLOR"),
        (lambda e: e.set_color(0, 0, 0, hold_sec=-1), "LED_BAD_HOLD"),
        (lambda e: e.set_color(0, 0, 0, brightness=2), "LED_BAD_BRIGHTNESS"),
        (lambda e: e.play_expression("nope"), "LED_UNKNOWN_EXPRESSION"),
        (lambda e: e.play_expression("happy", loops=-1), "LED_BAD_LOOPS"),
    ],
)
async def test_errors_are_structured(call: Any, code: str) -> None:
    eyes, _ = _eyes()
    with pytest.raises(NebiError) as ei:
        await call(eyes)
    assert ei.value.code == code and ei.value.hint


async def test_legacy_api_still_works() -> None:
    eyes, bus = _eyes()
    seq = await eyes.set_led_rgbw(0, 210, 255, hold_sec=0)
    assert seq == 1 and len(bus.frames) == 1
    await eyes.set_led_expression("wink.csv", play_count=1)
    assert eyes.eyes_status()["expression"] == "wink"
    await eyes.set_led_expression("wink.csv", action=0)
    assert eyes.eyes_status()["mode"] == "firmware"


async def test_concurrent_holds_then_release_stop_all_frames() -> None:
    """Regression: two concurrent hold-until-release calls must not orphan a loop that `release()` can't stop."""
    eyes, bus = _eyes()
    await eyes.set_color(9, 9, 9, hold_sec=None)  # an existing loop both calls race to cancel
    await asyncio.gather(
        eyes.set_color(255, 0, 0, hold_sec=None),
        eyes.set_color(0, 255, 0, hold_sec=None),
        eyes.play_expression("heart", loops=0),
    )
    await asyncio.sleep(0.3)
    await eyes.release()
    n = len(bus.frames)
    await asyncio.sleep(0.5)
    assert len(bus.frames) == n  # nothing keeps streaming after release
    assert eyes.eyes_status()["mode"] == "firmware"
