"""Tunable robot parameters shown in the parameter editor.

`key` is the name the robot's config service uses; `param_id` (when set) is the number for the Zenoh
`/cmd/parameter/query_req` / `update_req` fallback. `default` is what nebi reports when the robot does not answer.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ParamSpec:
    key: str                     # "md_function.max_speed"
    label: str                   # short English label
    default: float
    unit: str = ""               # display unit ("m/s", "deg/s", ...)
    param_id: int | None = None  # Zenoh parameter number; None = config service only
    integer: bool = False        # whole numbers only

    @property
    def group(self) -> str:
        return self.key.split(".", 1)[0]


PARAMS: tuple[ParamSpec, ...] = (
    ParamSpec("md_function.max_speed", "Top speed", 2.0, "m/s", param_id=1),
    ParamSpec("md_function.max_yaw_rate", "Top turn rate", 90.0, "deg/s", param_id=2),
    ParamSpec("md_function.power_response_level", "Throttle response level", 2.0, param_id=3),
    ParamSpec("md_function.circling_linear_velocity", "Orbit speed", 0.5, "m/s", param_id=4),
    ParamSpec("camera.config_index", "Sensor profile index", 0, integer=True),
    ParamSpec("camera.eis_enable", "Stabilisation on", 0, integer=True),
    ParamSpec("camera.eis_level", "Stabilisation level", 0, integer=True),
    ParamSpec("camera.storage_type", "Storage target", 0, integer=True),
    ParamSpec("camera.zoom_data", "Zoom", 0, integer=True),
)
