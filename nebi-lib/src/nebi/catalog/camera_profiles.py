"""Camera recording profiles selectable with `camera.set_profile(id)`.

Ids are the robot's profile numbers; resolution, frame rate and aspect ratio describe the output. Id 40 is unused.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CameraProfileSpec:
    id: int
    resolution: str      # "4K", "3K", "2.5K", "2K", "1080p"
    fps: int
    aspect: str = "16:9"
    note: str = ""       # e.g. "HDR", "slow motion"

    @property
    def label(self) -> str:
        extra = f", {self.note}" if self.note else ""
        return f"{self.resolution} {self.fps}fps ({self.aspect}{extra}) #{self.id}"


def _p(pid: int, resolution: str, fps: int, aspect: str = "16:9", note: str = "") -> CameraProfileSpec:
    return CameraProfileSpec(pid, resolution, fps, aspect, note)


_FPS_STEPS = (24, 25, 30, 48, 50)

CAMERA_PROFILES: tuple[CameraProfileSpec, ...] = (
    *(_p(i, "4K", fps) for i, fps in enumerate((24, 25, 30, 48, 50, 60))),
    *(_p(6 + i, "3K", fps) for i, fps in enumerate(_FPS_STEPS)),
    _p(11, "2.5K", 60),
    *(_p(12 + i, "1080p", fps) for i, fps in enumerate((24, 25, 30, 48, 50, 60, 100))),
    *(_p(19 + i, "3K", fps, "9:16") for i, fps in enumerate(_FPS_STEPS)),
    _p(24, "1080p", 60, "9:16"),
    *(_p(25 + i, "1080p", fps, "9:16") for i, fps in enumerate((24, 25, 30, 48, 50, 60))),
    *(_p(31 + i, "4K", fps, "4:3") for i, fps in enumerate((24, 25, 30))),
    *(_p(34 + i, "1080p", fps, "4:3") for i, fps in enumerate((24, 25, 30, 48, 50, 60))),
    _p(41, "4K", 30, "9:16"),
    _p(42, "2K", 60, note="slow motion"),
    _p(43, "2K", 60),
    _p(44, "4K", 30, note="HDR"),
    _p(45, "3K", 60),
    _p(46, "1080p", 30, note="default"),
)
