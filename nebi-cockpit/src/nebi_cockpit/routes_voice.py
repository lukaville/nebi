"""Text-to-speech through Nebi's speaker (`/api/voice/*`, the cockpit "Speak" panel).

Engines come from `nebi.tts` (built-in `chirp` plus whatever extensions register). Audio goes out on the intercom
downlink as one *turn* on the client's shared speaker arbiter (`client.intercom.speaker`, `nebi.speaker`), the same
one `nebi say` and the intercom push-to-talk use, so:

* one voice at a time: a new `say` (from here or from `nebi say`) interrupts the current one; `stop` silences it;
* while someone holds the intercom talk button, `say` is refused with 409 ``SPEAKER_BUSY``.

Errors are the cockpit's usual JSON (`ok: false`, `detail`, `error: {code, message, hint, ...}`).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request
from pydantic import Field

from nebi.exceptions import NebiError
from nebi.speaker import SpeakerBusyError
from nebi.tts import EMOTIONS, TtsEngine, engines, get_engine, split_emoji
from nebi_cockpit.deps import ClientDep, ManagerDep
from nebi_cockpit.schemas import _Body

if TYPE_CHECKING:
    from nebi.client import AsyncNebiClient
    from nebi.speaker import SpeakerTurn
    from nebi.tts import SpeechAudio

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/voice", tags=["voice"])

ROBOT_RATE = 48000
OWNER = "cockpit"
MAX_TEXT_CHARS = 500


class SayBody(_Body):
    text: str = Field("", max_length=MAX_TEXT_CHARS)
    engine: str | None = None  # default: $PYNEBI_TTS_ENGINE, else the best registered engine
    emotion: str | None = None  # default: from an emoji in the text, else "neutral"
    preset: str | None = None  # engine voice preset (default: the engine's first)
    volume: float = Field(1.0, ge=0.0, le=2.0)  # gain applied to the rendered audio
    babble: bool = False  # wordless babble (also used when the text is empty)
    wait: bool = False  # True: reply after playback ends (with frames_sent / interrupted)


def voice_error(code: str, message: str, hint: str = "", **details: Any) -> NebiError:
    return NebiError(code, message, stage="voice", hint=hint, details=details)


def _engine_info(e: TtsEngine, default: str | None) -> dict[str, Any]:
    presets = list(e.presets)
    return {"name": e.name, "emotions": list(e.emotions), "presets": presets, "default_preset": presets[0] if presets else None,
            "priority": int(getattr(e, "priority", 10)), "default": e.name == default}


def _tasks(request: Request) -> set[asyncio.Task[Any]]:
    tasks: set[asyncio.Task[Any]] | None = getattr(request.app.state, "voice_tasks", None)
    if tasks is None:
        tasks = set()
        request.app.state.voice_tasks = tasks
    return tasks


async def _play(client: AsyncNebiClient, turn: SpeakerTurn, utt: SpeechAudio) -> dict[str, Any]:
    speaker = client.intercom.speaker
    try:
        return await speaker.stream(turn, utt.pcm16(ROBOT_RATE), alive=lambda: client.connected)
    except Exception:
        logger.warning("voice playback failed", exc_info=True)
        raise
    finally:
        speaker.end(turn)


@router.get("/engines")
async def api_voice_engines() -> dict[str, Any]:
    """TTS engines (best first) with their emotions and voice presets; `default` is what `say` uses without `engine`."""
    try:
        default: str | None = get_engine().name
    except KeyError:
        default = None
    return {"default": default, "engines": [_engine_info(e, default) for e in engines()], "emotions": list(EMOTIONS)}


@router.get("/status")
async def api_voice_status(manager: ManagerDep) -> dict[str, Any]:
    """Is Nebi speaking (who/what/remaining), and is the intercom mic held."""
    client = manager.client
    if client is None or not client.connected:
        return {"connected": False, "speaking": False, "mic_held": False, "turn": None}
    return {"connected": True, **client.intercom.speaker.status()}


@router.post("/say")
async def api_voice_say(body: SayBody, request: Request, client: ClientDep) -> dict[str, Any]:
    """Render `text` with a TTS engine and play it on the robot speaker (interrupts any current speech)."""
    speaker = client.intercom.speaker
    if speaker.mic_holder is not None:  # fail before rendering; begin() re-checks after
        raise SpeakerBusyError(speaker.mic_holder)  # 409 SPEAKER_BUSY
    try:
        engine = get_engine(body.engine or None)
    except KeyError as exc:
        names = [e.name for e in engines()]
        raise voice_error("UNKNOWN_ENGINE", str(exc).strip("'\""), hint=f"Use one of: {', '.join(names) or 'none'} "
                          "(GET /api/voice/engines), or omit `engine` for the default.", engines=names) from None
    if body.preset and body.preset not in engine.presets:
        raise voice_error("UNKNOWN_PRESET", f"Engine {engine.name!r} has no voice preset {body.preset!r}.",
                          hint=f"Presets: {', '.join(engine.presets)} (or omit `preset`).", presets=list(engine.presets))
    text, emoji_emotion = split_emoji(body.text.strip())
    warnings: list[str] = []
    emotion = (body.emotion or emoji_emotion or "neutral").strip()
    if emotion not in engine.emotions:
        warnings.append(f"Engine {engine.name!r} has no {emotion!r} voice; spoke neutral.")
        emotion = "neutral"
    babble = body.babble or not text
    t0 = time.perf_counter()
    try:
        utt = await asyncio.to_thread(engine.synthesize, None if babble else text, emotion=emotion, babble=babble,
                                      preset=body.preset or None)
    except ValueError as exc:
        raise voice_error("BAD_TEXT", str(exc) or "The engine cannot speak this text.",
                          hint="Give some words, or use babble.") from exc
    except Exception as exc:
        logger.warning("TTS engine %s failed", engine.name, exc_info=True)
        raise voice_error("TTS_FAILED", f"TTS engine {engine.name!r} failed: {type(exc).__name__}: {exc}",
                          hint="Try the built-in `chirp` engine, or check the cockpit log.", engine=engine.name) from exc
    if body.volume != 1.0:
        utt = utt.scaled(body.volume)
    render_ms = round((time.perf_counter() - t0) * 1000)
    if not client.connected:
        client = request.app.state.manager.require_client()  # link dropped while rendering -> 503
    turn = speaker.begin(OWNER, label=utt.text or f"({utt.emotion} babble)", duration_s=utt.duration)
    task = asyncio.create_task(_play(client, turn, utt), name="nebi-cockpit-voice")
    tasks = _tasks(request)
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    res: dict[str, Any] = {"ok": True, "spoken": utt.text, "mode": utt.mode, "emotion": utt.emotion, "engine": engine.name,
                           "duration_s": round(utt.duration, 2), "render_ms": render_ms, "turn_id": turn.id}
    if warnings:
        res["warnings"] = warnings
    if body.wait:
        res.update(await asyncio.shield(task))
    return res


@router.post("/stop")
async def api_voice_stop(manager: ManagerDep) -> dict[str, Any]:
    """Silence whatever Nebi is saying (cockpit speech or `nebi say`). No-op when quiet."""
    client = manager.client
    stopped = client.intercom.speaker.stop(by="cockpit stop") if client is not None else None
    return {"ok": True, "stopped": stopped is not None, "turn": stopped.info() if stopped is not None else None}
