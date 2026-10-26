"""Style C UI sound layer, synthesised from a cue plan (deterministic, seeded, no downloads).

Ported from the canonical look-C generator: the same key
clicks, line ticks, whooshes, pentatonic task ticks, button click, ascending check chimes
(the last one detuned) and the low tension drone. The cue *times* are no longer hard-coded:
they come from the timeline that styles/C/build.py derives from the words file.

The output is a raw layer (float peak 0.5, 48 kHz stereo 16-bit). Its level against the
voice is set afterwards by kit/sfx_bus.py, which owns the loudness rule.
"""
from __future__ import annotations

import wave
from pathlib import Path

import numpy as np

SR = 48000
PENTA = (880, 988, 1109, 1319, 1480, 1760)
CHIME = (1319, 1480, 1661, 1760, 1976, 2093)


class _Bus:
    def __init__(self, dur: float, seed: int = 417):
        self.n = int(round(SR * dur))
        self.L = np.zeros(self.n)
        self.R = np.zeros(self.n)
        self.rng = np.random.default_rng(seed)

    def place(self, sig, t, pan=0.0, gain=1.0):
        i = int(t * SR)
        if i >= self.n or i < 0:
            return
        j = min(self.n, i + len(sig))
        s = sig[: j - i] * gain
        self.L[i:j] += s * np.sqrt(0.5 * (1 - pan))
        self.R[i:j] += s * np.sqrt(0.5 * (1 + pan))


def _env(n, a, d):
    k = np.arange(n) / SR
    return np.minimum(1, k / max(a, 1e-4)) * np.exp(-k / d)


def _lp(x, a):
    y = np.zeros_like(x)
    acc = 0.0
    for i, v in enumerate(x):
        acc += a * (v - acc)
        y[i] = acc
    return y


def _key_click(rng, var):
    n = int(0.035 * SR)
    noise = rng.standard_normal(n)
    bp = _lp(noise, 0.55) - _lp(noise, 0.08)
    body = np.sin(2 * np.pi * (1800 + 260 * var) * np.arange(n) / SR) * 0.25
    return (bp + body) * _env(n, 0.0005, 0.006)


def _tick(freq, dec=0.05, n_s=0.12):
    n = int(n_s * SR)
    t = np.arange(n) / SR
    s = np.sin(2 * np.pi * freq * t) + 0.35 * np.sin(2 * np.pi * freq * 2.01 * t)
    return s * _env(n, 0.001, dec)


def _chime(freq):
    n = int(0.6 * SR)
    t = np.arange(n) / SR
    s = (np.sin(2 * np.pi * freq * t) + 0.5 * np.sin(2 * np.pi * freq * 1.5 * t) * np.exp(-t / 0.08)
         + 0.25 * np.sin(2 * np.pi * freq * 3 * t) * np.exp(-t / 0.04))
    return s * _env(n, 0.002, 0.16)


def _whoosh(rng, length, peak_frac=0.55):
    n = int(length * SR)
    noise = rng.standard_normal(n)
    y = np.zeros(n)
    acc = 0.0
    for i in range(n):
        p = i / n
        a = 0.02 + 0.25 * np.exp(-((p - peak_frac) ** 2) / 0.04)
        acc += a * (noise[i] - acc)
        y[i] = acc
    shape = np.exp(-((np.linspace(0, 1, n) - peak_frac) ** 2) / 0.05)
    return y * shape


def _drone(length):
    n = int(length * SR)
    t = np.arange(n) / SR
    s = 0.6 * np.sin(2 * np.pi * 55 * t) + 0.4 * np.sin(2 * np.pi * 58.3 * t) + 0.2 * np.sin(2 * np.pi * 110.6 * t)
    fade_in = np.minimum(1, t / 0.25)
    fade_out = np.minimum(1, (length - t) / 0.12)
    return s * fade_in * np.clip(fade_out, 0, 1)


def _mix(*sigs):
    out = np.zeros(max(len(x) for x in sigs))
    for x in sigs:
        out[: len(x)] += x
    return out


def render(timeline: dict, dst: str | Path) -> Path:
    """Write the raw UI layer for `timeline` (see build.plan_timeline) to dst."""
    T = timeline
    bus = _Bus(T["END"])
    rng = bus.rng
    # terminal: a key click per typed character (capped), a line tick per streamed line
    for k, t in enumerate(T["keys"]):
        bus.place(_key_click(rng, (k * 37 % 11) / 11), t, pan=-0.2 + 0.4 * ((k * 13) % 7) / 7, gain=0.55)
    for t in T["lineTicks"]:
        bus.place(_tick(2600, 0.012, 0.04), t, gain=0.25)
    # flights
    bus.place(_whoosh(rng, 1.0), T["leg1"][0] - 0.04, gain=0.9)
    bus.place(_whoosh(rng, 0.55, 0.5), T["leg2"][0] - 0.04, gain=0.9)
    # task ticks (pentatonic rise)
    for k, t in enumerate(T["ticks"]):
        bus.place(_tick(PENTA[k % len(PENTA)], 0.06, 0.18), t, gain=0.5)
    # button press / release
    bus.place(_mix(_key_click(rng, 0.9) * 1.4, _tick(300, 0.02, 0.06) * 0.6), T["press"], gain=0.9)
    bus.place(_tick(1200, 0.01, 0.04), T["release"], gain=0.4)
    # checks: ascending chimes, the last one detuned (the tension cue)
    n = len(T["checks"])
    for k, t in enumerate(T["checks"]):
        f = CHIME[min(k, len(CHIME) - 1)]
        bus.place(_chime(f * 0.972 if k == n - 1 else f), t, pan=-0.3 + 0.12 * min(k, 5), gain=0.32)
    # tension drone from the rack focus to the end-card cut
    if T["ecStart"] - T["tension"] > 0.2:
        bus.place(_drone(T["ecStart"] - T["tension"]), T["tension"], gain=0.45)
    # end-card sting: the low button thunk plus a clean (in-tune) chime
    bus.place(_mix(_tick(300, 0.03, 0.1) * 0.8, _chime(1319) * 0.6), T["ecStart"], gain=0.6)

    mix = np.stack([bus.L, bus.R], 1)
    tail = int(0.04 * SR)
    mix[-tail:] *= np.linspace(1, 0, tail)[:, None]
    peak = float(np.max(np.abs(mix)))
    if peak > 0:
        mix *= 0.5 / peak
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    pcm = (np.clip(mix, -1, 1) * 32767).astype("<i2")
    with wave.open(str(dst), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())
    return dst
