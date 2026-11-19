"""High-level robot voice: text -> styled speech (presets, halting delivery, effects chain)."""

from __future__ import annotations

import math
import re
import subprocess
import sys
import tempfile
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from pathlib import Path

import numpy as np

from nebi_voice import effects as fx
from nebi_voice import prosody
from nebi_voice.emotion import EMOTIONS, Emotion, babble_text, extract_emoji
from nebi_voice.engine import LN_SEMITONE, SAMPLE_RATE, TinyVoca

_ENGINE: TinyVoca | None = None


def get_engine() -> TinyVoca:
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = TinyVoca()
    return _ENGINE


@dataclass(frozen=True)
class VoiceParams:
    # --- source (rendered by tiny-voca) ---
    base_voice: str = "M2"
    pitch: float = 8.0  # semitones above the base voice
    formant: float = 1.3  # >1 = smaller vocal tract (applied by resampling, pitch/duration pre-compensated)
    pitch_range: float = 1.9  # melodic exaggeration (1 = natural, up to 3)
    speed: float = 0.85  # <1 = slower, more deliberate
    # --- delivery ---
    delivery: str = "auto"  # "phrase" (fluent) | "halting" (always paused) | "auto" (pauses fade out with length)
    halting: float = 0.85  # pause amount 0..1 (0 = fluent); auto scales it down smoothly for longer sentences
    halting_max_words: int = 5  # auto: sentence length where halting has faded to half
    word_gap: tuple[float, float] = (0.07, 0.18)  # seconds between words in halting delivery
    word_pitch_jitter: float = 2.5  # random semitone offset per word (inquisitive melody)
    stretch_last_word: float = 0.75  # speed multiplier for the last word ("Eee-vaaa?")
    question_lift: float = 3.0  # extra semitones on the last word of a question
    exclaim_lift: float = 1.5
    # --- machine coloration ---
    vibrato_hz: float = 6.5
    vibrato_cents: float = 22.0
    vibrato_drift: float = 0.5
    tremolo_hz: float = 11.0
    tremolo_depth: float = 0.08
    ring_hz: float = 36.0
    ring_mix: float = 0.12
    box_delay_ms: float = 2.4
    box_feedback: float = 0.38
    box_mix: float = 0.3
    crush_bits: int = 10
    crush_hold: int = 2
    crush_mix: float = 0.22
    speaker_low_hz: float = 280.0
    speaker_high_hz: float = 6800.0
    presence_db: float = 4.0
    drive: float = 1.6
    # --- mechanical extras ---
    servo: bool = True  # whirs/clicks between phrases (and occasionally between words)
    servo_prob: float = 0.35
    boop: bool = False  # electronic boop before speaking
    # --- inflection & output ---
    glide: float = 1.0  # strength of drawn pitch contours + vowel stretch (0 = off)
    volume: float = 1.0  # output level after loudness normalization (0..1.5)
    master: bool = True  # compressor + loudness normalize + limiter + fade-in
    description: str = ""


