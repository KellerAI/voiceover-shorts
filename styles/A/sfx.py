"""Style A sound design, generated from the phrase plan (no downloaded assets, seeded).

Ported from the canonical look sfx generator; the event list is no longer hand-timed to one
script. Events come from build.plan_phrases() output:
  - a swept-noise whoosh on every layout rebuild (section exit),
  - a reverse swell + pitched sub hit on the "slam" treatments (flip, zoom) and on the last hero,
  - wooden knocks under each dropped letter (drop treatment),
  - a lock click (track-lock treatment), a tick cascade (check-field treatment).

The raw bus is written at an arbitrary level; kit/sfx_bus.py sets the final level against the voice.

Usage (module): render(plan, out_wav, seed=61)
"""
from __future__ import annotations

import wave

import numpy as np

SR = 48000


class _Bus:
    def __init__(self, duration: float, seed: int):
        self.n = int(round(SR * duration))
        self.out = np.zeros((self.n, 2))
        self.rng = np.random.default_rng(seed)

    def place(self, sig, t, pan=0.0, gain=1.0):
        i = int(max(0.0, t) * SR)
        if i >= self.n:
            return
        seg = sig[: self.n - i] * gain
        left, r = np.sqrt((1 - pan) / 2), np.sqrt((1 + pan) / 2)
        self.out[i:i + len(seg), 0] += seg * left * 1.414
        self.out[i:i + len(seg), 1] += seg * r * 1.414

    def whoosh(self, d=0.42, peak=0.7, f0=600, f1=4200, rev=False):
        n = int(d * SR)
        t = np.arange(n) / SR
        noise = self.rng.standard_normal(n)
        y = np.zeros(n)
        blk = 256
        lo_s = hi_s = 0.0
        for b in range(0, n, blk):
            p = b / n
            fc = f0 + (f1 - f0) * (np.sin(np.pi * p) if not rev else p)
            a_hi = np.exp(-2 * np.pi * fc / SR)
            a_lo = np.exp(-2 * np.pi * (fc * 0.35) / SR)
            for k in range(b, min(n, b + blk)):
                hi_s = (1 - a_hi) * noise[k] + a_hi * hi_s
                lo_s = (1 - a_lo) * noise[k] + a_lo * lo_s
                y[k] = hi_s - lo_s
        env = np.sin(np.pi * np.clip(t / d, 0, 1)) ** 2.2
        if rev:
            env = (t / d) ** 2.5 * (1 - np.clip((t - d * 0.92) / (d * 0.08), 0, 1))
        y = y * env
        return y / (np.abs(y).max() + 1e-9) * peak

    def sub_hit(self, d=0.9, f_hi=95, f_lo=38, peak=1.0):
        n = int(d * SR)
        t = np.arange(n) / SR
        f = f_lo + (f_hi - f_lo) * np.exp(-t * 18)
        ph = 2 * np.pi * np.cumsum(f) / SR
        body = np.sin(ph) * np.exp(-t * 4.2)
        click = self.rng.standard_normal(n) * np.exp(-t * 160) * 0.5
        a = np.exp(-2 * np.pi * 3000 / SR)
        s = 0.0
        for k in range(min(n, 4000)):  # click decays to nothing well inside 4000 samples
            s = (1 - a) * click[k] + a * s
            click[k] = s
        click[4000:] = 0.0
        y = body + click
        return y / np.abs(y).max() * peak

    def tick(self, f=2600, d=0.05, peak=0.5):
        n = int(d * SR)
        t = np.arange(n) / SR
        y = np.sin(2 * np.pi * f * t) * np.exp(-t * 110) + self.rng.standard_normal(n) * np.exp(-t * 400) * 0.25
        return y / np.abs(y).max() * peak

    def knock(self, f=420, d=0.12, peak=0.45):
        n = int(d * SR)
        t = np.arange(n) / SR
        y = np.sin(2 * np.pi * f * t * (1 - 0.3 * t)) * np.exp(-t * 45)
        return y / np.abs(y).max() * peak


def events(plan: dict) -> list[tuple]:
    """(kind, t, params) list derived from the phrase plan; exposed for tests."""
    ev = []
    phrases = plan["phrases"]
    words = plan["words"]
    for i, ph in enumerate(phrases):
        hero_t = words[ph["hero"]][1]
        hero_len = len(ph["hero_text"])
        k = ph["kind"]
        last = i == len(phrases) - 1
        if ph.get("exit") is not None:
            ev.append(("whoosh", ph["exit"] - 0.06, {}))
        if k in (1, 4) or last:
            ev.append(("swell", hero_t - 0.30, {}))
            ev.append(("hit", hero_t, {}))
        if k == 2:
            step = 0.36 / max(1, hero_len)
            for j in range(hero_len):
                ev.append(("knock", hero_t + j * step, {"j": j}))
        if k == 3:
            ev.append(("tick", hero_t + 0.32, {"f": 1800}))
        if k == 5:
            for j in range(12):
                ev.append(("tick", hero_t + j * 0.045, {"f": 2200 + (j % 4) * 260}))
    return ev


def render(plan: dict, out_wav: str, seed: int = 61) -> str:
    bus = _Bus(plan["duration"], seed)
    for kind, t, p in events(plan):
        if kind == "whoosh":
            bus.place(bus.whoosh(0.36, 0.5, 450, 3600), t)
        elif kind == "swell":
            bus.place(bus.whoosh(0.30, 0.45, 300, 2600, rev=True), t)
        elif kind == "hit":
            bus.place(bus.sub_hit(0.9, 100, 40, 0.9), t)
        elif kind == "knock":
            j = p["j"]
            bus.place(bus.knock(340 + (j * 53) % 200, peak=0.4), t, pan=(-0.4 if j % 2 else 0.4))
        elif kind == "tick":
            bus.place(bus.tick(p["f"], 0.045, 0.38), t)
    y = bus.out / (np.abs(bus.out).max() + 1e-9) * (10 ** (-18 / 20))
    pcm = (np.clip(y, -1, 1) * 32767).astype("<i2")
    with wave.open(out_wav, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())
    return out_wav
