import itertools

import numpy as np
import pytest

from nebi_voice import SAMPLE_RATE, RobotVoice, find_model
from nebi_voice import effects as fx
from nebi_voice.voice import split_utterances

# Rendering needs the tiny-voca model on disk (`nebi-voice download-model`); the tests never download it.
needs_model = pytest.mark.skipif(find_model() is None, reason="tiny-voca model not downloaded (run `nebi-voice download-model`)")


def test_split_utterances():
    u = split_utterances("Hello? Ready! I found something shiny.")
    assert [x.end for x in u] == ["?", "!", "."]
    assert u[2].words == ["I", "found", "something", "shiny"]


def test_formant_scale_shortens_by_factor():
    x = np.random.default_rng(0).standard_normal(24000).astype(np.float32)
    assert fx.formant_scale(x, 1.25).size == pytest.approx(24000 / 1.25, abs=2)


@needs_model
@pytest.mark.parametrize("preset", ["robot", "probe", "nebi", "plain"])
def test_presets_render(preset):
    a = RobotVoice(preset).synthesize("Hello? I am Nebi.")
    assert a.dtype == np.float32 and a.size > SAMPLE_RATE * 0.5
    assert np.isfinite(a).all() and np.abs(a).max() <= 0.95


@needs_model
def test_seed_fixes_timing_and_melody():
    # tiny-voca's breath-noise source is random inside the ONNX graph, so samples differ slightly,
    # but the seed fixes every choice we make (word gaps, pitch per word, servo sounds) -> same length.
    v = RobotVoice("robot")
    a, b = v.synthesize("Hello? Ready!", seed=3), v.synthesize("Hello? Ready!", seed=3)
    assert a.size == b.size
    assert np.corrcoef(a, b)[0, 1] > 0.8


@needs_model
def test_formant_compensation_keeps_duration():
    base = RobotVoice("plain").synthesize("This is a fairly long sentence for timing.")
    shifted = RobotVoice("plain", formant=1.3).synthesize("This is a fairly long sentence for timing.")
    assert shifted.size == pytest.approx(base.size, rel=0.08)


@needs_model
def test_web_server_roundtrip():
    import json
    import threading
    import urllib.request

    from nebi_voice.web import make_server

    srv = make_server("127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        assert b"Nebi Voice Lab" in urllib.request.urlopen(base + "/").read()
        cfg = json.load(urllib.request.urlopen(base + "/api/config"))
        assert "robot" in cfg["presets"] and cfg["params"]
        body = json.dumps({"text": "Hello?", "preset": "robot", "params": {"pitch": 4, "gap_min": 0.1, "gap_max": 0.2}}).encode()
        r = urllib.request.urlopen(urllib.request.Request(base + "/api/synth", body, {"Content-Type": "application/json"}))
        wav = r.read()
        assert wav[:4] == b"RIFF" and float(r.headers["X-Duration-S"]) > 0.2
    finally:
        srv.shutdown()


@needs_model
def test_emotions_babble_and_timeline():
    from nebi_voice import EMOTIONS

    v = RobotVoice("robot")
    for e in EMOTIONS:
        u = v.render("Where is my ball?", emotion=e)
        assert u.audio.size > SAMPLE_RATE * 0.5 and np.abs(u.audio).max() <= 0.9
    b = v.render(emotion="happy")  # no text -> babble
    assert b.mode == "babble" and b.text and b.text.endswith("!")
    u = v.render("Ball? 👀")  # emoji -> emotion, stripped from speech
    assert u.emotion == "curious" and "👀" not in u.text
    tl = u.timeline()
    assert tl["beats"] and tl["envelope"] and tl["words"][0]["word"] == "Ball"
    with pytest.raises(ValueError):
        v.render("hello", emotion="bogus")


@needs_model
def test_contour_shapes_pitch():
    from nebi_voice import prosody

    v = RobotVoice("robot")
    pw = prosody._pw

    def tail_ratio(a):
        x = a.astype(np.float64)
        # harvest (robust tracker) + trim onset/offset frames, where dio makes octave errors.
        f0, _t = pw.harvest(x, SAMPLE_RATE, f0_floor=60, f0_ceil=900, frame_period=5.0)
        vv = f0[f0 > 0]
        n = len(vv)
        # pitch late in the vowel (70-90%) vs early (20-40%): skips the onset consonant (the "l" in "plant")
        # and the voicing offset on the final "t", both of which wobble from render to render.
        return np.median(vv[int(n * 0.7) : int(n * 0.9)]) / np.median(vv[int(n * 0.2) : int(n * 0.4)])

    # tiny-voca's breath noise is random, so average a few renders and compare shapes relative to each other.
    rise = np.mean([tail_ratio(v._render("Plant", 8, 0.8, 1.9, "rise", 5, 1.3, 1.0)) for _ in range(3)])
    droop = np.mean([tail_ratio(v._render("Plant", 8, 0.8, 1.9, "droop", 5, 1.3, 1.0)) for _ in range(3)])
    assert rise > 1.04 and droop < 0.92 and rise > droop * 1.15


@needs_model
def test_halting_fades_smoothly_and_keeps_one_voice():
    v = RobotVoice("robot")
    amounts = [v._halting_amount(n) for n in range(1, 12)]
    assert all(a >= b for a, b in itertools.pairwise(amounts))  # monotonic, no hard switch
    assert max(a - b for a, b in itertools.pairwise(amounts)) < 0.2
    # Pauses scale with the halting amount: same sentence, fluent vs halting, only gaps differ.
    text = "Where is my ball?"
    fluent = RobotVoice("robot", delivery="phrase").render(text)
    halting = RobotVoice("robot", delivery="halting", halting=1.0).render(text)
    assert halting.duration > fluent.duration + 0.1
    assert [w["word"] for w in halting.words] == ["Where", "is", "my", "ball"]


def test_master_levels_and_fade():
    from nebi_voice import effects as fx

    x = (np.sin(np.linspace(0, 2000, 24000)) * 0.05).astype(np.float32)
    y = fx.master(x, SAMPLE_RATE)
    assert np.abs(y).max() <= 0.9 and abs(y[0]) < 1e-3
    assert 20 * np.log10(np.sqrt((y**2).mean())) > -22  # quiet input brought up to speech level