PRESETS: dict[str, VoiceParams] = {
    # Small pet robot: processed, high, squeezed, inquisitive single words, boxy tin speaker.
    "robot": VoiceParams(description="Small curious box robot: high, squeezed, halting, warbly, tin-can speaker."),
    # Sleek probe robot: cleaner, brighter, more synthetic, fewer mechanical noises.
    "probe": VoiceParams(
        base_voice="F2",
        pitch=5.0,
        formant=1.12,
        pitch_range=1.5,
        speed=1.0,
        delivery="phrase",
        vibrato_hz=7.5,
        vibrato_cents=10.0,
        vibrato_drift=0.1,
        tremolo_depth=0.04,
        ring_hz=220.0,
        ring_mix=0.18,
        box_mix=0.1,
        crush_bits=12,
        crush_mix=0.1,
        speaker_low_hz=200.0,
        speaker_high_hz=9000.0,
        drive=1.2,
        servo=False,
        boop=True,
        description="Sleek probe robot: brighter, smoother, synthetic ring tone.",
    ),
    # Nebi's own voice: friendly, intelligible full sentences, mild robot coloration.
    "nebi": VoiceParams(
        pitch=6.0,
        formant=1.2,
        pitch_range=1.5,
        speed=0.95,
        delivery="phrase",
        word_pitch_jitter=1.5,
        vibrato_cents=12.0,
        ring_mix=0.07,
        box_mix=0.2,
        crush_mix=0.12,
        drive=1.3,
        servo_prob=0.2,
        description="Friendly companion robot: clear sentences, light robot coloration.",
    ),
    # Male-character variants (from the M2 comparison renders in samples/male/).
    "robot-low": VoiceParams(pitch=3.0, formant=1.1, pitch_range=1.7, description="Small-robot delivery, still recognisably male."),
    "robot-male": VoiceParams(
        pitch=0.0, formant=1.0, pitch_range=1.4, speed=0.95, description="Natural male voice through the robot box/speaker chain."
    ),
    "deep-robot": VoiceParams(
        pitch=-3.0, formant=0.92, pitch_range=1.3, speed=0.9, delivery="phrase", description="Deeper, bigger-bodied robot, full sentences."
    ),
    "plain": VoiceParams(
        pitch=0.0,
        formant=1.0,
        pitch_range=1.0,
        speed=1.0,
        delivery="phrase",
        vibrato_cents=0.0,
        tremolo_depth=0.0,
        ring_mix=0.0,
        box_mix=0.0,
        crush_mix=0.0,
        speaker_low_hz=0.0,
        speaker_high_hz=0.0,
        presence_db=0.0,
        drive=1.0,
        servo=False,
        description="Unprocessed tiny-voca M2 (for comparison).",
    ),
}

_SENT_RE = re.compile(r"[^.!?…]+[.!?…]*")
_WORD_RE = re.compile(r"[\w'’-]+", re.UNICODE)


@dataclass
class _Utterance:
    text: str
    end: str  # "?", "!", "." or ""
    words: list[str] = field(default_factory=list)


def split_utterances(text: str) -> list[_Utterance]:
    out = []
    for m in _SENT_RE.finditer(text.strip()):
        s = m.group(0).strip()
        if not s:
            continue
        end = s[-1] if s[-1] in "?!." else ""
        words = _WORD_RE.findall(s)
        if words:
            out.append(_Utterance(s, end, words))
    return out


_FUNCTION_WORDS = {
    "a", "an", "the", "is", "are", "am", "was", "were", "be", "to", "of", "in", "on", "at", "by", "for", "with",
    "my", "your", "our", "his", "her", "its", "their", "i", "it", "and", "or", "but", "so", "do", "does", "this", "that",
    "can", "will", "not", "no", "you", "we", "me", "us", "just", "very",
}
_BOUNDARY_PUNCT = set(",;:—–…")
_END_PUNCT = set(".?!")


def _word_spans(symbols: tuple[str, ...], durations: np.ndarray) -> list[list]:
    """[start, end, kind_after] in model samples for each word, from tiny-voca's `<sp>` tokens and durations."""
    from nebi_voice.engine import HOP

    spans: list[list] = []
    cum = 0
    start: int | None = None
    for sym, d in zip(symbols, durations, strict=False):
        n = int(d) * HOP
        if sym in ("<bos>", "<eos>"):
            cum += n
            continue
        if sym == "<sp>" or sym in _BOUNDARY_PUNCT or sym in _END_PUNCT:
            kind = "comma" if sym in _BOUNDARY_PUNCT else "end" if sym in _END_PUNCT else "space"
            if start is not None:
                spans.append([start, cum, kind])
                start = None
            elif spans and kind == "comma":
                spans[-1][2] = "comma"
            cum += n
            continue
        if start is None:
            start = cum
        cum += n
    if start is not None:
        spans.append([start, cum, "end"])
    return spans


