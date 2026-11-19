"""Structured error hierarchy for `nebi` across BLE, Split-Socket Wi-Fi, UCP, Zenoh, and Provisioning."""

from __future__ import annotations

import asyncio
from typing import Any

# Python 3.10: asyncio.TimeoutError is its own class (merged into the builtin TimeoutError in 3.11). Timeouts
# derive from both so `except asyncio.TimeoutError` and `except TimeoutError` catch them on every version.
_TIMEOUT_BASES: tuple[type[BaseException], ...] = (
    (TimeoutError,) if asyncio.TimeoutError is TimeoutError else (TimeoutError, asyncio.TimeoutError)
)


class NebiError(Exception):
    """Base structured exception carrying machine-readable error code, connection stage, and recovery hint."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        stage: str = "unknown",
        recoverable: bool = True,
        hint: str = "",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.stage = stage
        self.recoverable = recoverable
        self.hint = hint
        self.details: dict[str, Any] = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "stage": self.stage,
            "recoverable": self.recoverable,
            "hint": self.hint,
            "details": self.details,
        }


class NebiNotConnectedError(NebiError, RuntimeError):
    """Raised when an operation requires an active robot session, or when the session is disconnected.

    Also a `RuntimeError` so code written against the old bare `RuntimeError("... is not open")` keeps working.
    """

    def __init__(
        self,
        message: str = "Robot client is not connected.",
        *,
        stage: str = "disconnected",
        hint: str = "Connect first: Home Wi-Fi LAN IP, Nebi's SoftAP, BLE bootstrap (needed after every power-on), or the Simulator.",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            "NOT_CONNECTED",
            message,
            stage=stage,
            recoverable=True,
            hint=hint,
            details=details,
        )


class NebiDiscoveryError(NebiError):
    """Raised when robot discovery or network interface binding fails."""


class NebiBleError(NebiError):
    """Raised on BLE GATT discovery, 5-second button authorization, or V0 framing errors."""


class NebiHandshakeError(NebiError):
    """Raised when HTTP health probing, UCP `ucp:sync`->`ucp:ack`, or the Zenoh-over-UCP tunnel `0xA4`->`0xA5` fails."""


class NebiLinkLostError(NebiError):
    """Raised when an active UCP/Zenoh link drops (`ucp:leav` or 3.5s heartbeat silence)."""


class NebiIdentityError(NebiError):
    """Raised when nebi's persistent MD-TS identity file cannot be loaded (code `IDENTITY_CORRUPT`)."""


class NebiWifiProvisionError(NebiError):
    """Raised when transactional AP->STA Home Wi-Fi provisioning fails or rolls back via `nm_switch_watchdog`."""


class NebiRpcTimeoutError(NebiError, *_TIMEOUT_BASES):  # type: ignore[misc]
    """Raised when a Zenoh Protobuf request-response call exceeds its timeout.

    Also a `TimeoutError` (== `asyncio.TimeoutError`) so existing `except asyncio.TimeoutError` callers still work.
    """

    def __init__(
        self,
        code: str = "RPC_TIMEOUT",
        message: str = "Robot RPC timed out.",
        **kwargs: Any,
    ) -> None:
        super().__init__(code, message, **kwargs)
