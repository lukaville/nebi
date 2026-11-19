"""Health (HMS) fault table: fault code -> severity, how it clears, and a short English summary.

A fault code is `0xF0MMNNNN`: `MM` is the reporting subsystem (see `SUBSYSTEMS`), `NNNN` the fault within it.
The robot sends the code and an instance number (which motor, which firmware component, ...); everything else
comes from this table. Unknown codes fall back to a generic summary for their subsystem.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# How a fault goes away (`recovery`).
INFO = "info"        # notice only, nothing to do
AUTO = "auto"        # clears by itself once the condition is gone
CONFIRM = "confirm"  # the robot asks the app to acknowledge it
USER = "user"        # the user has to fix the cause

RECOVERY_HINTS: dict[str, str] = {
    INFO: "Informational: nothing to do.",
    AUTO: "Clears by itself once the condition recovers; restart the robot if it persists.",
    CONFIRM: "Acknowledge it to clear it (cockpit Clear / `nebi faults --clear <code>`).",
    USER: "Needs you to fix the cause described in the message, then clear it.",
}

SUBSYSTEMS: dict[int, str] = {
    0x01: "System",
    0x02: "Video link",
    0x03: "Locomotion",
    0x04: "Battery",
    0x08: "Obstacle avoidance",
    0x09: "Camera",
    0x0A: "Firmware",
}


@dataclass(frozen=True)
class FaultSpec:
    severity: str                # "Info" | "Warning" | "Error" | "Fatal"
    recovery: str                # INFO | AUTO | CONFIRM | USER
    summary: str
    parts: dict[int, str] = field(default_factory=dict)  # instance -> affected component, when it matters


_FW_PARTS = {
    512: "hip motor", 513: "knee motor", 514: "wheel motor", 575: "gimbal motor", 768: "main controller",
    1280: "power board", 1793: "remote radio", 1794: "remote", 2560: "battery",
}
_IMU_PARTS = {0: "gimbal", 1: "body"}

FAULTS: dict[int, FaultSpec] = {
    # System, storage and power state
    0xF0010001: FaultSpec("Warning", INFO, "Internal storage is running out; delete or move media."),
    0xF0010002: FaultSpec("Warning", INFO, "SD card is running out of space; delete or move media."),
    0xF0010003: FaultSpec("Error", AUTO, "Storage is mounted over USB; some functions are paused."),
    0xF0010004: FaultSpec("Error", AUTO, "Charging cable plugged in; standing up is blocked."),
    0xF0010005: FaultSpec("Warning", AUTO, "Battery under 25%; jumps are restricted."),
    0xF0010006: FaultSpec("Warning", AUTO, "Battery under 10%; driving is restricted."),
    0xF0010007: FaultSpec("Error", USER, "Recording stopped: no free storage."),
    0xF0010008: FaultSpec("Error", AUTO, "Recording stopped: SD card error."),
    0xF0010009: FaultSpec("Error", INFO, "Robot is powering off."),
    0xF0010010: FaultSpec("Error", AUTO, "Cooling fan fault; restart the robot."),
    0xF0010011: FaultSpec("Error", AUTO, "Robot is too hot; let it cool down."),
    0xF0010012: FaultSpec("Warning", AUTO, "Log export in progress."),
    # Video link
    0xF0020001: FaultSpec("Warning", AUTO, "Video link is weak; move closer."),
    0xF0020002: FaultSpec("Warning", AUTO, "Video link interference; move closer or clear the line of sight."),
    0xF0020003: FaultSpec("Error", AUTO, "Video link failed; restart the robot."),
    # Locomotion: sensors, motors, posture
    0xF0030101: FaultSpec("Fatal", INFO, "Orientation sensor fault.", _IMU_PARTS),
    0xF0030102: FaultSpec("Fatal", INFO, "Orientation sensor fault.", _IMU_PARTS),
    0xF0030201: FaultSpec("Fatal", INFO, "Motor fault."),
    0xF0030202: FaultSpec("Error", INFO, "Motor temperature high; some functions are limited."),
    0xF0030203: FaultSpec("Error", INFO, "Joint is blocked.", {6: "gimbal"}),
    0xF0030204: FaultSpec("Fatal", INFO, "Motor fault."),
    0xF0030205: FaultSpec("Fatal", INFO, "Joint is blocked."),
    0xF0030206: FaultSpec("Fatal", INFO, "Joint is blocked."),
    0xF0030401: FaultSpec("Info", INFO, "Robot is tilted."),
    0xF0030402: FaultSpec("Info", INFO, "Drive command was interrupted."),
    0xF0030403: FaultSpec("Warning", INFO, "Poor traction; drive on flat, dry ground with suitable wheels."),
    0xF0031001: FaultSpec("Fatal", CONFIRM, "Gimbal could not return home; check for obstructions."),
    0xF0031002: FaultSpec("Fatal", USER, "Legs could not return home; check for obstructions."),
    0xF0031003: FaultSpec("Fatal", USER, "Stand-up failed; fold the robot fully and free the legs."),
    0xF0031004: FaultSpec("Error", INFO, "Wheel problem; inspect the wheels."),
    0xF0031005: FaultSpec("Fatal", USER, "Robot is not activated."),
    0xF0031006: FaultSpec("Warning", INFO, "Payload is too heavy."),
    0xF0031007: FaultSpec("Error", CONFIRM, "Legs could not return home; fold the robot on flat ground and retry."),
    # Battery
    0xF0040001: FaultSpec("Error", AUTO, "Battery voltage too high; unplug the charger."),
    0xF0040002: FaultSpec("Error", AUTO, "Battery voltage low; charge soon."),
    0xF0040003: FaultSpec("Error", AUTO, "Battery current too high; reduce the load."),
    0xF0040004: FaultSpec("Error", AUTO, "Battery too hot; let it cool down."),
    0xF0040005: FaultSpec("Warning", AUTO, "Battery is cold; performance may drop."),
    0xF0040006: FaultSpec("Error", AUTO, "Battery short-circuit protection tripped."),
    0xF0040007: FaultSpec("Error", AUTO, "Lost contact with the battery controller; restart the robot."),
    0xF0040008: FaultSpec("Error", AUTO, "Lost contact with the charger chip; restart the robot."),
    0xF0040009: FaultSpec("Warning", INFO, "Battery is worn; replace it soon."),
    0xF004000A: FaultSpec("Error", USER, "Battery has failed permanently; stop using it."),
    # Obstacle avoidance and perception
    0xF0080001: FaultSpec("Warning", AUTO, "Obstacle avoidance is less reliable at this speed."),
    0xF0080002: FaultSpec("Warning", AUTO, "Obstacle avoidance is less reliable in low light."),
    0xF0080003: FaultSpec("Error", AUTO, "Obstacle avoidance is not working; restart the robot."),
    0xF0080004: FaultSpec("Warning", AUTO, "Bump detected; check the robot and its surroundings."),
    0xF0080005: FaultSpec("Warning", AUTO, "Too dark for automatic stair climbing."),
    0xF0080006: FaultSpec("Warning", AUTO, "Obstacle avoidance is off: depth camera problem."),
    0xF0080007: FaultSpec("Warning", AUTO, "Obstacle avoidance is limited while the robot is pitched."),
    0xF008000C: FaultSpec("Error", INFO, "Obstacle avoidance is restricting driving (grass?); it can be turned off."),
    # Main camera
    0xF0090001: FaultSpec("Error", AUTO, "Main camera fault."),
    0xF0090002: FaultSpec("Warning", AUTO, "Lens may be dirty or fogged."),
    0xF0090003: FaultSpec("Info", AUTO, "Lens dirt check is unavailable right now."),
    0xF0090004: FaultSpec("Warning", AUTO, "Camera calibration data missing; image quality may suffer."),
    # Firmware consistency
    0xF00A0001: FaultSpec("Warning", CONFIRM, "Firmware versions do not match; update the firmware.", _FW_PARTS),
}


def fault_summary(code: int, instance: int) -> tuple[str, str, str]:
    """(severity, recovery, English message) for a fault code and instance."""
    spec = FAULTS.get(code & 0xFFFFFFFF)
    if spec is None:
        area = SUBSYSTEMS.get((code >> 16) & 0xFF)
        return "Warning", "", f"{area} fault 0x{code & 0xFFFFFFFF:08X}" if area else f"Unknown fault 0x{code & 0xFFFFFFFF:08X}"
    part = spec.parts.get(instance)
    return spec.severity, spec.recovery, f"{spec.summary} ({part})" if part else spec.summary