def _align_labels(words: list[str], n: int) -> list[str]:
    """Match regex words to the phonemizer's word units (it fuses clitic pairs like "in the")."""
    w = list(words)
    while len(w) > n:
        fw = [i for i in range(len(w) - 1) if w[i].lower().split()[-1] in _FUNCTION_WORDS]
        pair = next((i for i in fw if w[i + 1].lower().split()[0] in _FUNCTION_WORDS), fw[0] if fw else 0)
        w[pair : pair + 2] = [f"{w[pair]} {w[pair + 1]}"]
    return w if len(w) == n else [f"w{i + 1}" for i in range(n)]


def _quiet_point(y: np.ndarray, at: int, sr: int, radius_ms: float = 15.0) -> int:
    """Move a cut to the quietest 2 ms within +-radius (avoids clicks and chopped consonants)."""
    r, w = int(sr * radius_ms / 1000), max(1, int(sr * 0.002))
    lo, hi = max(0, at - r), min(y.size - w, at + r)
    if hi <= lo:
        return int(min(max(at, 0), y.size))
    e = np.convolve(y[lo : hi + w] ** 2, np.ones(w), "valid")[: hi - lo]
    return lo + int(np.argmin(e)) + w // 2


def _edge_fade(seg: np.ndarray, sr: int, start: bool, end: bool, ms: float = 8.0) -> np.ndarray:
    n = min(seg.size // 3, int(sr * ms / 1000))
    if n <= 1:
        return seg
    seg = seg.copy()
    ramp = (np.sin(np.linspace(0, np.pi / 2, n)) ** 2).astype(np.float32)
    if start:
        seg[:n] *= ramp
    if end:
        seg[-n:] *= ramp[::-1]
    return seg


@dataclass
class Utterance:
    """A rendered line plus everything a robot body needs to perform it."""

    audio: np.ndarray  # float32 mono
    sample_rate: int
    text: str  # what was actually voiced (the generated gibberish in babble mode)
    emotion: str
    mode: str  # "speak" | "babble"
    words: list[dict]  # [{"word", "t", "end"}] in seconds (halting delivery only)
    envelope: np.ndarray  # loudness 0..1 at ENVELOPE_HZ
    beats: list[tuple[float, float]]  # syllable nuclei (t, strength)
    color: tuple[int, int, int]  # suggested eye color

    @property
    def duration(self) -> float:
        return self.audio.size / self.sample_rate

    def timeline(self) -> dict:
        return {
            "duration_s": round(self.duration, 3),
            "text": self.text,
            "emotion": self.emotion,
            "mode": self.mode,
            "color": list(self.color),
            "envelope_hz": ENVELOPE_HZ,
            "envelope": [round(float(v), 2) for v in self.envelope],
            "beats": [list(b) for b in self.beats],
            "words": self.words,
        }


ENVELOPE_HZ = 25.0
_CACHE: OrderedDict[tuple, Utterance] = OrderedDict()
_CACHE_MAX = 64


class RobotVoice:
    """Text-to-speech with a small-robot (pet) character.

    >>> v = RobotVoice("robot")
    >>> u = v.render("Hello?", emotion="curious")       # Utterance: audio + envelope/beats timeline
    >>> u = v.render(emotion="happy")                  # no text -> pet babble ("Bee doo wee!")
    >>> audio = v.synthesize("Hello 😊")               # emoji pick the emotion
    """

    sample_rate = SAMPLE_RATE

    def __init__(self, preset: str | VoiceParams = "robot", seed: int | None = 0, **overrides) -> None:
        params = PRESETS[preset] if isinstance(preset, str) else preset
        self.params = replace(params, **overrides) if overrides else params
        self.seed = seed
        self.engine = get_engine()

    # ------------------------------------------------------------------ rendering

    def _render(
        self,
        text: str,
        pitch: float,
        speed: float,
        pitch_range: float,
        contour: str = "flat",
        depth: float = 0.0,
        stretch: float = 1.0,
        flatten: float = 0.0,
    ) -> np.ndarray:
        p = self.params
        k = max(0.5, min(2.0, p.formant))
        # Render low & slow, then resample up by k: formants rise by k, pitch/duration land on target.
        raw = self.engine.render(
            text,
            voice=p.base_voice,
            speed=speed / k,
            pitch_semitones=pitch,
            pitch_log_offset=-math.log(k),
            pitch_range=min(3.0, pitch_range),
        ).audio
        y = fx.formant_scale(raw, k)
        if p.glide > 0 and (depth or stretch != 1.0):
            y = prosody.apply(y, SAMPLE_RATE, contour, depth * p.glide, 1.0 + (stretch - 1.0) * p.glide, min(1.0, flatten * p.glide))
        return fx.fade(y, SAMPLE_RATE)

    def _halting_amount(self, n_words: int) -> float:
        p = self.params
        if p.delivery == "phrase":
            return 0.0
        if p.delivery == "halting":
            return p.halting
        # auto: fade smoothly from halting to fluent around `halting_max_words` (no hard switch)
        return p.halting / (1.0 + math.exp((n_words - p.halting_max_words - 0.5) / 1.2))

    def _sentence(self, u: _Utterance, emo: Emotion, neutral: bool, h: float, rng: np.random.Generator):
        """Render one sentence fluently, then perform per-word melody in a single WORLD pass.

        Returns (audio, word-start cut samples, labels, pause weights).
        """
        p = self.params
        k = max(0.5, min(2.0, p.formant))
        raw = self.engine.render(
            u.text,
            voice=p.base_voice,
            speed=p.speed * emo.speed_mul / k,
            pitch_semitones=p.pitch + emo.pitch,
            pitch_log_offset=-math.log(k),
            pitch_range=min(3.0, p.pitch_range * emo.range_mul),
        )
        y = fx.formant_scale(raw.audio, k)
        spans = _word_spans(raw.symbols, raw.durations)
        n = len(spans)
        if n == 0:
            return y, [], [], []
        bounds = [int(round((spans[i][1] + spans[i + 1][0]) / 2 / k)) for i in range(n - 1)]
        labels = _align_labels(u.words, n)

        g = p.glide
        lift = (p.question_lift if u.end == "?" else p.exclaim_lift if u.end == "!" else 0.0) * (0.5 + 0.5 * h)
        jitter = p.word_pitch_jitter * (0.35 + 0.65 * h)  # fluent speech keeps a little bounce
        offsets = [float(rng.uniform(-1, 1)) * jitter for _ in range(n)]
        offsets[-1] += lift
        last_contour = "question" if (u.end == "?" and neutral) else emo.last_contour
        shapes = [emo.word_contour] * (n - 1) + [last_contour]
        depths = [emo.word_depth * float(rng.uniform(0.5, 1.2)) * h * g for _ in range(n - 1)]
        depths.append(emo.last_depth * g * (0.6 + 0.4 * h))
        # The last word's melody is fully replaced by the drawn contour (consistent "Eee-vaaa?" shapes);
        # vibrato/servo jitter added later keeps it from sounding static.
        flattens = [0.3 * h * g] * (n - 1) + [min(1.0, g)]
        last_stretch = 1.0 + (emo.vowel_stretch * (1.0 + (1.0 / p.stretch_last_word - 1.0) * h) - 1.0) * g
        stretches = [1.0] * (n - 1) + [last_stretch]
        if g > 0 or jitter > 0 or lift:
            y, bounds = prosody.perform(y, SAMPLE_RATE, bounds, offsets, shapes, depths, flattens, stretches)

        weights = []
        for i in range(n - 1):
            w = 1.8 if spans[i][2] == "comma" else 1.0
            if labels[i].lower().split()[-1] in _FUNCTION_WORDS:
                w = 0.25 if spans[i][2] != "comma" else w  # "is my | plant": function words lean on the next word
            if i == n - 2:
                w *= 1.3  # anticipation before the last word
            weights.append(w)
        return fx.fade(y, SAMPLE_RATE), bounds, labels, weights

    def _dry(self, text: str, emo: Emotion, neutral: bool, rng: np.random.Generator) -> tuple[np.ndarray, list[dict]]:
        p = self.params
        sr = SAMPLE_RATE
        parts: list[np.ndarray] = []
        words: list[dict] = []
        pos = int(0.05 * sr)  # render() pads 50 ms of silence in front

        def add(a: np.ndarray) -> None:
            nonlocal pos
            parts.append(a)
            pos += a.size

        if p.boop:
            add(fx.chirp_boop(sr))
            add(fx.silence(sr, 0.08))
        gap_range = (p.word_gap[0] * emo.gap_mul, p.word_gap[1] * emo.gap_mul)
        utts = split_utterances(text)
        prev_h: float | None = None
        for ui, u in enumerate(utts):
            own = self._halting_amount(len(u.words))
            h = own if prev_h is None else 0.6 * own + 0.4 * prev_h  # style momentum across sentences
            prev_h = h
            y, bounds, labels, weights = self._sentence(u, emo, neutral, h, rng)
            gaps = [h * float(rng.uniform(*gap_range)) * w for w in weights]
            cuts = [_quiet_point(y, b, sr) for b in bounds]
            edges = [0, *cuts, y.size]
            for wi in range(len(edges) - 1):
                seg = y[edges[wi] : edges[wi + 1]]
                pause_before = wi > 0 and gaps[wi - 1] >= 0.02
                pause_after = wi < len(gaps) and gaps[wi] >= 0.02
                if pause_before or pause_after:
                    seg = _edge_fade(seg, sr, pause_before, pause_after)
                if wi < len(labels):
                    words.append({"word": labels[wi], "t": round(pos / sr, 3), "end": round((pos + seg.size) / sr, 3)})
                add(seg)
                if pause_after:
                    add(fx.silence(sr, gaps[wi]))
                    if p.servo and rng.random() < p.servo_prob * 0.5 * h:
                        add(fx.click(sr, seed=int(rng.integers(1 << 30))))
            if ui < len(utts) - 1:
                add(fx.silence(sr, rng.uniform(0.18, 0.32) * emo.gap_mul))
                if p.servo and rng.random() < p.servo_prob:
                    up = bool(rng.random() < 0.5)
                    add(
                        fx.servo_whir(
                            sr,
                            seconds=float(rng.uniform(0.1, 0.22)),
                            f_start=700 if up else 1100,
                            f_end=1100 if up else 700,
                            seed=int(rng.integers(1 << 30)),
                        )
                    )
                    add(fx.silence(sr, 0.08))
        return (np.concatenate(parts) if parts else np.zeros(0, np.float32)), words

    def _colorize(self, x: np.ndarray, emo: Emotion, seed: int) -> np.ndarray:
        p, sr = self.params, SAMPLE_RATE
        x = fx.normalize(x, 0.8)
        x = fx.vibrato(x, sr, p.vibrato_hz, p.vibrato_cents * emo.vibrato_mul, p.vibrato_drift, seed=seed)
        x = fx.box_resonance(x, sr, p.box_delay_ms, p.box_feedback, p.box_mix)
        x = fx.ring_mod(x, sr, p.ring_hz, p.ring_mix)
        x = fx.tremolo(x, sr, p.tremolo_hz, min(0.6, p.tremolo_depth + emo.tremolo_add))
        x = fx.bitcrush(x, p.crush_bits, p.crush_hold, p.crush_mix)
        x = fx.small_speaker(x, sr, p.speaker_low_hz, p.speaker_high_hz, presence_db=p.presence_db)
        x = fx.saturate(x, p.drive)
        return fx.normalize(x, 0.89)

    def render(
        self,
        text: str = "",
        emotion: str | None = None,
        mode: str = "auto",
        seed: int | None = None,
        volume: float | None = None,
    ) -> Utterance:
        """Render `text` (or pet babble) with an emotion. mode: auto (babble if no words) | speak | babble."""
        s = self.seed if seed is None else seed
        rng = np.random.default_rng(s)
        clean, emoji_emotion = extract_emoji(text or "")
        name = (emotion or emoji_emotion or "neutral").lower()
        if name not in EMOTIONS:
            raise ValueError(f"unknown emotion {name!r} (choose from {', '.join(EMOTIONS)})")
        emo = EMOTIONS[name]
        has_words = bool(_WORD_RE.search(clean))
        if mode not in ("auto", "speak", "babble"):
            raise ValueError("mode must be auto, speak or babble")
        use_mode = "babble" if (mode == "babble" or (mode == "auto" and not has_words)) else "speak"
        if use_mode == "speak" and not has_words:
            raise ValueError("nothing to say (no words); use mode='babble' for pet sounds")
        spoken = babble_text(name, rng) if use_mode == "babble" else clean
        vol = self.params.volume if volume is None else volume

        key = (spoken, name, use_mode, s, vol, self.params)
        hit = _CACHE.get(key) if s is not None else None
        if hit is not None:
            _CACHE.move_to_end(key)
            return hit

        dry, words = self._dry(spoken, emo, name == "neutral", rng)
        if dry.size == 0:
            audio = dry
        else:
            pad = fx.silence(SAMPLE_RATE, 0.05)
            audio = self._colorize(np.concatenate([pad, dry, pad]), emo, seed=int(rng.integers(1 << 30)))
            if self.params.master:
                audio = fx.master(audio, SAMPLE_RATE, volume=vol * emo.volume)
        env = fx.envelope(audio, SAMPLE_RATE, ENVELOPE_HZ)
        u = Utterance(audio, SAMPLE_RATE, spoken, name, use_mode, words, env, fx.beats(env, ENVELOPE_HZ), emo.color)
        if s is not None:
            _CACHE[key] = u
            while len(_CACHE) > _CACHE_MAX:
                _CACHE.popitem(last=False)
        return u

    def synthesize(self, text: str = "", seed: int | None = None, emotion: str | None = None, mode: str = "auto") -> np.ndarray:
        """Return float32 mono audio at `RobotVoice.sample_rate` (24 kHz)."""
        return self.render(text, emotion=emotion, mode=mode, seed=seed).audio

    # ------------------------------------------------------------------ convenience

    def save(self, text: str, path: str | Path, seed: int | None = None, emotion: str | None = None, mode: str = "auto") -> Path:
        import soundfile as sf

        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        sf.write(str(path), self.synthesize(text, seed, emotion, mode), SAMPLE_RATE, subtype="PCM_16")
        return path

    def play(self, text: str = "", seed: int | None = None, emotion: str | None = None, mode: str = "auto") -> None:
        play_audio(self.synthesize(text, seed, emotion, mode), SAMPLE_RATE)


def play_audio(audio: np.ndarray, sr: int = SAMPLE_RATE) -> None:
    """Play through the default output (sounddevice if installed, else afplay/aplay)."""
    try:
        import sounddevice as sd

        sd.play(audio, sr)
        sd.wait()
        return
    except ImportError:
        pass
    import soundfile as sf

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        sf.write(f.name, audio, sr, subtype="PCM_16")
        player = ["afplay", f.name] if sys.platform == "darwin" else ["aplay", "-q", f.name]
        subprocess.run(player, check=False)
        Path(f.name).unlink(missing_ok=True)


def speak(text: str = "", preset: str = "robot", out: str | Path | None = None, play: bool = False, emotion: str | None = None, **overrides) -> np.ndarray:
    """One-call helper: synthesize (and optionally save/play) text with a preset and emotion."""
    v = RobotVoice(preset, **overrides)
    audio = v.synthesize(text, emotion=emotion)
    if out:
        import soundfile as sf

        sf.write(str(out), audio, SAMPLE_RATE, subtype="PCM_16")
    if play:
        play_audio(audio)
    return audio


__all__ = ["LN_SEMITONE", "PRESETS", "RobotVoice", "Utterance", "VoiceParams", "play_audio", "speak"]
