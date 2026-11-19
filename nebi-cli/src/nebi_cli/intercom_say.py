"""`nebi tools say`: speak text (or play an audio file) through Nebi's speaker via a running cockpit's `/ws/intercom`.

Goes through the cockpit server because the robot's UCP link is single-client. Uses the exact same
path as the browser (PCM -> server Opus -> `/data/audio/playback_data`), so it is a repeatable
intercom test signal. Text is rendered by the `nebi.tts` registry (the built-in `system` engine — the OS's own
speech — when this machine has one, else `chirp`; `--engine` / $PYNEBI_TTS_ENGINE pick another); audio files are
decoded with PyAV. No ffmpeg binary needed.
"""

from __future__ import annotations

import array
import asyncio
import contextlib
import json
import sys
import time
from typing import Any

import websockets

from nebi.intercom import INTERCOM_FRAME_BYTES, INTERCOM_FRAME_MS, INTERCOM_SAMPLE_RATE
from nebi.tts import get_engine, read_audio_file


def synthesize_pcm(text: str | None = None, wav: str | None = None, voice: str | None = None,
                   engine: str | None = None) -> bytes:
    """Return s16le 48 kHz mono PCM for `text` (TTS, `voice` = engine preset) or an audio file."""
    if wav is not None:
        return read_audio_file(wav, rate=INTERCOM_SAMPLE_RATE).pcm16(INTERCOM_SAMPLE_RATE)
    try:
        eng = get_engine(engine)
    except KeyError as exc:
        raise RuntimeError(str(exc).strip("'\"")) from None
    if voice and voice not in eng.presets:
        raise RuntimeError(f"engine {eng.name!r} has no voice {voice!r} (e.g. {', '.join(list(eng.presets)[:12])})")
    utt = eng.synthesize(text or "", emotion="neutral", babble=False, preset=voice)
    return utt.pcm16(INTERCOM_SAMPLE_RATE)


async def stream_to_robot(
    pcm: bytes,
    server: str = "http://127.0.0.1:8000",
    lead_in_ms: int = 300,
    keep_intercom: bool = False,
) -> dict[str, Any]:
    """Join the cockpit intercom, talk, stream `pcm` in real time (20 ms pacing), then release."""
    ws_url = server.replace("https://", "wss://").replace("http://", "ws://").rstrip("/") + "/ws/intercom"
    frames = [pcm[i : i + INTERCOM_FRAME_BYTES] for i in range(0, len(pcm), INTERCOM_FRAME_BYTES)]
    if frames and len(frames[-1]) < INTERCOM_FRAME_BYTES:
        frames[-1] = frames[-1].ljust(INTERCOM_FRAME_BYTES, b"\x00")
    silence = bytes(INTERCOM_FRAME_BYTES)
    lead = [silence] * max(0, lead_in_ms // INTERCOM_FRAME_MS)
    tail = [silence] * 15  # let the robot's 100-300 ms jitter buffer drain
    last_state: dict[str, Any] = {}
    errors: list[str] = []

    async with websockets.connect(ws_url, max_size=None, proxy=None) as ws:

        async def reader() -> None:
            async for msg in ws:
                if isinstance(msg, str):
                    obj = json.loads(msg)
                    if obj.get("type") == "state":
                        last_state.update(obj)
                    elif obj.get("type") == "error":
                        errors.append(obj.get("message", ""))

        rd = asyncio.create_task(reader())
        await ws.send(json.dumps({"type": "start"}))
        await asyncio.sleep(0.4)
        if not last_state.get("connected", True):
            raise RuntimeError("Cockpit is not connected to a robot")
        await ws.send(json.dumps({"type": "talk", "on": True}))
        await asyncio.sleep(0.05)
        t0 = time.monotonic()
        for i, fr in enumerate(lead + frames + tail):
            await ws.send(fr)
            delay = t0 + (i + 1) * INTERCOM_FRAME_MS / 1000 - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
        await ws.send(json.dumps({"type": "talk", "on": False}))
        await asyncio.sleep(0.6)  # one state tick
        if not keep_intercom:
            await ws.send(json.dumps({"type": "stop"}))
            await asyncio.sleep(0.1)
        rd.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await rd
    return {
        "frames_sent": len(lead) + len(frames) + len(tail),
        "speech_s": round(len(pcm) / 96000, 2),
        "server_tx_frames": last_state.get("tx_frames"),
        "errors": errors,
    }


def main_say(args: Any) -> None:
    """CLI entry point for `nebi tools say` (prints the JSON result; exits 1 on failure)."""
    try:
        pcm = synthesize_pcm(text=None if args.wav else " ".join(args.text), wav=args.wav, voice=args.voice,
                             engine=getattr(args, "engine", None))
        if args.gain != 1.0:
            a = array.array("h", pcm)
            for i, v in enumerate(a):
                a[i] = max(-32768, min(32767, int(v * args.gain)))
            pcm = a.tobytes()
        res = asyncio.run(stream_to_robot(pcm, server=args.server, keep_intercom=args.keep))
        print(json.dumps(res, indent=2))
    except Exception as exc:
        print(f"say failed: {exc}", file=sys.stderr)
        sys.exit(1)
