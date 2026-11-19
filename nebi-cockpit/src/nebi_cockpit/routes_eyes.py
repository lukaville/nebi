"""Eye LEDs (`/api/eyes/*`, the cockpit "Eyes" card on the Audio tab).

The eyes show the robot's own (firmware) look until an app streams LED frames; the cockpit can then show a solid
colour or an animated expression, for a time or until released. Releasing just stops the frames and the firmware
takes the eyes back by itself a moment later.

* `GET  /api/eyes`             -> `{status, expressions, presets, connected}` (works while disconnected)
* `POST /api/eyes/color`       `{r, g, b, w?, hex?, hold_sec?: number|null, brightness?}`; `hold_sec: null` holds
                                until `/api/eyes/release`
* `POST /api/eyes/expression`  `{name, loops?, brightness?}`; `loops: 0` repeats until release
* `POST /api/eyes/release`     back to the firmware eyes

Errors are the cockpit's usual JSON (`ok: false`, `detail`, `error: {code, message, hint, ...}`); bad arguments
are 400 with `LED_*` codes.
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter
from pydantic import Field

from nebi.domains.led import DEFAULT_HOLD_S, LedController, led_error
from nebi_cockpit.deps import ClientDep, ManagerDep
from nebi_cockpit.schemas import _Body

router = APIRouter(prefix="/api/eyes", tags=["eyes"])

# Swatches shown in the cockpit (name, hex).
PRESETS: list[dict[str, str]] = [
    {"name": "White", "hex": "#ffffff"},
    {"name": "Red", "hex": "#ff2a1a"},
    {"name": "Orange", "hex": "#ff7a00"},
    {"name": "Yellow", "hex": "#ffd000"},
    {"name": "Green", "hex": "#20e050"},
    {"name": "Cyan", "hex": "#00d2ff"},
    {"name": "Blue", "hex": "#0040ff"},
    {"name": "Purple", "hex": "#8a2cff"},
    {"name": "Pink", "hex": "#ff2d92"},
    {"name": "Off", "hex": "#000000"},
]


class EyesColorBody(_Body):
    r: int = 0
    g: int = 210
    b: int = 255
    w: int = 0
    hex: str | None = None  # "#rrggbb" or "#rrggbbww"; overrides r/g/b/w when given
    hold_sec: float | None = DEFAULT_HOLD_S  # null: until release
    brightness: float = 1.0


class EyesExpressionBody(_Body):
    name: str = "happy"
    loops: int = Field(2, ge=0)
    brightness: float = 1.0


def _rgbw(body: EyesColorBody) -> tuple[int, int, int, int]:
    if body.hex is None:
        return body.r, body.g, body.b, body.w
    m = re.fullmatch(r"#?([0-9a-fA-F]{6})([0-9a-fA-F]{2})?", body.hex.strip())
    if not m:
        raise led_error("LED_BAD_COLOR", f"Colour {body.hex!r} is not a hex colour.", "Use #rrggbb or #rrggbbww.",
                        hex=body.hex)
    h = m.group(1)
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), int(m.group(2) or "00", 16)


@router.get("")
async def api_eyes(manager: ManagerDep) -> dict[str, Any]:
    c = manager.client
    connected = c is not None and c.connected
    return {
        "ok": True,
        "connected": connected,
        "status": c.system.eyes_status() if c is not None and connected else {"mode": "firmware"},
        "expressions": LedController.list_expressions(),
        "presets": PRESETS,
    }


@router.post("/color")
async def api_eyes_color(body: EyesColorBody, client: ClientDep) -> dict[str, Any]:
    return await client.system.set_color(*_rgbw(body), hold_sec=body.hold_sec, brightness=body.brightness)


@router.post("/expression")
async def api_eyes_expression(body: EyesExpressionBody, client: ClientDep) -> dict[str, Any]:
    return await client.system.play_expression(body.name, loops=body.loops, brightness=body.brightness)


@router.post("/release")
async def api_eyes_release(client: ClientDep) -> dict[str, Any]:
    return await client.system.release()
