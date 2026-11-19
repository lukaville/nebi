"""`nebi.speaker.Speaker`: one voice at a time on the intercom downlink."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from nebi.speaker import Speaker, SpeakerBusyError


class FakeIntercom:
    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self.sessions = 0
        self.active = False
        self.ops: list[str] = []

    async def start(self) -> dict[str, Any]:
        self.active = True
        self.ops.append("start")
        return {"ret_code": 0}

    async def stop(self) -> dict[str, Any]:
        self.active = False
        self.ops.append("stop")
        return {"ret_code": 0}

    def detach(self) -> None:
        self.active = False

    def new_talk_session(self) -> None:
        self.sessions += 1

    def send_pcm(self, pcm: bytes) -> int:
        self.sent.append(pcm)
        return 1


def _speaker() -> tuple[Speaker, FakeIntercom]:
    ic = FakeIntercom()
    sp = Speaker(ic)  # type: ignore[arg-type]
    return sp, ic


def test_new_turn_interrupts_and_stop() -> None:
    sp, _ = _speaker()
    a = sp.begin("cockpit", "hello", 1.0)
    b = sp.begin("nebi say", "bye")
    assert not a.live and a.interrupted_by == "nebi say" and sp.current() is b
    assert sp.stop() is b and sp.current() is None and b.interrupted_by == "stop"
    assert sp.stop() is None


def test_mic_hold_interrupts_and_refuses() -> None:
    sp, _ = _speaker()
    t = sp.begin("cockpit", "x")
    sp.hold_mic("intercom:1", True)
    assert not t.live and t.interrupted_by == "intercom" and sp.busy
    with pytest.raises(SpeakerBusyError) as ei:
        sp.begin("cockpit")
    assert ei.value.code == "SPEAKER_BUSY" and ei.value.hint
    sp.hold_mic("intercom:2", False)  # not the holder: no effect
    assert sp.mic_holder == "intercom:1"
    sp.hold_mic("intercom:1", False)
    assert not sp.busy and sp.begin("cockpit").live


def test_stream_paces_and_stops_on_interrupt() -> None:
    async def main() -> dict[str, Any]:
        sp, ic = _speaker()
        turn = sp.begin("t")
        task = asyncio.create_task(sp.stream(turn, bytes(1920 * 50)))  # 1 s
        await asyncio.sleep(0.3)
        sp.stop()
        stats = await task
        assert ic.sessions == 1 and 5 < len(ic.sent) < 40
        return stats

    stats = asyncio.run(main())
    assert stats["interrupted"] is True


def test_stream_full_utterance() -> None:
    async def main() -> tuple[dict[str, Any], int]:
        sp, ic = _speaker()
        turn = sp.begin("t")
        stats = await sp.stream(turn, bytes(1920 * 5 + 100), lead_s=0.0)
        return stats, len(ic.sent)

    stats, sent = asyncio.run(main())
    assert stats == {"frames_sent": 6} and sent == 6


def test_stream_opens_and_later_closes_intercom_session(monkeypatch) -> None:
    """The robot plays downlink audio only inside an intercom session: speech opens one and closes it after."""
    import nebi.speaker as speaker_mod

    monkeypatch.setattr(speaker_mod, "AUTO_SESSION_LINGER_S", 0.05)

    async def main() -> FakeIntercom:
        sp, ic = _speaker()
        a = sp.begin("a")
        await sp.stream(a, bytes(1920 * 2), lead_s=10)
        sp.end(a)
        assert ic.active and ic.ops == ["start"]
        b = sp.begin("b")
        await sp.stream(b, bytes(1920 * 2), lead_s=10)  # back-to-back: same session
        assert ic.ops == ["start"]
        await asyncio.sleep(0.12)
        assert ic.active  # turn b still live (caller's tail work): session stays open
        sp.end(b)
        await asyncio.sleep(0.15)
        assert not ic.active and ic.ops == ["start", "stop"]
        return ic

    asyncio.run(main())


def test_stream_never_closes_a_session_someone_else_opened(monkeypatch) -> None:
    import nebi.speaker as speaker_mod

    monkeypatch.setattr(speaker_mod, "AUTO_SESSION_LINGER_S", 0.05)

    async def main() -> None:
        sp, ic = _speaker()
        ic.active = True  # the cockpit intercom is running
        await sp.stream(sp.begin("a"), bytes(1920 * 2), lead_s=10)
        await asyncio.sleep(0.15)
        assert ic.active and ic.ops == []
        ic.active = False
        await sp.stream(sp.begin("b"), bytes(1920 * 2), lead_s=10)  # opens its own
        sp.adopt_session()  # then the intercom joins it
        await asyncio.sleep(0.15)
        assert ic.active and ic.ops == ["start"]

    asyncio.run(main())


def test_play_and_say_one_call(monkeypatch) -> None:
    """`play()` / `say()` wrap begin + stream + end, so callers don't manage turns or owner labels."""
    import nebi.speaker as speaker_mod
    from nebi.tts import SpeechAudio

    monkeypatch.setattr(speaker_mod, "AUTO_SESSION_LINGER_S", 0.05)

    async def main() -> None:
        sp, _ic = _speaker()
        stats = await sp.play(bytes(1920 * 3))
        assert stats["frames_sent"] == 3 and stats["duration_s"] == pytest.approx(0.06) and sp.current() is None
        speech = SpeechAudio(audio=[0.0] * 2400, sample_rate=24000, text="hi", emotion="happy")
        stats = await sp.play(speech)
        assert stats["frames_sent"] == 5 and sp.last is not None and sp.last.label == "hi"
        assert sp.last.owner == speaker_mod.DEFAULT_OWNER
        r = await sp.say("Ball? 👀", engine="chirp")  # emoji pick the emotion
        assert r["engine"] == "chirp" and r["emotion"] == "curious" and r["frames_sent"] > 0

    asyncio.run(main())
