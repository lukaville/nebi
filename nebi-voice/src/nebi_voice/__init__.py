"""nebi-voice: small-robot text-to-speech on top of the tiny-voca ONNX model."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _version

from nebi_voice.effects import to_pcm16
from nebi_voice.emotion import EMOTIONS
from nebi_voice.engine import SAMPLE_RATE, ModelUnavailableError, TinyVoca, default_model_dir, ensure_model, find_model
from nebi_voice.voice import PRESETS, RobotVoice, Utterance, VoiceParams, play_audio, speak

try:
    __version__ = _version("nebi-voice")  # pyproject.toml is the single source
except PackageNotFoundError:  # running from a source tree that was never installed
    __version__ = "0+unknown"
__all__ = [
    "EMOTIONS",
    "PRESETS",
    "SAMPLE_RATE",
    "ModelUnavailableError",
    "RobotVoice",
    "TinyVoca",
    "Utterance",
    "VoiceParams",
    "default_model_dir",
    "ensure_model",
    "find_model",
    "play_audio",
    "speak",
    "to_pcm16",
]
