"""`nebi-voice serve`: a tiny local web playground (stdlib http.server, no extra dependencies).

Routes:
  GET  /             the playground page (web.html)
  GET  /api/config   presets, parameter metadata (ranges/groups), sample texts
  POST /api/synth    {"text", "preset", "params": {...overrides}, "seed"} -> audio/wav
"""

from __future__ import annotations

import io
import json
import threading
import time
import webbrowser
from dataclasses import asdict, fields
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import Any
from urllib.parse import quote

import soundfile as sf

from nebi_voice.emotion import EMOTIONS
from nebi_voice.engine import SAMPLE_RATE
from nebi_voice.voice import PRESETS, RobotVoice, VoiceParams, get_engine

MAX_TEXT = 800

# UI metadata: group, label, min, max, step (selects/toggles have their own kind).
PARAM_META: list[dict[str, Any]] = [
    {"group": "Source", "key": "base_voice", "label": "Base voice", "kind": "select", "options": ["M2", "F2"]},
    {"group": "Source", "key": "pitch", "label": "Pitch (semitones)", "min": -12, "max": 16, "step": 0.5},
    {"group": "Source", "key": "formant", "label": "Formant / throat size", "min": 0.8, "max": 1.6, "step": 0.01, "hint": ">1 smaller robot, <1 bigger"},
    {"group": "Source", "key": "pitch_range", "label": "Melody exaggeration", "min": 0, "max": 3, "step": 0.05},
    {"group": "Source", "key": "speed", "label": "Speed", "min": 0.5, "max": 1.6, "step": 0.05},
    {"group": "Delivery", "key": "delivery", "label": "Delivery", "kind": "select", "options": ["auto", "phrase", "halting"]},
    {"group": "Delivery", "key": "halting", "label": "Halting amount", "min": 0, "max": 1, "step": 0.05, "hint": "0 = fluent, 1 = word-by-word pauses"},
    {"group": "Delivery", "key": "halting_max_words", "label": "Auto: halting fades out around N words", "min": 1, "max": 14, "step": 1},
    {"group": "Delivery", "key": "gap_min", "label": "Word gap min (s)", "min": 0, "max": 0.6, "step": 0.01},
    {"group": "Delivery", "key": "gap_max", "label": "Word gap max (s)", "min": 0, "max": 0.8, "step": 0.01},
    {"group": "Delivery", "key": "word_pitch_jitter", "label": "Per-word pitch jitter", "min": 0, "max": 6, "step": 0.1},
    {"group": "Delivery", "key": "stretch_last_word", "label": "Last-word speed", "min": 0.4, "max": 1.2, "step": 0.05},
    {"group": "Delivery", "key": "question_lift", "label": "Question lift (st)", "min": 0, "max": 6, "step": 0.25},
    {"group": "Delivery", "key": "exclaim_lift", "label": "Exclaim lift (st)", "min": 0, "max": 6, "step": 0.25},
    {"group": "Warble", "key": "vibrato_hz", "label": "Vibrato rate (Hz)", "min": 0, "max": 12, "step": 0.1},
    {"group": "Warble", "key": "vibrato_cents", "label": "Vibrato depth (cents)", "min": 0, "max": 80, "step": 1},
    {"group": "Warble", "key": "vibrato_drift", "label": "Servo jitter", "min": 0, "max": 1, "step": 0.05},
    {"group": "Warble", "key": "tremolo_hz", "label": "Tremolo rate (Hz)", "min": 0, "max": 20, "step": 0.5},
    {"group": "Warble", "key": "tremolo_depth", "label": "Tremolo depth", "min": 0, "max": 0.5, "step": 0.01},
    {"group": "Metal", "key": "ring_hz", "label": "Ring mod (Hz)", "min": 0, "max": 400, "step": 1},
    {"group": "Metal", "key": "ring_mix", "label": "Ring mix", "min": 0, "max": 0.6, "step": 0.01},
    {"group": "Metal", "key": "box_delay_ms", "label": "Box size (ms)", "min": 0.5, "max": 8, "step": 0.1},
    {"group": "Metal", "key": "box_feedback", "label": "Box resonance", "min": 0, "max": 0.9, "step": 0.01},
    {"group": "Metal", "key": "box_mix", "label": "Box mix", "min": 0, "max": 1, "step": 0.01},
    {"group": "Metal", "key": "crush_bits", "label": "Bit depth", "min": 4, "max": 16, "step": 1},
    {"group": "Metal", "key": "crush_hold", "label": "Sample hold", "min": 1, "max": 8, "step": 1},
    {"group": "Metal", "key": "crush_mix", "label": "Crush mix", "min": 0, "max": 1, "step": 0.01},
    {"group": "Speaker", "key": "speaker_low_hz", "label": "Low cut (Hz, 0 off)", "min": 0, "max": 800, "step": 10},
    {"group": "Speaker", "key": "speaker_high_hz", "label": "High cut (Hz, 0 off)", "min": 0, "max": 12000, "step": 100},
    {"group": "Speaker", "key": "presence_db", "label": "Presence (dB)", "min": -6, "max": 12, "step": 0.5},
    {"group": "Speaker", "key": "drive", "label": "Drive", "min": 1, "max": 5, "step": 0.05},
    {"group": "Mechanics", "key": "servo", "label": "Servo whirs & clicks", "kind": "toggle"},
    {"group": "Mechanics", "key": "servo_prob", "label": "Servo probability", "min": 0, "max": 1, "step": 0.05},
    {"group": "Mechanics", "key": "boop", "label": "Boop before speaking", "kind": "toggle"},
    {"group": "Output", "key": "glide", "label": "Pitch glides & vowel stretch", "min": 0, "max": 2, "step": 0.05, "hint": "emotion contours drawn on words"},
    {"group": "Output", "key": "volume", "label": "Volume", "min": 0, "max": 1.5, "step": 0.05},
    {"group": "Output", "key": "master", "label": "Mastering (compress + level + limit)", "kind": "toggle"},
]

