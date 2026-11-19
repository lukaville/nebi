"""High-level domain controllers for `AsyncNebiClient` (all derive from `DomainController`)."""

from nebi.domains._base import DomainController
from nebi.domains.audio import AudioController
from nebi.domains.camera import CameraController
from nebi.domains.games import GamesController
from nebi.domains.led import LedController
from nebi.domains.media import MediaController
from nebi.domains.motion import MotionController
from nebi.domains.system import SystemController
from nebi.domains.tracking import TrackingController
from nebi.domains.wifi import WifiController

__all__ = [
    "AudioController",
    "CameraController",
    "DomainController",
    "GamesController",
    "LedController",
    "MediaController",
    "MotionController",
    "SystemController",
    "TrackingController",
    "WifiController",
]
