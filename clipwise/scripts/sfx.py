#!/usr/bin/env python3
"""sfx.py - sound effects synthesised with numpy at render time (no audio files shipped).

  whoosh_fast  hook -> body transition (filtered-noise swell)
  whoosh_soft  a few jump cuts (darker, slower swell)
  thud_low     under the payoff line (pitch-dropping sine + click)
  pop          clip start when the clip opens on a moved hook
  tick         spare cue for custom edits

Users who prefer their own sounds pass --sfx-dir DIR to render.py; any file there named
<cue>.wav/.mp3/.flac/.ogg replaces the synthesised one.

  sfx.py OUT_DIR      writes every cue as OUT_DIR/<cue>.wav (to audition them)
"""
import math
import os
import sys
import wave

import numpy as np

SR = 48000
CUES = ("whoosh_fast", "whoosh_soft", "thud_low", "pop", "tick")


def _svf_bandpass(x, fc, q=0.7):
    """Chamberlin state-variable band-pass with a per-sample cutoff array."""
    low = band = 0.0
    out = np.empty_like(x)
    f = 2.0 * np.sin(np.pi * np.clip(fc, 20, SR / 6) / SR)
    for i in range(len(x)):
        low += f[i] * band
        high = x[i] - low - q * band
        band += f[i] * high
        out[i] = band
    return out


def _swell(n, peak_at):
    """Rise to 1.0 at peak_at (0..1 of n), then fall off faster: the shape of a pass-by."""
    t = np.linspace(0, 1, n)
    rise = np.clip(t / peak_at, 0, 1) ** 2.2
    fall = np.exp(-np.clip(t - peak_at, 0, None) / (1 - peak_at) * 5.0)
    return np.where(t <= peak_at, rise, fall)


def _norm(x, peak=0.9):
    m = float(np.abs(x).max()) or 1.0
    return (x / m * peak).astype(np.float32)


def whoosh_fast(seed=1):
    n = int(SR * 0.45)
    rng = np.random.default_rng(seed)
    t = np.linspace(0, 1, n)
    fc = 350 + 2600 * np.sin(np.pi * np.clip(t / 0.75, 0, 1)) ** 1.5  # sweep up then back down
    x = _svf_bandpass(rng.standard_normal(n), fc, q=0.9)
    return _norm(x * _swell(n, 0.62))


def whoosh_soft(seed=2):
    n = int(SR * 0.6)
    rng = np.random.default_rng(seed)
    t = np.linspace(0, 1, n)
    fc = 220 + 1100 * np.sin(np.pi * np.clip(t / 0.85, 0, 1))
    x = _svf_bandpass(rng.standard_normal(n), fc, q=1.2)
    x = np.convolve(x, np.ones(6) / 6, mode="same")  # gentle extra low-pass
    return _norm(x * _swell(n, 0.55))


def thud_low(seed=3):
    n = int(SR * 0.7)
    t = np.arange(n) / SR
    f = 42 + 55 * np.exp(-t / 0.06)                  # 97 Hz -> 42 Hz
    phase = 2 * np.pi * np.cumsum(f) / SR
    body = np.sin(phase) * np.exp(-t / 0.22)
    rng = np.random.default_rng(seed)
    click = rng.standard_normal(n) * np.exp(-t / 0.004)
    click = np.convolve(click, np.ones(12) / 12, mode="same")
    x = np.tanh(1.8 * (body + 0.35 * click))
    x[: int(SR * 0.002)] *= np.linspace(0, 1, int(SR * 0.002))
    return _norm(x)


def pop(seed=4):
    n = int(SR * 0.09)
    t = np.arange(n) / SR
    f = 380 + 720 * np.exp(-t / 0.012)
    x = np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t / 0.028)
    x[: int(SR * 0.001)] *= np.linspace(0, 1, int(SR * 0.001))
    return _norm(x)


def tick(seed=5):
    n = int(SR * 0.06)
    t = np.arange(n) / SR
    rng = np.random.default_rng(seed)
    x = (np.sin(2 * np.pi * 2200 * t) + 0.5 * np.sin(2 * np.pi * 3300 * t)
         + 0.15 * rng.standard_normal(n)) * np.exp(-t / 0.012)
    return _norm(x)


GEN = {"whoosh_fast": whoosh_fast, "whoosh_soft": whoosh_soft, "thud_low": thud_low, "pop": pop, "tick": tick}


def write_wav(path, x, sr=SR):
    pcm = (np.clip(x, -1, 1) * 32767).astype("<i2")
    st = np.repeat(pcm[:, None], 2, axis=1)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(st.tobytes())
    return str(path)


def synth_info(name, out_dir):
    """Synthesise cue `name` into out_dir and return {path, peak_db, peak_t, dur}."""
    x = GEN[name]()
    path = write_wav(os.path.join(out_dir, f"sfx_{name}.wav"), x)
    win = 480
    k = len(x) // win
    env = np.abs(x[: k * win]).reshape(k, win).max(axis=1)
    return {"path": path, "peak_db": 20 * math.log10(float(np.abs(x).max()) + 1e-9),
            "peak_t": float(np.argmax(env) * win / SR), "dur": len(x) / SR, "source": "synth"}


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    os.makedirs(sys.argv[1], exist_ok=True)
    for c in CUES:
        print(synth_info(c, sys.argv[1]))
