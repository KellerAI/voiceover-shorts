#!/usr/bin/env python3
"""Style B (Cinematic depth) sound design, parameterised for any script length.

Adapted from the canonical look's sfx generator. The canonical bed was authored on the
10.16 s world clock of dir-B/index.html; here every world event time is mapped onto real time
with the same warp the template uses (real = world * card_start / WORLD_DUR), so the whooshes,
node ticks, check pings and the "passes" thump stay locked to the picture. The drone runs on to
the end of the end-card hold and a low cut hit marks the end-card hard cut.

Usage: python3 sfx.py OUT.wav --card-start S --end S
Writes 48 kHz stereo 16-bit, deterministic (seeded). Level is set later by kit/sfx_bus.py.
Exit 0 on success, 2 on a usage error.
"""
from __future__ import annotations

import argparse
import sys
import wave

import numpy as np

SR = 48000
WORLD_DUR = 10.16  # must match DUR in template.html
LIT_T = [5.6 + k * 0.13 for k in range(8)]      # world times, template.html LIT_T
CHECK_T = [9.06 + k * 0.125 for k in range(8)]  # world times, template.html CHECK_T
PENT = [0, 2, 4, 7, 9, 12, 14, 16]


def _one_pole(x: np.ndarray, a: float | np.ndarray) -> np.ndarray:
    """One-pole lowpass y[i] = a*y[i-1] + (1-a)*x[i]; `a` scalar or per-sample array."""
    y = np.empty_like(x)
    acc = 0.0
    if isinstance(a, np.ndarray):
        for i in range(len(x)):
            acc = a[i] * acc + (1.0 - a[i]) * x[i]
            y[i] = acc
    else:
        a_s = float(a)
        b = 1.0 - a_s
        for i in range(len(x)):
            acc = a_s * acc + b * x[i]
            y[i] = acc
    return y


