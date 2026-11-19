"""nebi-ai: DeepAgents-powered autonomous pet robot agent and web UI for Nebi."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _version

from nebi_ai.agent import PetAgentSession, TurnRecord, build_pet_agent, build_system_prompt
from nebi_ai.bridge import RobotBridge, RobotTarget
from nebi_ai.config import DEFAULT_MODEL, MemoryStore, PetConfig, default_home_dir

try:
    __version__ = _version("nebi-ai")  # pyproject.toml is the single source
except PackageNotFoundError:  # running from a source tree that was never installed
    __version__ = "0+unknown"

__all__ = [
    "DEFAULT_MODEL",
    "MemoryStore",
    "PetAgentSession",
    "PetConfig",
    "RobotBridge",
    "RobotTarget",
    "TurnRecord",
    "__version__",
    "build_pet_agent",
    "build_system_prompt",
    "default_home_dir",
]
