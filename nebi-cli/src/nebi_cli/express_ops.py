"""Eyes, sounds and emotes: the Expression service (automatic expressions) and the eyes/sound/emote commands.

The eyes are driven directly (nebi `robot.eyes`: solid colours and animated expressions streamed as LED
frames). They never move the body, so automatic expressions (emote, body language, idle life, the
voice pulse in `say`) show eyes alongside body moves. When frames stop the robot's own (firmware) eyes come back
by themselves; `nebi eyes release` does that on purpose.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from nebi.domains.audio import SOUND_EFFECT_CATALOG
from nebi.domains.led import DEFAULT_HOLD_S, LedController
from nebi_cli import spec as S
from nebi_cli.errors import AgentError, bad_arg
from nebi_cli.registry import Args, CommandGroup, command

if TYPE_CHECKING:
    from nebi.client import AsyncNebiClient
    from nebi_cli.runtime import AgentRuntime

logger = logging.getLogger(__name__)

EYE_EXPRESSIONS = {
    "happy": "happy",
    "wink": "wink",
    "heart": "heart",
    "love": "heart",
    "cool": "cool",
    "sleepy": "sleepy",
    "dizzy": "dizzy",
    "angry": "angry",
    "music": "music_pulse",
    "music_pulse": "music_pulse",
}
COLOR_NAMES = {
    "off": (0, 0, 0, 0),
    "black": (0, 0, 0, 0),
    "white": (0, 0, 0, 255),
    "red": (255, 0, 0, 0),
    "green": (0, 255, 0, 0),
    "blue": (0, 0, 255, 0),
    "yellow": (255, 200, 0, 0),
    "orange": (255, 110, 0, 0),
    "purple": (150, 0, 255, 0),
    "pink": (255, 60, 160, 0),
    "cyan": (0, 220, 255, 0),
    "teal": (0, 200, 140, 0),
}

# Friendly slugs for the built-in sound effect catalog.
SOUND_SLUGS = {
    1: "greet",
    2: "meow",
    3: "chirp",
    4: "shutter",
    5: "victory",
    6: "curious",
    7: "purr",
    8: "horn",
}

# Emote recipes: (eye expression, sound id)
EMOTES: dict[str, tuple[str, int]] = {
    "happy": ("happy", 3),
    "curious": ("cool", 6),
    "love": ("heart", 7),
    "sad": ("sleepy", 0),
    "celebrate": ("music_pulse", 5),
    "greet": ("wink", 1),
}


def _eyes_summary(st: dict[str, Any]) -> str:
    left = "until released" if st.get("until_release") else (
        f"for {st['seconds_left']:g}s" if st.get("seconds_left") is not None else "")
    if st.get("mode") == "color":
        what = "dark (off)" if st.get("color") == "#000000" else f"colour {st.get('color')}"
        return f"eyes: {what} {left}".strip()
    if st.get("mode") == "expression":
        return f"eyes: {st.get('expression')} expression {left}".strip()
    return "eyes: the robot's own (firmware) eyes"


class Expression:
    """Automatic expressions shared by emote, body language and idle life (eyes + low-priority babble)."""

    def __init__(self, rt: AgentRuntime) -> None:
        self._rt = rt

    async def show_eyes(self, c: AsyncNebiClient, expression: str, warnings: list[str], *, loops: int = 2) -> str:
        """Play an eye expression (LED frames only). Returns what was shown."""
        key = EYE_EXPRESSIONS.get(expression, expression)
        await c.eyes.play_expression(key, loops=loops)
        return key

    async def express(self, c: AsyncNebiClient, *, eyes: str | None, voice: str | None, warnings: list[str]) -> dict[str, Any]:
        """Automatic expression (body, idle): short eyes + low-priority babble that never interrupts speech."""
        done: dict[str, Any] = {}
        if eyes:
            try:
                done["eyes"] = await self.show_eyes(c, eyes, warnings)
            except Exception as exc:  # expressions are decoration: report, never fail the caller
                warnings.append(f"eyes: {exc}")
        if voice:
            try:  # TTS babble through the robot speaker (low priority, never interrupts speech)
                r = await self._rt.invoke("say", {"_warnings": warnings, "babble": True, "emotion": voice, "no_wait": True,
                                                  "no_animate": True, "_auto": True})
                if not r.get("skipped"):
                    done["voice"] = r.get("spoken")
            except Exception as exc:
                warnings.append(f"voice: {exc}")
        return done


class ExpressCommands(CommandGroup):
    @command("eyes")
    async def eyes(self, a: Args) -> dict[str, Any]:
        what = str(a["what"]).strip().lower()
        if what in ("list", "help"):
            c = self.connected_client()
            return {
                "expressions": {e["name"]: e["description"] for e in LedController.list_expressions()},
                "aliases": {k: v for k, v in EYE_EXPRESSIONS.items() if k != v},
                "colors": [*COLOR_NAMES, "#rrggbb[ww]"],
                "release": "`nebi eyes release` gives the eyes back to the robot (its own idle look)",
                "status": c.eyes.eyes_status() if c is not None else None,
                "summary": "solid colours (held for --hold seconds or --until-release) and animated expressions "
                "(--repeat loops, 0 = until release); `off` holds the eyes dark, `release` returns them to the robot",
            }
        c = self.client()
        if what == "status":
            st = c.eyes.eyes_status()
            return {**st, "summary": _eyes_summary(st)}
        if what in ("release", "auto", "default", "firmware"):
            st = await c.eyes.release()
            return {**st, "summary": "eyes released: the robot shows its own eyes again"}
        b_raw = a.get("brightness")
        brightness = 1.0 if b_raw is None else float(b_raw)
        until_release = bool(a.get("until_release"))
        if what in EYE_EXPRESSIONS:
            key = EYE_EXPRESSIONS[what]
            repeat = a.get("repeat")
            loops = 0 if until_release else int(3 if repeat is None else repeat)
            st = await c.eyes.play_expression(key, loops=loops, brightness=brightness)
            return {**st, "summary": _eyes_summary(st)}
        rgbw = COLOR_NAMES.get(what)
        if rgbw is None:
            m = re.fullmatch(r"#?([0-9a-f]{6})([0-9a-f]{2})?", what)
            if not m:
                raise bad_arg(
                    f"Unknown expression/color {what!r}.",
                    hint=f"Expressions: {', '.join(sorted(set(EYE_EXPRESSIONS)))}; colors: #rrggbb or "
                    f"{', '.join(COLOR_NAMES)}; `release` gives the eyes back to the robot.",
                )
            h = m.group(1)
            rgbw = (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), int(m.group(2) or "00", 16))
        h_raw = a.get("hold")
        hold = None if until_release else (DEFAULT_HOLD_S if h_raw is None else float(h_raw))
        st = await c.eyes.set_color(*rgbw, hold_sec=hold, brightness=brightness)
        return {**st, "summary": _eyes_summary(st)}

    @command("sound")
    async def sound(self, a: Args) -> dict[str, Any]:
        name = (a.get("name") or "").strip().lower()
        catalog = [{"id": s["id"], "name": SOUND_SLUGS.get(s["id"], str(s["id"])), "title": s["name"]} for s in SOUND_EFFECT_CATALOG]
        if not name:
            return {"sounds": catalog, "note": "Play with `nebi sound <name|id>` or an on-robot path `nebi sound /md/etc/...wav`."}
        c = self.client()
        if name.startswith("/"):
            r = await c.audio.play_audio_file(name)
            return {"path": name, **r, "summary": f"played {name}"}
        sid = None
        if name.isdigit():
            sid = int(name)
        else:
            for s in catalog:
                if name == s["name"] or name in s["title"].lower():
                    sid = s["id"]
                    break
        if sid is None or sid not in {s["id"] for s in catalog}:
            raise AgentError("UNKNOWN_SOUND", f"No sound {name!r}.", hint="`nebi sound` lists available sounds.", exit_code=S.EXIT_USAGE)
        r = await c.audio.play_sound_effect(sid)
        return {"sound": SOUND_SLUGS.get(sid), **r, "summary": f"played the {SOUND_SLUGS.get(sid, sid)} sound"}

    @command("emote")
    async def emote(self, a: Args) -> dict[str, Any]:
        c = self.client()
        emotion = a["emotion"]
        eyes, sound = EMOTES[emotion]
        warnings: list[str] = a["_warnings"]
        done: dict[str, Any] = {"emotion": emotion}
        try:
            done["eyes"] = await self.rt.expression.show_eyes(c, eyes, warnings)
        except Exception as exc:
            warnings.append(f"eyes failed: {exc}")
        voice_emotion = {"celebrate": "excited", "greet": "happy"}.get(emotion, emotion)
        said = None
        try:
            said = await self.rt.invoke("say", {"_warnings": warnings, "babble": True, "emotion": voice_emotion,
                                                "no_wait": True, "no_animate": True})
            done["voice"] = said.get("spoken")
        except AgentError:  # no TTS / not connected: fall back to a built-in sound below
            said = None
        if said is None and sound:
            try:
                await c.audio.play_sound_effect(sound)
                done["sound"] = SOUND_SLUGS.get(sound)
            except Exception as exc:
                warnings.append(f"sound failed: {exc}")
        parts = [p for p in (f"eyes {done['eyes']}" if "eyes" in done else "",
                             f"voice \"{done['voice']}\"" if done.get("voice") else "",
                             f"sound {done['sound']}" if "sound" in done else "") if p]
        done["summary"] = f"emoted {emotion}" + (f" ({', '.join(parts)})" if parts else " (nothing could play)")
        return done
