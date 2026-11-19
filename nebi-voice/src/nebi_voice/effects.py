"""DSP building blocks for small-robot voices (numpy + scipy only, mono float32).

The classic recipe for a small-robot voice: analyse a human voice, resynthesize it with a raised pitch
and a smaller vocal tract, and play the inflection like an instrument. Then make the result sound like
it comes out of a small, boxy machine.
"""

from __future__ import annotations

import math
from fractions import Fraction

import numpy as np
from scipy import signal


def formant_scale(x: np.ndarray, factor: float) -> np.ndarray:
    """Resample so that playback at the same rate scales every frequency (pitch AND formants) by `factor`.

    The voice engine pre-compensates pitch and duration, so the net effect is a pure formant shift:
    a smaller "throat" without phase-vocoder smearing.
    """
    if abs(factor - 1.0) < 1e-3 or x.size == 0:
        return x
    fr = Fraction(factor).limit_denominator(64)
    return signal.resample_poly(x, fr.denominator, fr.numerator).astype(np.float32)


def vibrato(x: np.ndarray, sr: int, rate_hz: float = 6.0, depth_cents: float = 25.0, drift: float = 0.5, seed: int = 0) -> np.ndarray:
    """Pitch wobble through a modulated fractional delay (sine LFO + slow random drift: 'servo jitter')."""
    if depth_cents <= 0 or x.size == 0:
        return x
    rng = np.random.default_rng(seed)
    n = x.size
    t = np.arange(n) / sr
    lfo = np.sin(2 * np.pi * rate_hz * t + rng.uniform(0, 2 * np.pi))
    if drift > 0:
        knots = rng.standard_normal(int(t[-1] * 4) + 3)
        lfo = (1 - drift) * lfo + drift * np.interp(t, np.linspace(0, t[-1] + 1e-9, knots.size), knots).clip(-1.5, 1.5)
    # Pitch ratio r(t) = 2^(cents/1200); delay derivative = 1 - r.
    ratio = 2.0 ** (depth_cents * lfo / 1200.0)
    read_pos = np.cumsum(ratio) - ratio[0]
    read_pos *= (n - 1) / max(read_pos[-1], 1.0)
    return np.interp(read_pos, np.arange(n), x).astype(np.float32)


def tremolo(x: np.ndarray, sr: int, rate_hz: float = 9.0, depth: float = 0.1) -> np.ndarray:
    if depth <= 0:
        return x
    t = np.arange(x.size) / sr
    return (x * (1.0 - depth * 0.5 * (1 + np.sin(2 * np.pi * rate_hz * t)))).astype(np.float32)


def ring_mod(x: np.ndarray, sr: int, freq_hz: float = 40.0, mix: float = 0.15) -> np.ndarray:
    """Metallic sidebands; low mix keeps words intelligible."""
    if mix <= 0:
        return x
    t = np.arange(x.size) / sr
    return ((1 - mix) * x + mix * x * np.sin(2 * np.pi * freq_hz * t)).astype(np.float32)


def box_resonance(x: np.ndarray, sr: int, delay_ms: float = 2.6, feedback: float = 0.35, mix: float = 0.3) -> np.ndarray:
    """Feedback comb filter: the hollow 'talking inside a metal box' coloration."""
    if mix <= 0:
        return x
    d = max(1, int(sr * delay_ms / 1000))
    a = np.zeros(d + 1)
    a[0], a[d] = 1.0, -feedback
    wet = signal.lfilter([1.0 - feedback], a, x)
    return ((1 - mix) * x + mix * wet).astype(np.float32)


def bitcrush(x: np.ndarray, bits: int = 10, hold: int = 2, mix: float = 0.25) -> np.ndarray:
    """Lo-fi digital grain: sample-and-hold decimation + amplitude quantization."""
    if mix <= 0:
        return x
    y = np.repeat(x[::hold], hold)[: x.size] if hold > 1 else x.copy()
    q = 2 ** (bits - 1)
    y = np.round(y * q) / q
    return ((1 - mix) * x + mix * y).astype(np.float32)


def _peaking(sr: int, f0: float, gain_db: float, q: float) -> tuple[np.ndarray, np.ndarray]:
    A = 10 ** (gain_db / 40)
    w = 2 * np.pi * f0 / sr
    alpha = np.sin(w) / (2 * q)
    b = np.array([1 + alpha * A, -2 * np.cos(w), 1 - alpha * A])
    a = np.array([1 + alpha / A, -2 * np.cos(w), 1 - alpha / A])
    return b / a[0], a / a[0]


