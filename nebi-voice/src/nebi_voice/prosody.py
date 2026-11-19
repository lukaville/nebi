"""Drawn pitch contours and vowel stretching via the WORLD vocoder (inflection drawn like on an instrument).

tiny-voca only takes one global pitch per call, so melody inside a word ("Heee-looo?" rising, "uh-oh"
stepping down) is added afterwards: analyse with WORLD, multiply F0 by a contour shape, repeat voiced
frames to stretch vowels (consonants keep their length), resynthesize.
"""

from __future__ import annotations

import sys
import types
from collections.abc import Callable

import numpy as np

try:  # pyworld's __init__ imports the removed `pkg_resources` only to read its version string
    import pkg_resources  # noqa: F401
except ImportError:  # pragma: no cover - environment dependent
    _shim = types.ModuleType("pkg_resources")

    class _Dist:
        version = "0"

    _shim.get_distribution = lambda _name: _Dist()  # type: ignore[attr-defined]
    _shim.DistributionNotFound = Exception  # type: ignore[attr-defined]
    sys.modules["pkg_resources"] = _shim

try:
    import pyworld as _pw
except Exception:  # pragma: no cover
    _pw = None

FRAME_MS = 5.0

# Contour shapes over normalized voiced time x in [0, 1] -> semitone offset for depth = 1.
SHAPES: dict[str, Callable[[np.ndarray], np.ndarray]] = {
    "flat": np.zeros_like,
    "rise": lambda x: x**1.5,
    "fall": lambda x: -(x**1.2),
    "rise_fall": lambda x: np.sin(np.pi * x) * 1.0 - 0.3 * x,
    "fall_rise": lambda x: -np.sin(np.pi * x) * 0.8 + 0.5 * x**2,
    "question": lambda x: -0.25 * np.sin(np.pi * np.clip(x * 1.6, 0, 1)) + np.clip((x - 0.45) / 0.55, 0, 1) ** 1.6,
    "step_down": lambda x: np.where(x < 0.5, 0.35, -0.65),  # "uh-oh"
    "wobble": lambda x: 0.6 * np.sin(2 * np.pi * 2.5 * x) * (1 - 0.5 * x),
    "droop": lambda x: -0.2 * x - 0.8 * x**3,  # sad / tired tail
}

available = _pw is not None


def _track(x: np.ndarray, sr: int) -> tuple[np.ndarray, np.ndarray]:
    """F0 tracking (dio + stonemask: ~3x faster than harvest; outliers are clamped by the callers)."""
    f0, t = _pw.dio(x, sr, frame_period=FRAME_MS, f0_floor=60.0, f0_ceil=900.0)
    return _pw.stonemask(x, f0, t, sr), t


def apply(
    audio: np.ndarray,
    sr: int,
    shape: str = "flat",
    depth_st: float = 0.0,
    vowel_stretch: float = 1.0,
    flatten: float = 0.0,
) -> np.ndarray:
    """Return audio with `shape` (scaled to `depth_st` semitones) drawn over its voiced part and vowels stretched.

    `flatten` (0..1) first squashes the TTS model's own melody toward its mean so the drawn contour dominates.
    """
    if _pw is None or audio.size < sr // 20 or (abs(depth_st) < 0.05 and abs(vowel_stretch - 1.0) < 0.02 and flatten <= 0):
        return audio
    x = audio.astype(np.float64)
    f0, t = _track(x, sr)
    voiced = f0 > 0
    if voiced.sum() < 4:
        return audio
    sp = _pw.cheaptrick(x, f0, t, sr)
    ap = _pw.d4c(x, f0, t, sr)

    # Contour over the voiced span (positions measured in voiced-frame time, so consonants don't skew it).
    vi = np.flatnonzero(voiced)
    pos = np.zeros(f0.size)
    pos[vi[0] : vi[-1] + 1] = np.linspace(0.0, 1.0, vi[-1] - vi[0] + 1)
    pos[vi[-1] + 1 :] = 1.0
    if flatten > 0:
        lf = np.log(f0[voiced])
        m = float(np.median(lf))
        lf = np.clip(lf, m - 0.35, m + 0.35)  # tame word-final creak / octave slips (> ~6 semitones off)
        f0[voiced] = np.exp(m + (1.0 - min(1.0, flatten)) * (lf - m))
    semis = SHAPES.get(shape, SHAPES["flat"])(pos) * depth_st
    f0 = np.where(voiced, f0 * 2.0 ** (semis / 12.0), 0.0)

    # Vowel stretch: repeat voiced frames, keep unvoiced (consonant) frames at 1x.
    if abs(vowel_stretch - 1.0) >= 0.02:
        rate = np.where(voiced, vowel_stretch, 1.0)
        acc = np.cumsum(rate)
        n_out = int(acc[-1])
        idx = np.clip(np.searchsorted(acc, np.arange(n_out) + 0.5), 0, f0.size - 1)
        f0, sp, ap = f0[idx], sp[idx], ap[idx]

    y = _pw.synthesize(np.ascontiguousarray(f0), np.ascontiguousarray(sp), np.ascontiguousarray(ap), sr, FRAME_MS)
    peak_in, peak_out = float(np.abs(x).max()), float(np.abs(y).max()) or 1.0
    return (y * (peak_in / peak_out)).astype(np.float32)


