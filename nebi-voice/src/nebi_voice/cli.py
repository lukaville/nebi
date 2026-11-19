"""`nebi-voice` command line: say / presets / demo / serve / download-model."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

DEMO_LINES = [
    "Hello?",
    "Ready!",
    "Beep. Nebi!",
    "Hello. I am Nebi.",
    "Whoa! Ta-da!",
    "Where is my ball?",
    "I found something shiny. Do you want it?",
]


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="nebi-voice", description="Small-robot text-to-speech.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("say", help="Synthesize text (saves a WAV and/or plays it).")
    s.add_argument("text", nargs="*", help="What to say (emoji set the emotion). Omit for pet babble.")
    s.add_argument("-e", "--emotion", help="neutral, happy, excited, curious, sad, scared, sleepy, grumpy, love, uh-oh")
    s.add_argument("--babble", action="store_true", help="Pet gibberish instead of words.")
    s.add_argument("--volume", type=float)
    s.add_argument("--timeline", help="Also write the body-sync timeline (envelope, beats, words) as JSON here.")
    s.add_argument("-p", "--preset", default="robot")
    s.add_argument("-o", "--out", help="Output WAV path.")
    s.add_argument("--play", action="store_true", help="Play through speakers (default if no --out).")
    s.add_argument("--seed", type=int, default=0, help="Randomness seed for melody/servo choices.")
    s.add_argument("--voice", dest="base_voice", choices=["M2", "F2"], help="Base tiny-voca voice (M2 male, F2 female).")
    s.add_argument("--pitch", type=float, help="Semitones above the base voice.")
    s.add_argument("--formant", type=float, help="Vocal-tract scale (>1 = smaller robot).")
    s.add_argument("--range", dest="pitch_range", type=float, help="Melodic exaggeration 0..3.")
    s.add_argument("--speed", type=float)
    s.add_argument("--delivery", choices=["auto", "phrase", "halting"])
    s.add_argument("--no-servo", action="store_true")

    sub.add_parser("presets", help="List presets and their parameters (JSON).")

    w = sub.add_parser("serve", help="Run the local web playground (presets, sample texts, every parameter).")
    w.add_argument("--host", default="127.0.0.1")
    w.add_argument("--port", type=int, default=8770)
    w.add_argument("--open", action="store_true", help="Open the page in your browser.")

    d = sub.add_parser("demo", help="Render demo lines for every preset into a folder.")
    d.add_argument("--out-dir", default="samples")
    d.add_argument("--presets", default="robot,probe,nebi,plain")

    m = sub.add_parser("download-model", help="Download the tiny-voca model (~27 MB) now, e.g. before going offline.")
    m.add_argument("--dir", help="Target directory (default: $NEBI_VOICE_MODEL_DIR or the user cache).")

    a = ap.parse_args(argv)
    if a.cmd == "download-model":
        from nebi_voice.engine import ModelUnavailableError, ensure_model

        try:
            print(ensure_model(a.dir))
        except ModelUnavailableError as exc:
            sys.exit(f"nebi-voice: {exc}")
        return
    if a.cmd == "serve":
        from nebi_voice.web import serve

        serve(a.host, a.port, a.open)
        return
    from nebi_voice import PRESETS, RobotVoice

    if a.cmd == "presets":
        print(json.dumps({k: asdict(v) for k, v in PRESETS.items()}, indent=2))
        return
    if a.cmd == "demo":
        out = Path(a.out_dir)
        files = []
        for name in a.presets.split(","):
            v = RobotVoice(name)
            for i, line in enumerate(DEMO_LINES):
                files.append(str(v.save(line, out / f"{name}-{i + 1:02d}.wav")))
        print(json.dumps({"files": files, "lines": DEMO_LINES}, indent=2))
        return

    over = {k: getattr(a, k) for k in ("base_voice", "pitch", "formant", "pitch_range", "speed", "delivery", "volume") if getattr(a, k) is not None}
    if a.no_servo:
        over["servo"] = False
    v = RobotVoice(a.preset, seed=a.seed, **over)
    text = " ".join(a.text)
    from nebi_voice import play_audio

    u = v.render(text, emotion=a.emotion, mode="babble" if a.babble else "auto")
    if a.out:
        import soundfile as sf

        sf.write(a.out, u.audio, u.sample_rate, subtype="PCM_16")
        print(a.out)
    if a.timeline:
        Path(a.timeline).write_text(json.dumps(u.timeline()))
    if u.mode == "babble":
        print(f"[{u.emotion}] {u.text}", file=sys.stderr)
    if a.play or not a.out:
        play_audio(u.audio, u.sample_rate)


if __name__ == "__main__":
    main(sys.argv[1:])
