"""Style E UI + kinetic sound layer, synthesised from the beat plan (deterministic, seeded, no samples).

Ported from the canonical look-E generator: key clicks, line
ticks, the slam thuds, the cartoon boings + stamp of the sloppy crash, whooshes on the flights,
pentatonic task ticks, the button click, ascending check chimes (the last one detuned), the low
tension drone, the "worse" thud and the slightly-off closing chime. The cue *times* are no longer
hard-coded: they come from the plan styles/E/build.py derives from the words file.

The output is a raw layer (float peak 0.5, 48 kHz stereo 16-bit). Unlike the canonical generator it
does NOT normalise itself against the voice: kit/sfx_bus.py owns the level (SFX 14 dB under).
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
    return s * np.minimum(1, t / 0.25) * np.clip((length - t) / 0.12, 0, 1)


def _thud(f0=70, dec=0.18):
    n = int(0.5 * SR)
    t = np.arange(n) / SR
    f = f0 * (1 + 1.5 * np.exp(-t / 0.03))
    return np.sin(2 * np.pi * np.cumsum(f) / SR) * _env(n, 0.001, dec)


def _boing(t_len=0.7):
    n = int(t_len * SR)
    t = np.arange(n) / SR
    f = 220 + 140 * np.sin(2 * np.pi * 11 * t) * np.exp(-t / 0.35) + 60 * t
    ph = 2 * np.pi * np.cumsum(f) / SR
    s = np.tanh(3 * np.sin(ph)) * 0.6 + 0.4 * np.tanh(3 * np.sin(ph * 1.013))
    return s * _env(n, 0.003, 0.28)


def _mix(*sigs):
    out = np.zeros(max(len(x) for x in sigs))
    for x in sigs:
        out[: len(x)] += x
    return out


def render(T: dict, dst: str | Path) -> Path:
    """Write the raw E sound layer for the beat plan `T` (build.plan_timeline()["T"]) to dst."""
    bus = _Bus(T["END"])
    rng = bus.rng
    for k, t in enumerate(T["keys"]):  # one click per typed character (capped by the planner)
        bus.place(_key_click(rng, (k * 37 % 11) / 11), t, pan=-0.2 + 0.4 * ((k * 13) % 7) / 7, gain=0.55)
    for t in T["lineTicks"]:
        bus.place(_tick(2600, 0.012, 0.04), t, gain=0.22)
    bus.place(_thud(), T["fail"], gain=1.0)                                  # condensed slam
    bus.place(_boing(), T["slop"], pan=-0.3, gain=0.9)                       # sloppy crash
    bus.place(_thud(55, 0.12), T["slop"] + 0.02, gain=0.8)
    sd = T["slopOut"] - T["slop"]
    bus.place(_boing(0.5), T["slop"] + min(0.40, sd * 0.45), pan=0.35, gain=0.7)
    bus.place(_mix(_key_click(rng, 0.9) * 2, _thud(140, 0.05)), T["slop"] + min(0.55, sd * 0.6), gain=0.7)  # stamp
    bus.place(_whoosh(rng, 0.8), T["slopOut"], gain=0.9)                     # slop falls / leg 1
    bus.place(_thud(80, 0.2), T["rig"] - 0.02, gain=1.0)                     # act-2 slam
    for k, t in enumerate(T["ticks"]):
        bus.place(_tick(PENTA[k % len(PENTA)], 0.06, 0.18), t, gain=0.5)
    bus.place(_mix(_key_click(rng, 0.9) * 1.4, _tick(300, 0.02, 0.06) * 0.6), T["press"], gain=0.9)
    bus.place(_tick(1200, 0.01, 0.04), T["release"], gain=0.4)
    bus.place(_whoosh(rng, 0.55, 0.5), T["leg2"][0] - 0.04, gain=0.9)
    n = len(T["checks"])
    for k, t in enumerate(T["checks"]):
        f = CHIME[min(k, len(CHIME) - 1)]
        bus.place(_chime(f * 0.972 if k == n - 1 else f), t, pan=-0.3 + 0.12 * min(k, 5), gain=0.32)
    if T["ecStart"] - T["tension"] > 0.2:
        bus.place(_drone(T["ecStart"] - T["tension"]), T["tension"], gain=0.45)
    bus.place(_thud(48, 0.4), T["worse"], gain=0.9)                         # orange word
    bus.place(_chime(1319 * 0.97), T["end"] + 0.1, gain=0.3)                # closing caps, slightly off
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
