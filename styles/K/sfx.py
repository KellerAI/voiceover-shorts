"""Style K16 arcade sound layer, synthesised from the plan (deterministic, seeded, no downloads).

Ported from the canonical K16 generator: the same decaying
square waves with pitch slides, chunky sample-and-hold noise and a 0.12 s echo tap. The cue *times*
are no longer hard-coded S49 times: they come from the timeline styles/K/build.py derives from the
words file (coin settle, stage jingle, headline slams, walk steps, twin drop, gag stamp and boil,
negation boom, key-word hits, ? block bump, power-up arpeggio, end-card coin).

The output is a raw layer (float peak 0.5, 48 kHz stereo 16-bit) as long as the composition. Its
level against the voice is set afterwards by kit/sfx_bus.py, which owns the loudness rule.
"""
from __future__ import annotations

import wave
from pathlib import Path

import numpy as np

SR = 48000
RAW_PEAK = 0.5


class _Bus:
    def __init__(self, dur: float, seed: int = 16):
        self.n = int(round(SR * dur))
        self.out = np.zeros(self.n)
        self.rng = np.random.default_rng(seed)

    def add(self, t: float, sig: np.ndarray, echo: bool = True):
        for e, g in (((0.0, 1.0), (0.12, 0.35)) if echo else ((0.0, 1.0),)):
            i = int(round((t + e) * SR))
            if i < 0 or i >= self.n:
                continue
            j = min(self.n, i + len(sig))
            self.out[i:j] += g * sig[: j - i]


def sq(f, d, v=1.0, duty=0.5, slide=0.0):
    t = np.arange(int(d * SR)) / SR
    fr = f * (1 + slide * t / d)
    ph = np.cumsum(fr) / SR
    return v * np.where((ph % 1) < duty, 1.0, -1.0) * np.exp(-t * 3 / d)


def noise(rng, d, v=1.0):
    n = int(d * SR)
    s = np.repeat(rng.uniform(-1, 1, n // 6 + 1), 6)[:n]
    return v * s * np.exp(-np.arange(n) / SR * 8 / d)


def cues(T: dict) -> list[tuple[float, str]]:
    """(time, cue name) pairs, in time order; exposed for tests."""
    out = [(T["coinSettle"] - 0.08, "coin"), (T["stageStart"], "jingle")]
    hl = T["headline"]
    for i, h in enumerate(hl):
        out.append((h["s"], "slam" if i == len(hl) - 1 else "blip"))
    out.append((T["levelStart"], "whoosh"))
    w0, w1 = T["walk"]
    out += [(float(x), "step") for x in np.arange(w0, w1, 0.111)]
    out += [(T["twinDrop"], "drop"), (T["twinLand"], "land")]
    if T.get("gag"):
        out += [(T["gag"][0], "stamp"), (T["gagSloppy"], "boil")]
    if T.get("neg"):
        out.append((T["neg"]["t"], "boom"))
    for h in T["hero"]:
        if h["kind"] in ("line", "power"):
            out.append((h["words"][h["key"]]["s"], "hit"))
    out += [(T["bump"], "bump"), (T["powerOn"], "powerup"), (T["ecStart"], "endcoin")]
    return sorted((round(t, 4), n) for t, n in out if 0 <= t < T["END"])


def render(T: dict, dst) -> Path:
    bus = _Bus(T["END"])
    rng = bus.rng
    for t, name in cues(T):
        if name == "coin":
            bus.add(t, sq(988, 0.08))
            bus.add(t + 0.08, sq(1319, 0.35))
        elif name == "jingle":
            for i, f in enumerate((523, 659, 784, 1047)):
                bus.add(t + i * 0.07, sq(f, 0.1, 0.6))
        elif name == "blip":
            bus.add(t, sq(784, 0.08, 0.6))
            bus.add(t + 0.06, sq(1047, 0.1, 0.5))
        elif name == "slam":
            bus.add(t, noise(rng, 0.5))
            bus.add(t, sq(80, 0.4, 1, slide=-0.5))
        elif name == "whoosh":
            bus.add(t, sq(200, 0.25, 0.5, slide=3))
        elif name == "step":
            bus.add(t, noise(rng, 0.03, 0.25), echo=False)
        elif name == "drop":
            bus.add(t, sq(900, 0.36, 0.5, slide=-0.8))
        elif name == "land":
            bus.add(t, noise(rng, 0.12, 0.6))
        elif name == "stamp":
            bus.add(t, sq(1568, 0.06, 0.6))
            bus.add(t + 0.08, sq(2093, 0.08, 0.6))
        elif name == "boil":
            bus.add(t, sq(300, 0.4, 0.4, duty=0.2, slide=-0.6))
        elif name == "boom":
            bus.add(t, noise(rng, 0.6))
            bus.add(t, sq(55, 0.5, 1, slide=-0.4))
            bus.add(t + 0.12, sq(600, 0.6, 0.5, slide=-0.85))
        elif name == "hit":
            bus.add(t, sq(110, 0.25, 0.8, slide=-0.4))
        elif name == "bump":
            bus.add(t, sq(330, 0.1, 0.7))
            bus.add(t + 0.1, sq(440, 0.3, 0.5, slide=1))
        elif name == "powerup":
            for i, f in enumerate((523, 659, 784, 1047, 1319, 1568, 2093)):
                bus.add(t + i * 0.05, sq(f, 0.12, 0.6))
        elif name == "endcoin":
            bus.add(t, sq(988, 0.08, 0.6))
            bus.add(t + 0.08, sq(1319, 0.3, 0.6))
    out = bus.out
    pk = float(np.max(np.abs(out))) or 1.0
    out = out / pk * RAW_PEAK
    st = np.stack([out, out], 1)
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(dst), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((st * 32767).astype("<i2").tobytes())
    return dst
