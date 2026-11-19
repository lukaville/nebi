"""Emotions, pet babble and emoji handling.

A pet robot mostly *vocalizes*: chirps, coos and gibberish carry the mood, words are rare. An `Emotion`
bends any preset (pitch, melody, tempo, warble, loudness) and picks the pitch contour drawn on words.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class Emotion:
    pitch: float = 0.0  # semitones added to the preset
    range_mul: float = 1.0  # pitch_range multiplier
    speed_mul: float = 1.0
    last_contour: str = "rise_fall"  # contour on the last word of an utterance
    last_depth: float = 3.0  # semitones
    word_contour: str = "rise_fall"  # contour on other words
    word_depth: float = 1.2
    vowel_stretch: float = 1.25  # last-word vowel lengthening ("Eeee-vaaa")
    gap_mul: float = 1.0
    vibrato_mul: float = 1.0
    tremolo_add: float = 0.0
    volume: float = 1.0  # linear gain before the master limiter
    end_mark: str = "."  # punctuation used for babble
    color: tuple[int, int, int] = (255, 255, 255)  # suggested eye color while speaking (RGB)


EMOTIONS: dict[str, Emotion] = {
    "neutral": Emotion(),
    "happy": Emotion(pitch=1.5, range_mul=1.15, speed_mul=1.05, last_contour="rise_fall", last_depth=4.0, end_mark="!", color=(255, 190, 40)),
    "excited": Emotion(
        pitch=3.0, range_mul=1.3, speed_mul=1.2, last_contour="rise", last_depth=5.0, word_depth=2.0, vowel_stretch=1.1,
        gap_mul=0.6, vibrato_mul=1.3, volume=1.1, end_mark="!", color=(255, 120, 0),
    ),
    "curious": Emotion(
        pitch=1.0, range_mul=1.1, speed_mul=0.9, last_contour="question", last_depth=5.0, word_contour="rise",
        vowel_stretch=1.45, end_mark="?", color=(0, 210, 255),
    ),
    "sad": Emotion(
        pitch=-2.5, range_mul=0.7, speed_mul=0.75, last_contour="droop", last_depth=4.0, word_contour="fall", word_depth=1.0,
        vowel_stretch=1.5, gap_mul=1.6, vibrato_mul=1.4, volume=0.75, color=(40, 80, 255),
    ),
    "scared": Emotion(
        pitch=4.0, range_mul=0.9, speed_mul=1.25, last_contour="wobble", last_depth=3.0, word_contour="wobble", word_depth=1.5,
        vowel_stretch=1.0, gap_mul=0.7, vibrato_mul=2.2, tremolo_add=0.15, volume=0.85, end_mark="!", color=(150, 60, 255),
    ),
    "sleepy": Emotion(
        pitch=-3.0, range_mul=0.6, speed_mul=0.7, last_contour="droop", last_depth=3.0, word_contour="fall", word_depth=0.8,
        vowel_stretch=1.7, gap_mul=1.8, vibrato_mul=0.6, volume=0.6, color=(40, 40, 140),
    ),
    "grumpy": Emotion(
        pitch=-2.0, range_mul=0.8, speed_mul=1.0, last_contour="fall", last_depth=3.5, word_contour="fall", word_depth=1.5,
        vowel_stretch=1.0, gap_mul=0.8, volume=1.0, end_mark="!", color=(255, 40, 20),
    ),
    "love": Emotion(
        pitch=0.5, range_mul=1.0, speed_mul=0.85, last_contour="rise_fall", last_depth=3.5, word_contour="rise_fall",
        vowel_stretch=1.6, gap_mul=1.2, vibrato_mul=1.5, volume=0.85, color=(255, 70, 170),
    ),
    "uh-oh": Emotion(
        pitch=1.0, range_mul=1.0, speed_mul=0.9, last_contour="step_down", last_depth=5.0, word_contour="step_down",
        vowel_stretch=1.2, end_mark=".", color=(255, 160, 0),
    ),
}

# Pseudo-words per mood (spelled so the English G2P gives cute, vowel-heavy syllables).
BABBLE: dict[str, list[str]] = {
    "neutral": ["boo", "dee", "bah", "wuh", "doo", "beep", "mm"],
    "happy": ["wee", "bee", "doo", "deedoo", "yay", "boop", "bidee", "ooh"],
    "excited": ["wee", "yip", "yippee", "bada", "woohoo", "zoom", "deedee", "whoa"],
    "curious": ["hmm", "ooh", "wuh", "huh", "meep", "oo", "bwah"],
    "sad": ["oh", "ooh", "woo", "mm", "aww", "noo"],
    "scared": ["eep", "yikes", "ee", "nonono", "wah", "ah"],
    "sleepy": ["mm", "hwah", "mmhm", "zz", "nnn", "ahh"],
    "grumpy": ["hmph", "grr", "nuh", "bah", "pfft", "meh"],
    "love": ["aww", "ooh", "mwah", "hehe", "mm", "doo"],
    "uh-oh": ["uh-oh", "oh-oh", "oops", "woah"],
}

EMOJI_EMOTION: dict[str, str] = {
    "😀": "happy", "😃": "happy", "😄": "happy", "😊": "happy", "🙂": "happy", "😁": "happy", "👍": "happy",
    "🤩": "excited", "🎉": "excited", "😆": "excited", "🥳": "excited", "⚡": "excited", "‼": "excited",
    "🤔": "curious", "🧐": "curious", "❓": "curious", "👀": "curious",
    "😢": "sad", "😞": "sad", "😔": "sad", "😭": "sad", "🥺": "sad",
    "😱": "scared", "😨": "scared", "😰": "scared", "🙀": "scared",
    "😴": "sleepy", "🥱": "sleepy", "💤": "sleepy",
    "😠": "grumpy", "😤": "grumpy", "😡": "grumpy", "🙄": "grumpy",
    "❤": "love", "❤️": "love", "😍": "love", "🥰": "love", "💕": "love", "😘": "love",
    "😬": "uh-oh", "😅": "uh-oh",
}

_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF☀-➿⬀-⯿️‍‼⁉]+",
)


def extract_emoji(text: str) -> tuple[str, str | None]:
    """Strip emoji from `text`; return (clean text, emotion implied by the first known emoji)."""
    found = None
    for m in _EMOJI_RE.finditer(text):
        for ch in [m.group(0), *m.group(0)]:
            if ch in EMOJI_EMOTION:
                found = EMOJI_EMOTION[ch]
                break
        if found:
            break
    clean = _EMOJI_RE.sub(" ", text)
    return re.sub(r"\s+", " ", clean).strip(), found


def babble_text(emotion: str, rng: np.random.Generator, words: int | None = None) -> str:
    """Generate a short gibberish line for `emotion`, e.g. 'Bee doo wee!'."""
    vocab = BABBLE.get(emotion, BABBLE["neutral"])
    n = words or int(rng.choice([1, 2, 2, 3, 3, 4]))
    picks = [str(rng.choice(vocab)) for _ in range(n)]
    end = EMOTIONS.get(emotion, EMOTIONS["neutral"]).end_mark
    return " ".join(picks).capitalize() + end
