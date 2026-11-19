"""nebi-patch: hardened firmware patcher, integrity verifier, and OTA uploader compatible with Beni (LZ110/LZ100)."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _version

from nebi_patch.patcher import PatchResult, patch_firmware
from nebi_patch.uploader import (
    DEFAULT_OTA_PORT,
    DEFAULT_UCP_PORT,
    MIN_SAFE_BATTERY_PCT,
    OTA_CMD_CANCEL,
    OTA_CMD_CAPABILITY_PROBE,
    OTA_CMD_CONFIRM_COMPLETE,
    OTA_CMD_INSTALL,
    OTA_CMD_QUERY,
    OTA_CMD_START,
    UploadResult,
    check_robot_ota,
    trigger_zenoh_ota,
    trigger_zenoh_ota_sync,
    upload_firmware,
)
from nebi_patch.validator import (
    PackageVerificationReport,
    SafetyValidationError,
    verify_package,
)

try:
    __version__ = _version("nebi-patch")  # pyproject.toml is the single source
except PackageNotFoundError:  # running from a source tree that was never installed
    __version__ = "0+unknown"

__all__ = [
    "DEFAULT_OTA_PORT",
    "DEFAULT_UCP_PORT",
    "MIN_SAFE_BATTERY_PCT",
    "OTA_CMD_CANCEL",
    "OTA_CMD_CAPABILITY_PROBE",
    "OTA_CMD_CONFIRM_COMPLETE",
    "OTA_CMD_INSTALL",
    "OTA_CMD_QUERY",
    "OTA_CMD_START",
    "PackageVerificationReport",
    "PatchResult",
    "SafetyValidationError",
    "UploadResult",
    "__version__",
    "check_robot_ota",
    "patch_firmware",
    "trigger_zenoh_ota",
    "trigger_zenoh_ota_sync",
    "upload_firmware",
    "verify_package",
]
