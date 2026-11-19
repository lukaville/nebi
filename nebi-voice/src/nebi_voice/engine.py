"""Thin wrapper around the tiny-voca ONNX text-to-speech model (4.5M params, 24 kHz, source-filter vocoder).

tiny-voca's waveform comes from a pulse-train + noise source shaped by predicted filters (no neural
vocoder). That gives us direct, artifact-free control of the voice source: pitch (log-F0 offset),
pitch range (how much the melody moves) and speed. Those are the knobs a small-robot voice needs.
"""

from __future__ import annotations

import math
import os
import shutil
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

import numpy as np

REPO_ID = "n4ze3m/tiny-voca"
SAMPLE_RATE = 24000
HOP = 256
VOICES = {"F2": 0, "M2": 1}
LN_SEMITONE = math.log(2.0) / 12.0  # tiny-voca pitch_shift is added to natural-log F0

MODEL_SIZE_MB = 27
ENV_MODEL_DIR = "NEBI_VOICE_MODEL_DIR"
_FILES = ["onnx/*", "phonemizer/*", "config.json", "LICENSE", "README.md"]
_download_lock = threading.Lock()


class ModelUnavailableError(RuntimeError):
    """The tiny-voca model is not on disk and could not be downloaded. ``hint`` says what to do."""

    def __init__(self, message: str, hint: str) -> None:
        super().__init__(f"{message} ({hint})")
        self.hint = hint


def _cache_root() -> Path:
    if sys.platform == "win32" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"])
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")


def default_model_dir() -> Path:
    """Where the model is downloaded: $NEBI_VOICE_MODEL_DIR, else <user cache>/nebi-voice/tiny-voca."""
    env = os.environ.get(ENV_MODEL_DIR, "").strip()
    return Path(env).expanduser() if env else _cache_root() / "nebi-voice" / "tiny-voca"


def model_ready(d: Path) -> bool:
    return (d / "onnx" / "model.onnx").is_file() and (d / "phonemizer" / "lexicon.json").is_file()


def find_model(model_dir: str | Path | None = None) -> Path | None:
    """The local tiny-voca directory if it is already there (never downloads)."""
    if model_dir is not None:
        return Path(model_dir) if model_ready(Path(model_dir)) else None
    candidates = [default_model_dir()]
    if not os.environ.get(ENV_MODEL_DIR, "").strip():
        candidates += [
            Path(__file__).resolve().parents[2] / "models" / "tiny-voca",  # editable monorepo checkout
            Path.home() / ".cache" / "beni-voice" / "tiny-voca",  # cache of the package's former name
        ]
    return next((d for d in candidates if model_ready(d)), None)


def ensure_model(model_dir: str | Path | None = None, *, download: bool = True) -> Path:
    """Return the local tiny-voca directory, downloading ONNX + phonemizer data (~27 MB) on first use.

    Downloads go to a ``.partial`` sibling and are renamed into place only when complete, so an interrupted
    download is never mistaken for a model. Raises `ModelUnavailableError` (with a hint) when it cannot.
    """
    found = find_model(model_dir)
    if found is not None:
        return found
    d = Path(model_dir) if model_dir is not None else default_model_dir()
    if not download:
        raise ModelUnavailableError(f"tiny-voca model not found in {d}", "run `nebi-voice download-model` once while online")
    with _download_lock:
        if model_ready(d):  # another thread finished it meanwhile
            return d
        partial = d.with_name(d.name + ".partial")
        try:
            from huggingface_hub import snapshot_download

            d.parent.mkdir(parents=True, exist_ok=True)
            snapshot_download(REPO_ID, local_dir=str(partial), allow_patterns=_FILES)
        except Exception as exc:
            raise ModelUnavailableError(
                f"cannot download the tiny-voca voice model (~{MODEL_SIZE_MB} MB) from huggingface.co/{REPO_ID}: {exc}",
                f"check the internet connection, or run `nebi-voice download-model` once while online, "
                f"or point ${ENV_MODEL_DIR} at a copy of the model",
            ) from exc
        if not model_ready(partial):
            raise ModelUnavailableError(f"the download of {REPO_ID} into {partial} is incomplete", "retry `nebi-voice download-model`")
        if d.exists():
            shutil.rmtree(d)
        partial.rename(d)
        return d


@dataclass
class RawSpeech:
    audio: np.ndarray  # float32 mono, SAMPLE_RATE
    durations: np.ndarray  # frames (of HOP samples) per phoneme token
    symbols: tuple[str, ...] = ()  # phoneme symbols ("<sp>" = word boundary, punctuation kept)


class TinyVoca:
    """Loads tiny-voca once; `render()` turns text into raw speech with source-level controls."""

    def __init__(self, model_dir: str | Path | None = None, threads: int = 4) -> None:
        self.model_dir = ensure_model(model_dir)
        # dizione locates its lexicon/G2P data through this env var; set before importing it.
        os.environ.setdefault("DIZIONE_DATA", str(self.model_dir / "phonemizer"))
        import onnxruntime as ort

        from nebi_voice._vendor import dizione  # vendored: not on PyPI

        self._dizione = dizione
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = threads
        opts.log_severity_level = 3
        self._session = ort.InferenceSession(str(self.model_dir / "onnx" / "model.onnx"), opts)
        self._lock = threading.Lock()

    def phonemize(self, text: str):
        return self._dizione.phonemize(text)

    def render(
        self,
        text: str,
        *,
        voice: str = "M2",
        speed: float = 1.0,
        pitch_semitones: float = 0.0,
        pitch_log_offset: float = 0.0,
        pitch_range: float = 1.0,
    ) -> RawSpeech:
        """Synthesize one utterance (<= ~32 s). Pitch is given in semitones (+ optional raw log offset)."""
        ph = self.phonemize(text)
        if len(ph.ids) == 0:
            return RawSpeech(np.zeros(0, np.float32), np.zeros(0, np.int64), ())
        one = lambda v: np.array([v], dtype=np.float32)  # noqa: E731
        feeds = {
            "tokens": np.array([ph.ids], dtype=np.int64),
            "stress": np.array([ph.stress], dtype=np.int64),
            "voice": np.array([VOICES[voice]], dtype=np.int64),
            "speed": one(min(4.0, max(0.25, speed))),
            "pitch_shift": one(pitch_semitones * LN_SEMITONE + pitch_log_offset),
            "pitch_range": one(min(3.0, max(0.0, pitch_range))),
        }
        with self._lock:
            audio, durations = self._session.run(None, feeds)
        return RawSpeech(audio[0].astype(np.float32), durations[0], tuple(ph.symbols))
