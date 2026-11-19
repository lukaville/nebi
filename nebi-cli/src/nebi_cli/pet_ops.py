"""Pet commands: body language, faces (meet/who), memory (note/recall), self-care.

Same rules as the rest of the CLI:
  * every reply has a one-line `summary` an LLM can quote or reason over, plus structured fields;
  * errors carry `code`, `message`, `hint` (the next command to try);
  * body language only moves in place and always returns the body to neutral.

Camera, microphone, voice, eyes and motion come from the robot link this daemon owns; extensions can add
people sensing (look-at bearings), look-at target locators and `care` fields (agent/extensions.py).
Hearing/voice live in voice_ops.py.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nebi.types import DEFAULT_BODY_HEIGHT
from nebi_cli import spec as S
from nebi_cli.errors import STANDING, AgentError, bad_arg
from nebi_cli.motion import done_after
from nebi_cli.perceive_ops import fresh_frame
from nebi_cli.registry import Args, CommandGroup, command
from nebi_cli.settings import CONFIG_DIR
from nebi_cli.util import where_in_image

if TYPE_CHECKING:
    from nebi.client import AsyncNebiClient
    from nebi_cli.runtime import AgentRuntime

__all__ = ["FACES_FILE", "MEMORY_FILE", "PetCommands", "look_at"]

logger = logging.getLogger(__name__)

# Read at call time (not bound at import) so tests can point them at a temp dir.
MEMORY_FILE = CONFIG_DIR / "memory.json"
FACES_FILE = CONFIG_DIR / "faces.json"
MEMORY_KEEP = 2000
FACE_SAMPLES_KEEP = 12

MODELS_DIR = Path.home() / ".cache" / "pynebi" / "models"
YUNET = "face_detection_yunet_2023mar.onnx"
SFACE = "face_recognition_sface_2021dec.onnx"
MODEL_URL = "https://github.com/opencv/opencv_zoo/raw/main/models/"
SFACE_MATCH = 0.363  # cosine threshold from the SFace paper / OpenCV sample
CAMERA_HFOV_DEG = 70.0
FACING_TOLERANCE_DEG = 8.0
LOOK_AT_PEOPLE = ("person", "people", "me", "human", "pet", "dog")

BODY_GESTURES = S.BODY_CHOICES
# gesture -> (eye expression, babble emotion) played alongside unless --silent
GESTURE_EXPRESSION = {"nod": ("happy", "happy"), "shake": ("cool", "uh-oh"), "perk": ("happy", "curious"),
                      "cower": ("sleepy", "scared"), "curious": ("cool", "curious"), "wiggle": ("happy", "excited")}
GESTURE_WORDS = {"nod": "nodded yes", "shake": "shook its head no", "perk": "perked up tall", "cower": "cowered low",
                 "wiggle": "wiggled happily", "curious": "tilted its head curiously"}


def gesture_steps(g: str, k: float) -> list[tuple[dict[str, float], float]]:
    """In-place stick sequence (axes, seconds) for a body gesture at intensity k."""
    return {
        "nod": [({"ry": 0.8 * k}, 0.22), ({"ry": -0.8 * k}, 0.22), ({"ry": 0.8 * k}, 0.22), ({"ry": -0.8 * k}, 0.22)],
        "shake": [({"rx": 0.55 * k}, 0.16), ({"rx": -0.55 * k}, 0.32), ({"rx": 0.55 * k}, 0.32), ({"rx": -0.55 * k}, 0.16)],
        "perk": [({"height": 1.0, "ry": 0.5 * k}, 0.45), ({"height": 1.0}, 0.9)],
        "cower": [({"height": 0.1, "ry": -0.6 * k}, 0.6), ({"height": 0.1}, 1.4)],
        "wiggle": [({"roll": 0.8 * k}, 0.2), ({"roll": -0.8 * k}, 0.2)] * 3,
        "curious": [({"roll": 0.7 * k, "ry": 0.25 * k}, 0.5), ({"roll": 0.7 * k}, 1.0)],
    }[g]


def _load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def _save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False))
    tmp.replace(path)


def _download_atomic(url: str, dest: Path) -> None:
    """Download to a temp file next to `dest`, then rename (never leaves a partial model file)."""
    import urllib.request

    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=dest.name + ".", suffix=".part", dir=dest.parent)
    os.close(fd)
    try:
        urllib.request.urlretrieve(url, tmp)
        if os.path.getsize(tmp) == 0:
            raise OSError(f"empty download from {url}")
        os.replace(tmp, dest)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _load_face_models() -> tuple[Any, Any]:
    """Worker thread: fetch (once) and load YuNet + SFace."""
    import cv2  # optional heavy dep (faces only)

    for name, sub in ((YUNET, "face_detection_yunet"), (SFACE, "face_recognition_sface")):
        p = MODELS_DIR / name
        if not p.exists():
            _download_atomic(f"{MODEL_URL}{sub}/{name}", p)
    det = cv2.FaceDetectorYN.create(str(MODELS_DIR / YUNET), "", (320, 320), 0.8, 0.3, 50)
    rec = cv2.FaceRecognizerSF.create(str(MODELS_DIR / SFACE), "")
    return det, rec


def _detect_faces(jpeg: bytes, det: Any, rec: Any) -> list[dict[str, Any]] | None:
    """Worker thread: decode + detect + embed. None when the frame cannot be decoded."""
    import cv2  # optional heavy dep (faces only)
    import numpy as np

    img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return None
    h, w = img.shape[:2]
    det.setInputSize((w, h))
    _, faces = det.detect(img)
    out = []
    for f in (faces if faces is not None else []):
        emb = rec.feature(rec.alignCrop(img, f)).flatten()
        cx = (f[0] + f[2] / 2) / w
        out.append({"box": [round(float(v) / s, 3) for v, s in zip(f[:4], (w, h, w, h), strict=True)], "size": round(float(f[2]) / w, 3),
                    "where": where_in_image(cx), "_emb": emb})
    return out


async def look_at(rt: AgentRuntime, c: AsyncNebiClient, target: str, warnings: list[str], *,
                  nested: dict[str, Any] | None = None, source: str = "") -> dict[str, Any]:
    """Turn in place to face the nearest person (extension people sensing, else the first camera target) or a target
    an extension can locate (AgentExtensionContext.add_target_locator)."""
    bearing = None
    label = target
    if target in LOOK_AT_PEOPLE:
        ppl = [p for p in await rt.safety.people_now(1.5) if isinstance(p.get("bearing_deg"), (int, float))]
        if ppl:
            p = min(ppl, key=lambda q: float(q["distance_m"]) if isinstance(q.get("distance_m"), (int, float)) else 99.0)
            bearing, label = float(p["bearing_deg"]), f"{p.get('type', 'person')} {p.get('distance_m', '?')} m away"
        else:
            tg = c.telemetry.targets
            if tg:
                bearing, label = (0.5 - tg[0].cx) * CAMERA_HFOV_DEG, f"{tg[0].label} in camera"
    else:
        for locate in rt.hooks.target_locators:
            found = await locate(target)
            if found is not None:
                bearing, label = float(found[0]), found[1]
                break
    if bearing is None:
        raise AgentError("TARGET_NOT_FOUND", f"Can't see or locate {target!r}.",
                         hint="`nebi targets` lists the people/pets the camera sees (only people/pets can be looked at, "
                         "unless an extension locates other targets).")
    if abs(bearing) < FACING_TOLERANCE_DEG:
        return {"target": label, "turned_deg": 0, "summary": f"already facing {label}"}
    res = await rt.invoke("turn", {**(nested or {}), "_warnings": warnings, "direction": "left" if bearing > 0 else "right",
                                   "degrees": min(180.0, abs(bearing)), "speed": 0.45}, source=source)
    return {"target": label, "turned_deg": res.get("estimated_deg"), "summary": f"turned {abs(bearing):.0f}° to face {label}"}


class PetCommands(CommandGroup):
    def __init__(self, rt: AgentRuntime) -> None:
        super().__init__(rt)
        self._face_models: tuple[Any, Any] | None = None
        self._faces_lock = asyncio.Lock()
        self._memory_lock = asyncio.Lock()  # serialises read-modify-write of memory.json

    # ================================================================== body language

    @command("body")
    async def body(self, a: Args) -> dict[str, Any]:
        c = self.client()
        g = a["gesture"]
        warnings: list[str] = a["_warnings"]
        silent = bool(a.get("silent"))
        expression = self.rt.expression
        nested = {"_warnings": warnings, "force": a.get("force"), "ignore_carried": a.get("ignore_carried")}
        if g == "sleep":
            if not silent:
                await expression.express(c, eyes="sleepy", voice="sleepy", warnings=warnings)
            if c.telemetry.posture_name in STANDING:
                await self.rt.invoke("sit", nested)
            return {"gesture": g, "summary": "yawned and curled up (folded)"}
        if g == "wake":
            if not silent:
                await expression.express(c, eyes="happy", voice="happy", warnings=warnings)
            if c.telemetry.posture_name not in STANDING:
                await self.rt.invoke("stand", nested)
            return {"gesture": g, "summary": f"woke up ({c.telemetry.posture_name.lower()})"}
        if c.telemetry.posture_name not in STANDING and g not in ("nod", "curious"):  # the gimbal still tilts while folded
            raise AgentError("NOT_STANDING", f"`body {g}` needs the robot standing (it is {c.telemetry.posture_name}).",
                             hint="`nebi body wake` or `nebi stand` first.", exit_code=S.EXIT_FAILED)
        if g == "look-at":
            return await look_at(self.rt, c, a.get("target") or "person", warnings, nested=nested)
        k = max(0.4, min(1.5, float(a.get("intensity") or 1.0)))
        extra = GESTURE_EXPRESSION.get(g)
        if extra and not silent:  # eyes are LED frames, not motions: they run alongside the body move
            self.rt.spawn(expression.express(c, eyes=extra[0], voice=extra[1], warnings=[]), name=f"nebi-agent-body-{g}-eyes")
        motion = self.motion
        async with motion.lock:
            base_h = c.motion.joystick.height
            epoch = motion.new_epoch()
            reason = "done"
            try:
                for axes, dur in gesture_steps(g, k):
                    reason, _ = await motion.hold_axes(c, axes, epoch=epoch, until=done_after(dur), max_s=dur + 0.3)
                    if reason != "done":
                        break
            finally:
                c.motion.set_axes(roll=0.0, height=base_h if base_h is not None else DEFAULT_BODY_HEIGHT)
        if g in ("nod", "shake") and reason == "done":
            await self.rt.invoke("tilt", {"_warnings": [], "direction": "center"})
        words = GESTURE_WORDS[g]
        return {"gesture": g, "completed": reason == "done", "summary": words if reason == "done" else f"{words} (interrupted: {reason})"}

    # ================================================================== faces

    async def _models(self) -> tuple[Any, Any]:
        try:
            import cv2  # noqa: F401  (optional heavy dep: fail early with an actionable hint)
        except ImportError as exc:
            raise AgentError("FACES_UNAVAILABLE", "OpenCV is not installed.",
                             hint="pip install opencv-python-headless into the nebi venv") from exc
        if self._face_models is None:
            try:
                self._face_models = await asyncio.to_thread(_load_face_models)
            except OSError as exc:
                raise AgentError("FACE_MODELS_UNAVAILABLE", f"Could not fetch/load the face models: {exc}",
                                 hint=f"Check the network (downloads from {MODEL_URL}) or put {YUNET} and {SFACE} into {MODELS_DIR}.") from exc
        return self._face_models

    async def _faces_in_view(self) -> tuple[list[dict[str, Any]], Any]:
        det, rec = await self._models()
        c = self.client()
        jpeg, _fresh = await fresh_frame(self.manager, c)
        faces = await asyncio.to_thread(_detect_faces, jpeg, det, rec)
        if faces is None:
            raise AgentError("BAD_FRAME", "The latest camera frame could not be decoded.",
                             hint="Retry in a second; `nebi look` to check video.")
        return faces, rec

    @command("meet")
    async def meet(self, a: Args) -> dict[str, Any]:
        if a.get("list"):
            db = await asyncio.to_thread(_load_json, FACES_FILE, {})
            return {"people": {k: len(v) for k, v in db.items()},
                    "summary": f"knows {len(db)} face(s)" + (f": {', '.join(db)}" if db else "")}
        name = (a.get("name") or "").strip()
        if not name:
            raise bad_arg("give a name: `nebi meet Alice` (or --list / --forget NAME)")
        if a.get("forget"):
            async with self._faces_lock:
                db = await asyncio.to_thread(_load_json, FACES_FILE, {})
                existed = db.pop(name, None) is not None
                await asyncio.to_thread(_save_json, FACES_FILE, db)
            return {"forgot": name, "existed": existed, "summary": f"forgot {name}'s face" if existed else f"didn't know {name}"}
        faces, _ = await self._faces_in_view()
        if not faces:
            raise AgentError("NO_FACE", "No face visible to the camera.",
                             hint="Ask the person to face Nebi ~0.5-2 m away; tilt up with `nebi tilt up`.")
        f = max(faces, key=lambda q: q["size"])
        async with self._faces_lock:
            db = await asyncio.to_thread(_load_json, FACES_FILE, {})
            db.setdefault(name, []).append([round(float(v), 5) for v in f["_emb"]])
            db[name] = db[name][-FACE_SAMPLES_KEEP:]
            await asyncio.to_thread(_save_json, FACES_FILE, db)
            samples = len(db[name])
        return {"name": name, "samples": samples, "where": f["where"],
                "summary": f"learned {name}'s face ({samples} sample(s)); more samples from other angles help",
                **({"note": f"{len(faces)} faces visible; used the largest"} if len(faces) > 1 else {})}

    @command("who")
    async def who(self, a: Args) -> dict[str, Any]:
        import numpy as np  # heavy; faces only

        faces, rec = await self._faces_in_view()
        db = await asyncio.to_thread(_load_json, FACES_FILE, {})

        def match_all() -> list[dict[str, Any]]:
            seen = []
            for f in faces:
                best, score = None, 0.0
                for name, embs in db.items():
                    for e in embs:
                        s = float(rec.match(f["_emb"].reshape(1, -1).astype(np.float32), np.asarray(e, np.float32).reshape(1, -1), 0))
                        if s > score:
                            best, score = name, s
                known = best is not None and score >= SFACE_MATCH
                seen.append({"name": best if known else None, "confidence": round(score, 2), "where": f["where"], "size": f["size"]})
            return seen

        seen = await asyncio.to_thread(match_all)
        names = [s["name"] or "someone unknown" for s in seen]
        summary = "no faces in view" if not seen else "sees " + ", ".join(f"{n} ({s['where']})" for n, s in zip(names, seen, strict=True))
        hint = "Introduce an unknown face with `nebi meet <name>`." if any(s["name"] is None for s in seen) else None
        return {"faces": seen, "summary": summary, **({"hint": hint} if hint else {})}

    # ================================================================== memory

    @command("note")
    async def note(self, a: Args) -> dict[str, Any]:
        async with self._memory_lock:
            mem = await asyncio.to_thread(_load_json, MEMORY_FILE, [])
            if a.get("forget"):
                n0 = len(mem)
                mem = [m for m in mem if str(m["id"]) != str(a["forget"])]
                await asyncio.to_thread(_save_json, MEMORY_FILE, mem)
                return {"forgot": a["forget"], "removed": n0 - len(mem),
                        "summary": f"forgot memory #{a['forget']}" if n0 - len(mem) else f"no memory #{a['forget']}"}
            text = (a.get("text") or "").strip()
            if not text:
                raise bad_arg("nothing to remember: `nebi note 'Alice likes the red ball' --about Alice`")
            nid = max((m["id"] for m in mem), default=0) + 1
            item = {"id": nid, "t": time.strftime("%Y-%m-%d %H:%M"), "text": text,
                    "about": a.get("about") or None, "tags": [t.strip() for t in (a.get("tags") or "").split(",") if t.strip()]}
            mem.append(item)
            await asyncio.to_thread(_save_json, MEMORY_FILE, mem[-MEMORY_KEEP:])
            return {"saved": item, "summary": f"remembered #{nid}: {text}"}

    @command("recall")
    async def recall(self, a: Args) -> dict[str, Any]:
        mem = await asyncio.to_thread(_load_json, MEMORY_FILE, [])
        q = (a.get("query") or "").strip().lower()
        limit = int(a.get("limit") or 10)
        if q:
            words = [w for w in re.findall(r"\w+", q) if len(w) > 2] or [q]
            scored = []
            for m in mem:
                hay = " ".join([m["text"], m.get("about") or "", " ".join(m.get("tags") or [])]).lower()
                s = sum(hay.count(w) for w in words) + (3 if q in hay else 0)
                if s:
                    scored.append((s, m["id"], m))
            hits = [m for _, _, m in sorted(scored, key=lambda z: (-z[0], -z[1]))[:limit]]
        else:
            hits = mem[-limit:][::-1]
        return {"memories": hits, "total": len(mem),
                "summary": (f"{len(hits)} memory(ies)" + (f" about '{q}'" if q else " (most recent)")) if hits else
                ("nothing remembered about that" if q else "no memories yet (`nebi note ...`)")}

    # ================================================================== self-care

    @command("care")
    async def care(self, a: Args) -> dict[str, Any]:
        c = self.client()
        t = c.telemetry
        warnings: list[str] = a["_warnings"]
        needs: list[str] = []
        advice: list[str] = []
        bat = t.battery_soc
        if t.charging:
            advice.append("charging, no action needed")
        elif bat is not None and bat <= 15:
            needs.append("charge_now")
            advice.append("battery critical: fold (`nebi sit`) and ask a human to plug in USB-C or `nebi dock`")
        elif bat is not None and bat <= 30:
            needs.append("charge_soon")
            advice.append("battery low: keep activity calm and head for the charger soon")
        faults = [{"code": x.code, "message": getattr(x, "message_en", ""), "severity": getattr(x, "severity", "")} for x in t.hms_alerts]
        if faults:
            needs.append("check_faults")
            advice.append("active faults: see `nebi faults` for recovery hints")
        if self.rt.hooks.carried is not None:
            carried, _why = await self.rt.safety.carried_state()
            if carried:
                advice.append("being carried: stay still")
        if t.link.degraded:
            needs.append("better_wifi")
            advice.append("link is degraded: avoid driving far from the router")
        extra: dict[str, Any] = {}
        for provider in self.rt.hooks.care:
            try:
                contrib = await provider()
            except Exception as exc:  # an extension's care check is a bonus, never a failure
                warnings.append(f"care extension failed: {type(exc).__name__}: {exc}")
                continue
            extra.update(contrib.get("fields") or {})
            needs += [n for n in contrib.get("needs") or [] if n not in needs]
            advice += list(contrib.get("advice") or [])
            warnings += list(contrib.get("warnings") or [])
        mood = "tired" if "charge_now" in needs or "charge_soon" in needs else "uneasy" if needs else "fine"
        return {"battery": bat, "charging": t.charging, "posture": t.posture_name, **extra,
                "faults": faults, "needs": needs, "advice": advice, "mood": mood,
                "summary": f"feeling {mood}: battery {bat}%" + (" (charging)" if t.charging else "")
                + (f", {len(faults)} fault(s)" if faults else "") + (f"; needs: {', '.join(needs)}" if needs else "")}
