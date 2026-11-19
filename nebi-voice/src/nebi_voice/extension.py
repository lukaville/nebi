"""nebi extension: nebi-voice as a TTS engine for `nebi say` (nebi-cli) and the cockpit Speak panel.

Loaded through the ``nebi.extensions`` entry point (``nebi-voice = "nebi_voice.extension:extension"``). nebi
is imported lazily, so ``import nebi_voice`` keeps working without it.

* `NebiVoiceEngine` implements ``nebi.tts.TtsEngine``: emotions = nebi-voice's emotions, presets = its voice
  presets (the robot-pet ``robot`` first, the default), ``priority`` 20 so it beats the OS ``system`` engine (5)
  and ``chirp`` (0) as the default engine.
* Both setup hooks register it in the process-wide ``nebi.tts`` registry (idempotent) and start downloading the
  tiny-voca model (~27 MB) in a background thread when it is not on disk yet, so the first Speak is not slow.
  While that runs, `synthesize` waits a few seconds and then raises ``TtsError`` asking to retry.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

from nebi_voice import __version__
from nebi_voice.emotion import EMOTIONS
from nebi_voice.engine import MODEL_SIZE_MB, ModelUnavailableError, ensure_model, find_model
from nebi_voice.voice import PRESETS, RobotVoice

if TYPE_CHECKING:
    from nebi.tts import SpeechAudio

__all__ = ["DEFAULT_PRESET", "ENGINE_NAME", "NebiVoiceEngine", "NebiVoiceExtension", "extension"]

ENGINE_NAME = "nebi-voice"
DEFAULT_PRESET = "robot"  # the small robot-pet voice
DOWNLOAD_WAIT_S = 4.0  # how long a Speak waits for a running model download before asking to retry


class NebiVoiceEngine:
    """`nebi.tts.TtsEngine` on top of `nebi_voice.RobotVoice` (one cached voice per preset)."""

    name = ENGINE_NAME
    priority = 20

    def __init__(self) -> None:
        self.emotions: tuple[str, ...] = tuple(EMOTIONS)
        self.presets: tuple[str, ...] = (DEFAULT_PRESET, *(p for p in PRESETS if p != DEFAULT_PRESET))
        self._voices: dict[str, RobotVoice] = {}
        # RobotVoice keeps its render cache in a module-level OrderedDict and shares one ONNX session: serialize.
        self._lock = threading.Lock()
        self._prefetch: threading.Thread | None = None
        self._prefetch_lock = threading.Lock()

    # ------------------------------------------------------------------ model download

    def prefetch(self) -> None:
        """Start downloading the model in the background unless it is on disk (or already downloading)."""
        with self._prefetch_lock:
            if self.downloading or find_model() is not None:
                return
            self._prefetch = threading.Thread(target=self._download, name="nebi-voice-model-download", daemon=True)
            self._prefetch.start()

    @staticmethod
    def _download() -> None:
        try:
            ensure_model()
        except ModelUnavailableError:
            pass  # reported (with its hint) by the next synthesize(), which retries the download

    @property
    def downloading(self) -> bool:
        return self._prefetch is not None and self._prefetch.is_alive()

    # ------------------------------------------------------------------ TtsEngine

    def synthesize(self, text: str | None, *, emotion: str, babble: bool, preset: str | None) -> SpeechAudio:
        from nebi.tts import SpeechAudio, TtsError

        preset = preset or self.presets[0]
        if preset not in PRESETS:
            raise TtsError(f"unknown nebi-voice preset {preset!r} (presets: {', '.join(self.presets)})")
        prefetch = self._prefetch
        if prefetch is not None and prefetch.is_alive():
            prefetch.join(DOWNLOAD_WAIT_S)
            if prefetch.is_alive():
                raise TtsError(f"voice model downloading (~{MODEL_SIZE_MB} MB), retry in a few seconds")
        emotion = emotion if emotion in self.emotions else "neutral"
        with self._lock:
            try:
                voice = self._voices.get(preset)
                if voice is None:
                    voice = self._voices[preset] = RobotVoice(preset)  # first use loads (or downloads) the model
                u = voice.render(text or "", emotion=emotion, mode="babble" if babble else "speak")
            except ModelUnavailableError as exc:
                raise TtsError(str(exc)) from exc
            except ValueError:
                raise  # "nothing to say (no words)" -> the host's BAD_TEXT
            except Exception as exc:
                raise TtsError(f"nebi-voice could not render: {type(exc).__name__}: {exc}") from exc
        return SpeechAudio(
            u.audio,
            u.sample_rate,
            text="" if babble else u.text,
            emotion=u.emotion,
            mode="babble" if babble else "words",
            beats=[(float(t), float(s)) for t, s in u.beats],
            color=u.color,
        )


_GUIDE = """\
### nebi-voice (TTS extension)

`nebi-voice` is the default speech engine when installed (priority 20, above `system` and `chirp`): a small-robot
voice on the tiny-voca model with real words, emotions and babble. Presets: `robot` (default; high, halting, pet-like),
`probe`, `nebi` (clear full sentences), `robot-low`, `robot-male`, `deep-robot`, `plain`.
Example: `nebi say 'Ball? 👀' --engine nebi-voice --preset nebi`. The model (~27 MB) downloads on first use; while
it downloads, `say` fails with "voice model downloading" — retry after a few seconds (or prepare it offline with
`nebi-voice download-model`).
"""


class NebiVoiceExtension:
    name = "nebi-voice"
    version = __version__

    def __init__(self) -> None:
        self.engine: NebiVoiceEngine | None = None

    def _register(self, *, prefetch: bool) -> NebiVoiceEngine:
        from nebi.tts import register_engine

        if self.engine is None:
            self.engine = NebiVoiceEngine()
        register_engine(self.engine, replace=True)
        if prefetch:
            self.engine.prefetch()
        return self.engine

    def setup_agent(self, ctx: Any) -> None:
        # Spec-only passes (no runtime, e.g. `nebi --help`) register the engine but do not start a download.
        self._register(prefetch=getattr(ctx, "runtime", None) is not None)

    def setup_server(self, ctx: Any) -> None:
        self._register(prefetch=True)

    def guide_markdown(self) -> str:
        return _GUIDE


extension = NebiVoiceExtension()