def synth(card_start: float, end: float, seed: int = 61) -> np.ndarray:
    """Return an (N, 2) float array in roughly [-1, 1]."""
    if not (card_start > 0 and end > card_start):
        raise ValueError(f"need 0 < card_start < end (got {card_start}, {end})")
    k = card_start / WORLD_DUR

    def rt(world_t: float) -> float:
        return world_t * k

    n = int(round(SR * end))
    t = np.arange(n) / SR
    rng = np.random.default_rng(seed)

    def env(t0, attack, decay):
        x = t - t0
        return np.where(x < 0, 0.0, np.where(x < attack, x / max(attack, 1e-6),
                                             np.exp(-(x - attack) / decay)))

    def ramp(t0, t1, v0, v1):
        return np.clip((t - t0) / max(t1 - t0, 1e-6), 0, 1) * (v1 - v0) + v0

    L = np.zeros(n)
    R = np.zeros(n)

    # Drone: A1 + E2 + A2, slow breathing, swell into the run, unease Bb1 creeping in at the end.
    amp = ramp(0, 1.6, 0.0, 1.0) * (0.85 + 0.15 * np.sin(2 * np.pi * 0.23 * t))
    amp *= 1 + ramp(rt(5.0), rt(6.6), 0, 0.35) + ramp(rt(8.8), card_start, 0, 0.4)
    drone = (np.sin(2 * np.pi * 55 * t) + 0.55 * np.sin(2 * np.pi * 82.41 * t + 0.7)
             + 0.3 * np.sin(2 * np.pi * 110.3 * t + 1.3) + 0.12 * np.sin(2 * np.pi * 164.8 * t))
    unease = ramp(rt(8.9), card_start, 0, 1.0) * (
        0.6 * np.sin(2 * np.pi * 58.27 * t)
        + 0.08 * np.sin(2 * np.pi * 1864.7 * t) * (0.6 + 0.4 * np.sin(2 * np.pi * 5.3 * t)))
    # Under the end card the unease resolves away and the drone settles.
    settle = ramp(card_start, end, 1.0, 0.45)
    unease *= ramp(card_start, card_start + 0.6, 1.0, 0.0)
    L += (0.22 * amp * drone + 0.2 * unease) * settle
    R += (0.22 * amp * drone * 0.97 + 0.2 * unease * 1.03) * settle

    # Air: dark filtered noise bed.
    air = _one_pole(rng.standard_normal(n), 0.985)
    air /= np.abs(air).max()
    L += 0.10 * air * ramp(0, 1.2, 0, 1)
    R += 0.10 * np.roll(air, 977) * ramp(0, 1.2, 0, 1)

    def whoosh(t0, dur, pan0, pan1, gain):
        n0, n1 = int(t0 * SR), min(n, int((t0 + dur) * SR))
        m = n1 - n0
        if m <= 0:
            return
        x = rng.standard_normal(m)
        a = 0.995 - 0.065 * np.sin(np.linspace(0, np.pi, m))  # cutoff opens mid-whoosh
        y = _one_pole(x, a)
        y = y - _one_pole(y, 0.999)  # strip rumble
        y /= np.abs(y).max() + 1e-9
        e = np.sin(np.linspace(0, np.pi, m)) ** 2
        p = np.linspace(pan0, pan1, m)
        L[n0:n1] += gain * y * e * np.cos((p + 1) * np.pi / 4)
        R[n0:n1] += gain * y * e * np.sin((p + 1) * np.pi / 4)

    # Camera moves (world times 3.3 pan, 5.0 whip, 8.75 crane); length follows the warp, capped.
    whoosh(rt(3.3), min(1.1 * k, 2.0), 0.6, -0.7, 0.55)
    whoosh(rt(5.0), min(0.7 * k, 1.4), -0.8, 0.3, 0.6)
    whoosh(rt(8.75), min(0.9 * k, 1.8), -0.2, 0.7, 0.7)

    # Node-light ticks.
    for i, wt in enumerate(LIT_T):
        t0 = rt(wt)
        f = 659.25 * 2 ** (PENT[i] / 12)
        s = 0.10 * env(t0, 0.003, 0.09) * np.sin(2 * np.pi * f * (t - t0))
        pan = -0.6 + i * 0.17
        L += s * np.cos((pan + 1) * np.pi / 4)
        R += s * np.sin((pan + 1) * np.pi / 4)

    # Check cascade glass pings.
    for i, wt in enumerate(CHECK_T):
        t0 = rt(wt)
        f = 1046.5 * 2 ** (PENT[i] / 12)
        e = env(t0, 0.002, 0.42)
        s = e * (np.sin(2 * np.pi * f * (t - t0))
                 + 0.35 * np.sin(2 * np.pi * f * 2.76 * (t - t0)) * env(t0, 0.002, 0.12)
                 + 0.15 * np.sin(2 * np.pi * f * 5.4 * (t - t0)) * env(t0, 0.002, 0.05))
        s *= 0.16
        pan = -0.7 + i * 0.2
        L += s * np.cos((pan + 1) * np.pi / 4)
        R += s * np.sin((pan + 1) * np.pi / 4)

    def thump(t0, gain, f0=38.0):
        x = t - t0
        ph = 2 * np.pi * np.cumsum(np.where(x < 0, 0, f0 + 50 * np.exp(-np.maximum(x, 0) / 0.05))) / SR
        return gain * env(t0, 0.004, 0.22) * np.sin(ph)

    th = thump(rt(9.04), 0.7) + thump(card_start, 0.55, 44.0)  # "passes" + end-card cut
    L += th
    R += th

    tail = ramp(end - 0.12, end, 1.0, 0.0)
    return np.stack([L * tail, R * tail], axis=1)


def write_wav(path: str, st: np.ndarray) -> None:
    peak = float(np.abs(st).max())
    if peak > 0.98:
        st = st * (0.98 / peak)
    pcm = (st * 32767).astype(np.int16)
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())


def main(argv: list[str] | None = None) -> int:
    doc_lines = (__doc__ or "").splitlines()
    ap = argparse.ArgumentParser(description=doc_lines[0] if doc_lines else None)
    ap.add_argument("out")
    ap.add_argument("--card-start", type=float, required=True)
    ap.add_argument("--end", type=float, required=True)
    a = ap.parse_args(argv)
    try:
        write_wav(a.out, synth(a.card_start, a.end))
    except (ValueError, OSError) as e:
        print(f"sfx.py: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