SAMPLES: dict[str, list[str]] = {
    "Pet (emoji set the mood)": ["Hi! 😊", "Uh-oh 😬", "Ball? 👀", "Sleepy… 😴", "Yay! 🎉", "❤️", "No! 😠", "Help! 😱"],
    "Robot-isms": ["Hello?", "Ready!", "Beep. Nebi!", "Whoa! Ta-da!", "Uh-oh.", "Hmm? Ooh!", "Where is my ball?"],
    "Nebi": [
        "Hello. I am Nebi.",
        "Battery low. Going home.",
        "I found something shiny. Do you want it?",
        "Photo time! Say cheese!",
        "Follow me!",
        "Good morning! Did you sleep well?",
    ],
    "Longer": [
        "I rolled all the way to the kitchen and back, and I only bumped into the table twice.",
        "Scanning the room. I see one person, one cat, and a very suspicious sock.",
    ],
}


def _preset_ui(p: VoiceParams) -> dict[str, Any]:
    d = asdict(p)
    gmin, gmax = d.pop("word_gap")
    d["gap_min"], d["gap_max"] = gmin, gmax
    return d


_FIELD_TYPES = {f.name: f.type for f in fields(VoiceParams)}


def _overrides(raw: dict[str, Any]) -> dict[str, Any]:
    """Validate/coerce UI params into VoiceParams overrides (unknown keys are ignored)."""
    out: dict[str, Any] = {}
    raw = dict(raw or {})
    if "gap_min" in raw or "gap_max" in raw:
        a, b = float(raw.pop("gap_min", 0.07)), float(raw.pop("gap_max", 0.18))
        out["word_gap"] = (min(a, b), max(a, b))
    for k, v in raw.items():
        t = str(_FIELD_TYPES.get(k, ""))
        if not t or k == "description":
            continue
        if t == "bool":
            out[k] = bool(v)
        elif t == "int":
            out[k] = int(round(float(v)))
        elif t == "float":
            out[k] = float(v)
        elif t == "str":
            out[k] = str(v)
    if out.get("base_voice") not in (None, "M2", "F2"):
        raise ValueError("base_voice must be M2 or F2")
    if out.get("delivery") not in (None, "auto", "phrase", "halting"):
        raise ValueError("delivery must be auto, phrase or halting")
    return out


class _Handler(BaseHTTPRequestHandler):
    server_version = "nebi-voice"

    def log_message(self, fmt: str, *args: Any) -> None:  # quieter console
        pass

    def _send(self, status: int, body: bytes, ctype: str, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, obj: Any) -> None:
        self._send(status, json.dumps(obj).encode(), "application/json")

    def do_GET(self) -> None:
        if self.path in ("/", "/index.html"):
            html = resources.files("nebi_voice").joinpath("web.html").read_bytes()
            self._send(HTTPStatus.OK, html, "text/html; charset=utf-8")
        elif self.path == "/api/config":
            self._json(
                HTTPStatus.OK,
                {
                    "presets": {k: {"description": v.description, "params": _preset_ui(v)} for k, v in PRESETS.items()},
                    "params": PARAM_META,
                    "samples": SAMPLES,
                    "emotions": list(EMOTIONS),
                    "sample_rate": SAMPLE_RATE,
                },
            )
        else:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/api/synth":
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            text = str(body.get("text", "")).strip()[:MAX_TEXT]
            mode = str(body.get("mode") or "auto")
            emotion = body.get("emotion") or None
            if not text and mode != "babble":
                raise ValueError("text is empty (or use Babble)")
            preset = str(body.get("preset") or "robot")
            if preset not in PRESETS:
                raise ValueError(f"unknown preset {preset!r}")
            seed = int(body.get("seed") or 0)
            voice = RobotVoice(preset, seed=seed, **_overrides(body.get("params") or {}))
            t0 = time.perf_counter()
            utt = voice.render(text, emotion=emotion, mode=mode)
            audio = utt.audio
            ms = (time.perf_counter() - t0) * 1000
            if audio.size == 0:
                raise ValueError("nothing to say (no pronounceable words)")
            buf = io.BytesIO()
            sf.write(buf, audio, SAMPLE_RATE, format="WAV", subtype="PCM_16")
            self._send(
                HTTPStatus.OK,
                buf.getvalue(),
                "audio/wav",
                {
                    "X-Render-Ms": f"{ms:.0f}",
                    "X-Duration-S": f"{audio.size / SAMPLE_RATE:.2f}",
                    "X-Spoken": quote(utt.text),
                    "X-Emotion": utt.emotion,
                    "X-Mode": utt.mode,
                },
            )
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception as exc:  # pragma: no cover
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"{type(exc).__name__}: {exc}"})


def make_server(host: str = "127.0.0.1", port: int = 8770) -> ThreadingHTTPServer:
    get_engine()  # load the model up front so the first click is fast
    return ThreadingHTTPServer((host, port), _Handler)


def serve(host: str = "127.0.0.1", port: int = 8770, open_browser: bool = False) -> None:
    srv = make_server(host, port)
    url = f"http://{host}:{srv.server_address[1]}/"
    print(f"nebi-voice playground: {url}  (Ctrl+C to stop)", flush=True)
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
