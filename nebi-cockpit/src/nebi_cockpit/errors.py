"""Uniform JSON error responses for the cockpit API.

Every error body has `ok: false` and a human-readable `detail`; robot/link errors (`NebiError`) add the
structured `error` dict and the current `connection` summary so the UI can update its banner.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from nebi.exceptions import (
    NebiBleError,
    NebiError,
    NebiHandshakeError,
    NebiLinkLostError,
    NebiNotConnectedError,
    NebiRpcTimeoutError,
)

_CONFLICT_CODES = frozenset(
    {"ROBOT_CHARGING_LOCKED", "LINK_OP_CANCELLED", "SPEAKER_BUSY", "EGO_MODE_NEEDS_STANDING", "HMS_NO_PENDING_ACTION", "CAMERA_NOT_RECORDING"}
)
# The robot/firmware cannot do this at all (not a link problem): 501 Not Implemented.
_UNSUPPORTED_CODES = frozenset(
    {"CAMERA_PHOTO_UNSUPPORTED", "CAMERA_HORIZON_LOCK_UNSUPPORTED"}
)
_BAD_REQUEST_CODES = frozenset(
    {
        "UNKNOWN_ENGINE", "UNKNOWN_PRESET", "BAD_TEXT",  # /api/voice
        "LED_BAD_COLOR", "LED_BAD_HOLD", "LED_BAD_LOOPS", "LED_BAD_BRIGHTNESS", "LED_UNKNOWN_EXPRESSION",  # /api/eyes
    }
)


def http_status_for_nebi_error(err: NebiError) -> int:
    """HTTP status for a `NebiError`: 503 no link, 504 RPC timeout, 409 needs user action, 400 bad voice/eye args,
    501 not supported by the robot, else 502."""
    if isinstance(err, (NebiNotConnectedError, NebiLinkLostError)):
        return 503
    if isinstance(err, NebiRpcTimeoutError):
        return 504
    if err.code in _CONFLICT_CODES:
        return 409
    if err.code in _BAD_REQUEST_CODES:
        return 400
    if err.code in _UNSUPPORTED_CODES:
        return 501
    if isinstance(err, NebiBleError) and err.code == "BLE_AUTH_REQUIRED":
        return 409
    if isinstance(err, NebiHandshakeError) and err.code == "ZENOH_AUTH_REQUIRED":
        return 409
    return 502


def install_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(NebiError)
    async def handle_nebi_error(request: Request, err: NebiError) -> JSONResponse:
        manager = getattr(request.app.state, "manager", None)
        return JSONResponse(
            status_code=http_status_for_nebi_error(err),
            content={
                "ok": False,
                "detail": err.message,
                "error": err.to_dict(),
                "connection": manager.connection_summary() if manager is not None else None,
            },
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        # The default handler echoes the offending input; NaN/Infinity can't be serialized as JSON.
        errors = [{k: (repr(v) if k == "input" else v) for k, v in e.items() if k != "ctx"} for e in exc.errors()]
        return JSONResponse(status_code=422, content={"ok": False, "detail": errors})