def perform(
    audio: np.ndarray,
    sr: int,
    bounds: list[int],
    offsets_st: list[float],
    shapes: list[str],
    depths: list[float],
    flattens: list[float],
    stretches: list[float],
) -> tuple[np.ndarray, list[int]]:
    """One WORLD pass over a whole sentence with per-word melody.

    `bounds` are the sample positions where words 1..n-1 start (word 0 starts at 0). Each word gets a pitch
    offset, a contour shape*depth over its own voiced frames, partial flattening of the TTS melody toward the
    sentence median, and a vowel stretch. Returns (audio, new word-start samples after stretching).
    """
    n_words = len(offsets_st)
    if _pw is None or audio.size < sr // 20:
        return audio, list(bounds)
    x = audio.astype(np.float64)
    f0, t = _track(x, sr)
    voiced = f0 > 0
    if voiced.sum() < 4:
        return audio, list(bounds)
    sp = _pw.cheaptrick(x, f0, t, sr)
    ap = _pw.d4c(x, f0, t, sr)

    hop = sr * FRAME_MS / 1000.0
    start_frames = [0] + [int(round(b / hop)) for b in bounds]
    seg = np.clip(np.searchsorted(np.array(start_frames[1:]), np.arange(f0.size), side="right"), 0, n_words - 1)
    m = float(np.median(np.log(f0[voiced])))
    new_f0 = f0.copy()
    rate = np.ones(f0.size)
    for w in range(n_words):
        sel = (seg == w) & voiced
        if not sel.any():
            continue
        idx = np.flatnonzero(sel)
        lf = np.clip(np.log(f0[idx]), m - 0.35, m + 0.35)
        lf = m + (1.0 - min(1.0, flattens[w])) * (lf - m) + (lf.mean() - m) * min(1.0, flattens[w]) * 0.5
        pos = np.linspace(0.0, 1.0, idx.size)
        semis = offsets_st[w] + SHAPES.get(shapes[w], SHAPES["flat"])(pos) * depths[w]
        new_f0[idx] = np.exp(lf) * 2.0 ** (semis / 12.0)
        if abs(stretches[w] - 1.0) >= 0.02:
            rate[idx] = stretches[w]
    acc = np.cumsum(rate)
    out_idx = np.clip(np.searchsorted(acc, np.arange(int(acc[-1])) + 0.5), 0, f0.size - 1)
    y = _pw.synthesize(
        np.ascontiguousarray(new_f0[out_idx]), np.ascontiguousarray(sp[out_idx]), np.ascontiguousarray(ap[out_idx]), sr, FRAME_MS
    )
    y *= float(np.abs(x).max()) / (float(np.abs(y).max()) or 1.0)
    # Map word starts through the stretch (frame f starts at output frame acc[f-1]).
    new_bounds = [int(round((acc[f - 1] if f > 0 else 0.0) * hop)) for f in start_frames[1:]]
    return y.astype(np.float32), new_bounds