def small_speaker(x: np.ndarray, sr: int, low_hz: float = 260.0, high_hz: float = 6500.0, presence_hz: float = 2200.0, presence_db: float = 4.0) -> np.ndarray:
    """Band-limit like a tiny robot loudspeaker and add a presence bump."""
    y = x
    if low_hz > 0:
        y = signal.sosfilt(signal.butter(2, low_hz, "highpass", fs=sr, output="sos"), y)
    if high_hz and high_hz < sr / 2:
        y = signal.sosfilt(signal.butter(4, high_hz, "lowpass", fs=sr, output="sos"), y)
    if presence_db:
        b, a = _peaking(sr, presence_hz, presence_db, 1.0)
        y = signal.lfilter(b, a, y)
    return y.astype(np.float32)


def saturate(x: np.ndarray, drive: float = 1.5) -> np.ndarray:
    if drive <= 1.0:
        return x
    return (np.tanh(drive * x) / math.tanh(drive)).astype(np.float32)


def normalize(x: np.ndarray, peak: float = 0.9) -> np.ndarray:
    m = float(np.abs(x).max()) if x.size else 0.0
    return (x * (peak / m)).astype(np.float32) if m > 1e-6 else x


def fade(x: np.ndarray, sr: int, ms: float = 6.0) -> np.ndarray:
    n = min(x.size // 2, int(sr * ms / 1000))
    if n > 0:
        ramp = np.linspace(0, 1, n, dtype=np.float32)
        x = x.copy()
        x[:n] *= ramp
        x[-n:] *= ramp[::-1]
    return x


def silence(sr: int, seconds: float) -> np.ndarray:
    return np.zeros(max(0, int(sr * seconds)), np.float32)


# ------------------------------------------------------------------ mechanical sound effects


def servo_whir(sr: int, seconds: float = 0.16, f_start: float = 700.0, f_end: float = 1100.0, level: float = 0.18, seed: int = 0) -> np.ndarray:
    """Small geared-motor whir: buzzy sawtooth glide + gear noise, band-passed, with a soft envelope."""
    rng = np.random.default_rng(seed)
    n = int(sr * seconds)
    t = np.arange(n) / sr
    f = np.linspace(f_start, f_end, n) * (1 + 0.01 * np.sin(2 * np.pi * 47 * t))
    phase = 2 * np.pi * np.cumsum(f) / sr
    saw = 2 * ((phase / (2 * np.pi)) % 1.0) - 1
    gear = rng.standard_normal(n) * (0.5 + 0.5 * np.sign(np.sin(2 * np.pi * 90 * t)))
    y = 0.7 * saw + 0.3 * gear
    y = signal.sosfilt(signal.butter(2, [400, 3500], "bandpass", fs=sr, output="sos"), y)
    env = np.sin(np.pi * np.linspace(0, 1, n)) ** 0.6
    return (level * normalize(y * env, 1.0)).astype(np.float32)


def click(sr: int, level: float = 0.25, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    n = int(sr * 0.012)
    y = rng.standard_normal(n) * np.exp(-np.linspace(0, 9, n))
    y = signal.sosfilt(signal.butter(2, 2500, "highpass", fs=sr, output="sos"), y)
    return (level * normalize(y, 1.0)).astype(np.float32)


def chirp_boop(sr: int, rising: bool = True, seconds: float = 0.12, level: float = 0.2) -> np.ndarray:
    """Tiny electronic 'boop' (power-up / acknowledge)."""
    n = int(sr * seconds)
    f = np.geomspace(500, 1400, n) if rising else np.geomspace(1400, 500, n)
    y = np.sin(2 * np.pi * np.cumsum(f) / sr) * np.sin(np.pi * np.linspace(0, 1, n))
    return (level * y).astype(np.float32)


def to_pcm16(x: np.ndarray, sr_in: int, sr_out: int = 48000) -> bytes:
    """Resample and pack as little-endian int16 PCM (e.g. 48 kHz mono for the Beni robot's intercom `send_pcm`)."""
    if sr_out != sr_in:
        g = math.gcd(sr_in, sr_out)
        x = signal.resample_poly(x, sr_out // g, sr_in // g)
    return (np.clip(x, -1.0, 1.0) * 32767).astype("<i2").tobytes()


# ------------------------------------------------------------------ mastering for a small robot speaker


def compress(x: np.ndarray, sr: int, threshold_db: float = -20.0, ratio: float = 3.0, attack_ms: float = 5.0, release_ms: float = 80.0) -> np.ndarray:
    """Feed-forward RMS compressor: evens out loud/quiet syllables so everything cuts through motor noise."""
    if ratio <= 1.0 or x.size == 0:
        return x
    a_att = math.exp(-1.0 / (sr * attack_ms / 1000))
    a_rel = math.exp(-1.0 / (sr * release_ms / 1000))
    p = x.astype(np.float64) ** 2
    # One-pole envelope with separate attack/release (vectorised in blocks via lfilter on two passes).
    env_att = signal.lfilter([1 - a_att], [1, -a_att], p)
    env_rel = signal.lfilter([1 - a_rel], [1, -a_rel], p)
    env = np.maximum(env_att, env_rel)
    lvl_db = 10 * np.log10(env + 1e-10)
    over = np.maximum(0.0, lvl_db - threshold_db)
    gain_db = -over * (1 - 1 / ratio)
    return (x * 10 ** (gain_db / 20)).astype(np.float32)


def loudness_normalize(x: np.ndarray, target_dbfs: float = -16.0, gate_db: float = -45.0) -> np.ndarray:
    """Scale so the RMS of the *speech* (gated, silence ignored) hits `target_dbfs`."""
    if x.size == 0:
        return x
    n = 480
    frames = x[: x.size // n * n].reshape(-1, n) if x.size >= n else x.reshape(1, -1)
    rms = np.sqrt((frames.astype(np.float64) ** 2).mean(axis=1) + 1e-12)
    db = 20 * np.log10(rms)
    active = rms[db > gate_db]
    if active.size == 0:
        return x
    cur = 20 * np.log10(np.sqrt((active**2).mean()))
    return (x * 10 ** ((target_dbfs - cur) / 20)).astype(np.float32)


def limit(x: np.ndarray, ceiling: float = 0.89) -> np.ndarray:
    """Soft-knee peak limiter (tanh above 70% of ceiling)."""
    knee = 0.7 * ceiling
    y = x.astype(np.float64).copy()
    m = np.abs(y) > knee
    y[m] = np.sign(y[m]) * (knee + (ceiling - knee) * np.tanh((np.abs(y[m]) - knee) / (ceiling - knee)))
    return y.astype(np.float32)


def master(x: np.ndarray, sr: int, volume: float = 1.0, target_dbfs: float = -16.0, fade_in_ms: float = 15.0) -> np.ndarray:
    """Compress -> loudness-normalize -> volume -> limit -> gentle fade-in (never startle anyone)."""
    y = compress(x, sr)
    y = loudness_normalize(y, target_dbfs)
    y = limit(y * max(0.0, min(1.5, volume)))
    n = min(y.size, int(sr * fade_in_ms / 1000))
    if n > 0:
        y[:n] *= np.linspace(0, 1, n, dtype=np.float32) ** 2
    return y


# ------------------------------------------------------------------ body-sync analysis


def envelope(x: np.ndarray, sr: int, rate_hz: float = 25.0) -> np.ndarray:
    """Loudness envelope (0..1) sampled at `rate_hz` - drive LED eyes / mouth lights with it."""
    hop = max(1, int(sr / rate_hz))
    if x.size < hop:
        return np.zeros(0, np.float32)
    frames = x[: x.size // hop * hop].reshape(-1, hop)
    rms = np.sqrt((frames.astype(np.float64) ** 2).mean(axis=1))
    db = 20 * np.log10(rms + 1e-9)
    e = np.clip((db + 45.0) / 40.0, 0.0, 1.0)  # -45 dB -> 0, -5 dB -> 1
    return e.astype(np.float32)


def beats(env: np.ndarray, rate_hz: float = 25.0, min_gap_s: float = 0.16, threshold: float = 0.45) -> list[tuple[float, float]]:
    """Syllable nuclei as (time_s, strength) - local envelope peaks; schedule nods/eye blinks on them."""
    out: list[tuple[float, float]] = []
    last = -1e9
    for i in range(1, env.size - 1):
        if env[i] >= threshold and env[i] >= env[i - 1] and env[i] > env[i + 1]:
            t = i / rate_hz
            if t - last >= min_gap_s:
                out.append((round(t, 3), round(float(env[i]), 2)))
                last = t
    return out
