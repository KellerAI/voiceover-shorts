#!/usr/bin/env python3
"""Instantiate the style H (terminal noir data-viz) template for any narration.

Canonical look: the reference lookdev (read only). The template keeps its system (one 2D canvas
drawn as a pure function of t, a dashboard world under a moving camera, screen-space headlines
with per-word rise, HUD chrome, CRT overlay, frame-blended motion blur, a red glitch "chaos"
beat, a trace waterfall, a checks grid, and an orange tension payoff) and drives it from the
words file instead of the hard-coded r2 timings.

Usage:
  python3 styles/H/build.py --wav VOICE.wav --words WORDS.json --out DIR
                            [--duration S] [--brand BRAND.json] [--content CONTENT.json]
                            [--slug S49] [--json]

Inputs (script-agnostic):
  --wav       narration WAV (any rate/channels; ffmpeg reads it).
  --words     word timings: a list of {text, start, end} (lookdev format) or {"words": [...]}.
  --duration  narration length in seconds; default: the WAV duration (ffprobe). Every word
              must end inside it. The composition runs past it to hold the end card.
  --brand     optional JSON {name, line, bg, fg, accent} for KAEndCard.mount (kit defaults).
  --content   optional JSON overriding derived or default copy (keys of DEFAULT_CONTENT plus
              "kickers": [6 str], "heads": [6 x [line1, line2?]]).
  --slug      optional short id shown in the boot line (default: the WAV stem).

How the words drive the picture:
  * Six acts like the canonical cut (open, chaos, trace, checks, tension, close). The five
    inner edges are word starts picked by dynamic programming near the canonical proportions,
    preferring phrase starts, every act >= 0.9 s; the sixth edge is the end-card cut.
  * Every structural time of the canonical cut is remapped piecewise-linearly onto those edges
    (the template's T()); headline words rise on their own spoken word; the trace spans are the
    trace act's content words, placed and timed by their speech.
  * The shared Keller AI end card (kit/endcard.js) cuts in 0.1 s after the last word, holds 2 s.

Output (DIR):
  DIR/project/   a HyperFrames project: index.html, kit/{gsap.min.js,endcard.js},
                 assets/{voice_norm.wav,sfx_bus.wav,mix.wav,fonts/,brand/}
  DIR/plan.json  the beat plan injected into index.html (also printed with --json)
  DIR/sfx_raw.wav the synthesised SFX layer before the bus
Render it with:
  cd DIR && npm_config_offline=true sandbox-exec -f <repo>/kit/offline.sb \
      npx hyperframes@0.8.183 render project -w 2 -q delivery -o look.mp4

Exit: 0 built, 1 the SFX bus loudness gate failed, 2 usage or IO error (message on stderr).
"""
from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import subprocess
import sys
import wave
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN = HERE.parents[1]
sys.path.insert(0, str(PLUGIN))

from kit import sfx_bus  # noqa: E402

TEMPLATE = HERE / "template"
FPS = 30
# canonical r2-15s-H act edges: open | "more" (chaos) | "it's" (trace) | "passes" (checks) |
# "still" (tension) | "here's" (close) | END
ORIG = [0.0, 3.40, 5.16, 9.04, 10.56, 12.29, 14.3]
END_GAP = 0.1      # KAEndCard.DEFAULT_GAP
END_HOLD = 2.0     # KAEndCard.DEFAULT_HOLD
MIN_ACT = 0.9
MID_PHRASE_COST = 6.0
KICK_FALLBACK = ["BRIEF", "NOISE", "TRACE", "CHECKS", "SIGNAL", "NEXT"]
FONTS = ["LilitaOne.woff2", "ComicShanns.woff2", "Virgil.woff2"]

STOP = set("""a an the and or but if then so of in on at to for from by with as into onto over under about
this that these those it its it's i i'm i've me my we our you your he she they them their his her
is are was were be been being am do does did done have has had having will would can could should
may might must shall just very really also too than more most much many some any each every
there here what which who whom whose when where why how all both own same such only
s t don't doesn't isn't aren't wasn't weren't let's one
called based takes take took get gets got make makes made want wants say says said""".split())
STOP -= {"not", "no", "never"}  # negations are content

# Style chrome: decorative terminal copy that belongs to the look, not to any script.
DEFAULT_CONTENT = {
    "slop": [  # [text, family key (hand|mono|disp), weight]: the chaos log
        ["ERORR: undefind is not a fucntion", "hand", 700],
        ["warnign: 47 tests skiped", "mono", 400],
        ["TODO: fix l8r!!!1", "hand", 700],
        ["Traceback (most recnet call):", "mono", 700],
        ["main_FINAL_v2 (1).py line ???", "hand", 400],
        ["assert True  # trust me", "mono", 400],
        ["segfualt (core dumpped)", "hand", 700],
        ["npm WARN depreacted@0.0.1", "mono", 700],
        ["KeyError: 'teh'", "hand", 400],
        ["rm -rf node_modules && pray", "mono", 400],
        ["FIXME FIXME FIXEM", "hand", 700],
        ["works on my machine", "mono", 700],
    ],
    "checks": ["unit", "integ", "lint", "types", "format", "secure", "deps", "license",
               "build", "e2e", "schema", "api", "a11y", "perf", "docs", "review"],
    "agents": [["agent/fast", "err 31%"], ["agent/strict", "ok"]],
    "titles": ["AGENT STREAMS", "LOG · stderr", "TRACE · run", "CHECKS · run"],
    "health_title": "PROJECT HEALTH",
    "health_label": "HEALTH",
    "match_label": "PLAN MATCH",
    "metric": "COVERAGE",
    "metric_sub": "lines run by tests",
    "metric_value": "0%",
    "cta": "",          # default: "NEXT →" plus the close act's last content word
    "handle": "follow @kellerai",
    "mark": "KELLERAI",
    "lead": "",         # default: the narration's first words, lower case
    "close_sub": "",    # default: the close act's words, lower case
    "ticker": [],       # default: keywords plus run stats
}


class BuildError(Exception):
    pass


# ----------------------------------------------------------------------------- inputs

def load_words(path: Path) -> list[dict]:
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError) as e:
        raise BuildError(f"cannot read words json {path}: {e}") from e
    if isinstance(data, dict):
        data = data.get("words")
    if not isinstance(data, list) or not data:
        raise BuildError(f"{path}: expected a non-empty list of words")
    out = []
    for w in data:
        try:
            out.append({"text": str(w["text"]), "start": float(w["start"]), "end": float(w["end"])})
        except (KeyError, TypeError, ValueError) as e:
            raise BuildError(f"{path}: bad word entry {w!r}") from e
    out.sort(key=lambda w: w["start"])
    return out


def probe_duration(path: Path) -> float:
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                           capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise BuildError(f"ffprobe failed on {path}: {e}") from e
    if r.returncode != 0:
        raise BuildError(f"ffprobe failed on {path}: {r.stderr.strip()}")
    try:
        return float(r.stdout.strip())
    except ValueError as e:
        raise BuildError(f"ffprobe gave no duration for {path}") from e


# ----------------------------------------------------------------------------- plan

def endcard_plan(last_word_end: float) -> dict:
    """Mirror of KAEndCard.plan() with default gap/hold (the template re-checks it in JS)."""
    start = round(last_word_end + END_GAP, 6)
    end = math.ceil((max(start, last_word_end) + END_HOLD) * FPS - 1e-6) / FPS
    return {"start": start, "end": round(end, 6), "hold": END_HOLD}


def clean(tok: str) -> str:
    return re.sub(r"[^\w'’\-]", "", tok)


def phrase_ends(words: list[dict]) -> list[bool]:
    res = []
    for i, w in enumerate(words):
        punct = bool(re.search(r"[.,;:?!—–]$", w["text"].strip()))
        gap = i + 1 < len(words) and words[i + 1]["start"] - w["end"] >= 0.2
        res.append(punct or gap)
    return res


def pick_bounds(words: list[dict], cut: float) -> list[float]:
    """Six acts. Inner edges are word starts minimising sum((edge - proportional canonical
    edge)^2) + MID_PHRASE_COST per edge inside a phrase, every act >= MIN_ACT."""
    if cut < 6 * MIN_ACT:
        raise BuildError(f"narration too short for six acts (cut at {cut:.2f} s)")
    ends = phrase_ends(words)
    cands = [(words[i]["start"], 0.0 if ends[i - 1] else MID_PHRASE_COST) for i in range(1, len(words))]
    cands = [c for c in cands if MIN_ACT <= c[0] <= cut - MIN_ACT]
    targets = [cut * ORIG[k] / ORIG[6] for k in range(1, 6)]
    INF = float("inf")
    best = [[(INF, -1)] * len(cands) for _ in range(5)]
    for j, (t, pen) in enumerate(cands):
        best[0][j] = ((t - targets[0]) ** 2 + pen, -1)
    for k in range(1, 5):
        for j, (t, pen) in enumerate(cands):
            c0 = (t - targets[k]) ** 2 + pen
            for i in range(j):
                if best[k - 1][i][0] < INF and t - cands[i][0] >= MIN_ACT:
                    c = best[k - 1][i][0] + c0
                    if c < best[k][j][0]:
                        best[k][j] = (c, i)
    last = [(best[4][j][0], j) for j, (t, _) in enumerate(cands) if cut - t >= MIN_ACT and best[4][j][0] < INF]
    if last:
        j = min(last)[1]
        inner = []
        for k in range(4, -1, -1):
            inner.insert(0, cands[j][0])
            j = best[k][j][1]
    else:
        inner = targets
    return [0.0] + [round(b, 4) for b in inner] + [round(cut, 4)]


def content_words(tokens: list[str]) -> list[str]:
    return [t for t in tokens if t and t.lower().strip("'’") not in STOP]


def headline(act_words: list[dict]) -> list[list[dict]]:
    """Up to three of the act's last content words (<= 24 chars) on one or two lines, caps.
    Returns lines of word dicts {text, start} keeping each word's spoken start. Words before
    the act's last sentence end are dropped when the final sentence has content of its own."""
    cut = 0
    for i, w in enumerate(act_words[:-1]):
        if re.search(r"[.?!]$", w["text"].strip()):
            cut = i + 1
    tail = act_words[cut:]
    if content_words([clean(w["text"]) for w in tail]):
        act_words = tail
    items = [(clean(w["text"]), w) for w in act_words]
    kw = [(t, w) for t, w in items if t and t.lower().strip("'’") not in STOP] or [(t, w) for t, w in items if t]
    end_p = ""
    if act_words:
        m = re.search(r"[.,?!]$", act_words[-1]["text"].strip())
        end_p = m.group(0) if m else ""
    pick: list[tuple[str, dict]] = []
    for t, w in reversed(kw):
        if len(pick) >= 3 or sum(len(p[0]) + 1 for p in pick) + len(t) > 24:
            break
        pick.insert(0, (t, w))
    if not pick and kw:
        pick = [(kw[-1][0][:16], kw[-1][1])]
    words = [{"text": t.upper(), "start": w["start"]} for t, w in pick]
    if words:
        words[-1]["text"] += end_p
    if len(words) <= 1:
        return [words]
    return [words[:-1], words[-1:]]


def keywords(words: list[dict]) -> list[str]:
    seen, out = set(), []
    for w in words:
        t = clean(w["text"])
        if not t or t.lower() in STOP or len(t) < 3:
            continue
        u = t.upper()
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def trunc(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def lower_phrase(ws: list[dict], n: int) -> str:
    out = ""
    for w in ws:
        nxt = (out + " " + w["text"]).strip()
        if len(nxt) > n:
            break
        out = nxt
    return out.lower() or trunc(" ".join(w["text"] for w in ws).lower(), n)


def build_plan(words: list[dict], duration: float, slug: str, brand: dict, content: dict) -> dict:
    lwe = max(w["end"] for w in words)
    if lwe > duration + 1e-3:
        raise BuildError(f"last word ends at {lwe:.3f} s, after the narration ({duration:.3f} s)")
    ec = endcard_plan(lwe)
    bounds = pick_bounds(words, ec["start"])
    ends = phrase_ends(words)
    co = dict(DEFAULT_CONTENT)
    unknown = set(content) - set(DEFAULT_CONTENT) - {"kickers", "heads"}
    if unknown:
        raise BuildError(f"--content has unknown keys: {sorted(unknown)}")
    co.update({k: v for k, v in content.items() if k in DEFAULT_CONTENT})

    acts = []
    for k in range(6):
        idx = [i for i, w in enumerate(words) if bounds[k] <= w["start"] < bounds[k + 1]]
        aw = [words[i] for i in idx]
        first = idx[0] if idx else 0
        while idx and first > 0 and not ends[first - 1]:
            first -= 1
        hw = words[first: idx[-1] + 1] if idx else []
        kw = content_words([clean(w["text"]).upper() for w in aw])
        kick = max(kw, key=len)[:14] if kw else KICK_FALLBACK[k]
        if content.get("kickers"):
            kick = str(content["kickers"][k])
        lines = headline(hw)
        if content.get("heads"):  # explicit copy: words land evenly through the act
            lines = []
            n = sum(len(str(ln).split()) for ln in content["heads"][k]) or 1
            j = 0
            for ln in content["heads"][k]:
                row = []
                for tok in str(ln).split():
                    row.append({"text": tok, "start": bounds[k] + (bounds[k + 1] - bounds[k]) * 0.6 * j / n})
                    j += 1
                lines.append(row)
        # each headline word rises on its spoken word, but never before the act or in its last 0.15 s
        a0, a1 = bounds[k], bounds[k + 1]
        hl = []
        for li, ln in enumerate(lines):
            row = []
            for wi, wd in enumerate(ln):
                t = min(max(wd["start"], a0 + 0.02 + 0.06 * wi), a1 - 0.15)
                row.append([wd["text"], round(t, 4), wi == len(ln) - 1 and li == len(lines) - 1])
            hl.append(row)
        acts.append({"start": a0, "end": a1, "text": " ".join(w["text"] for w in aw), "kicker": kick,
                     "lines": [" ".join(x[0] for x in row) for row in hl], "hl": hl, "words": aw})

    # trace spans: plan.load, then the trace act's content words placed by their speech, plan.verify
    a = acts[2]
    a0, a1 = a["start"], a["end"]
    span = max(a1 - a0, 0.5)
    tw, seen = [], set()
    for w in a["words"]:
        base = re.sub(r"['’]s$", "", clean(w["text"]).lower())
        if base and base.strip("'’") not in STOP and base not in seen:
            seen.add(base)
            tw.append(w)
    tw = tw[:5]
    spans = [{"name": f"plan.load {slug.lower()}"[:18], "x0": 0.0, "w": 0.05, "depth": 0, "t": round(a0 + 0.02, 4)}]
    for w in tw:
        x0 = (w["start"] - a0) / span
        spans.append({"name": "task." + re.sub(r"['’]s$", "", clean(w["text"]).lower())[:12], "x0": round(min(0.95, x0), 4),
                      "w": round(max(0.04, (w["end"] - w["start"]) / span * 2.5), 4), "depth": 1,
                      "t": round(w["start"], 4)})
    n_task = len(spans) - 1
    spans.append({"name": f"plan.verify {n_task}/{n_task}", "x0": 0.9, "w": 0.1, "depth": 0,
                  "t": round(max(a0 + 0.1, a1 - 0.6), 4)})
    for s in spans:  # waterfall bars stay inside the ruler
        s["w"] = round(min(s["w"], 1.0 - s["x0"]), 4)

    kws = keywords(words)
    if not co["lead"]:
        co["lead"] = lower_phrase(words, 20) + "…"
    if not co["close_sub"]:
        co["close_sub"] = lower_phrase(acts[5]["words"], 36)
    if not co["cta"]:
        last_kw = content_words([clean(w["text"]).upper() for w in acts[5]["words"]])
        co["cta"] = "NEXT: " + (last_kw[-1] if last_kw else "MORE") + "  →"
    if not co["ticker"]:
        hot = f"{co['metric'].lower()} {co['metric_value']}"
        co["ticker"] = [f"{w.lower()} ok" for w in kws[:4]] + [f"checks {len(co['checks'])}", hot,
                                                                 f"plan {n_task}/{n_task}"]
        co["ticker_hot"] = hot
    else:
        co.setdefault("ticker_hot", "")
    co["boot"] = [f"$ kobs attach --run {slug.lower()}"[:34], "otel collector ..... ok", "streams: traces logs",
                  f"words: {len(words)}  beats: 6", f"watching: {kws[0].lower() if kws else 'run'}"[:30]]

    for act in acts:
        act.pop("words")
    return {
        "orig": ORIG,
        "bounds": bounds,
        "acts": acts,
        "spans": spans,
        "traceSpan": round(span, 3),
        "firstWordStart": round(words[0]["start"], 4),
        "lastWordEnd": round(lwe, 4),
        "narration": round(duration, 6),
        "endcard": ec,
        "duration": ec["end"],
        "brand": brand,
        "content": co,
    }


# ----------------------------------------------------------------------------- sfx (port of r2-15s-H sfx_gen.py)

def synth_sfx(plan: dict, out: Path, sr: int = 48000) -> Path:
    """The canonical H sound layer (typing clicks, glitch bursts, sub tone, whooshes, pentatonic
    span ticks, a rising chime cascade, a coverage thud, a detuned drone) on this plan's times,
    plus a whoosh into a sub thump at the end-card cut. Seeded numpy, no samples."""
    import numpy as np

    BD = plan["bounds"]

    def T(x: float) -> float:
        for i in range(1, 7):
            if x <= ORIG[i]:
                return BD[i - 1] + (x - ORIG[i - 1]) * (BD[i] - BD[i - 1]) / (ORIG[i] - ORIG[i - 1])
        return BD[6] + x - ORIG[6]

    dur = plan["duration"]
    N = int(round(sr * dur))
    L = np.zeros(N)
    R = np.zeros(N)
    rng = np.random.default_rng(1103)

    def place(s, t, pan=0.0, g=1.0):
        i = int(t * sr)
        j = min(N, i + len(s))
        if i >= N or j <= i or i < 0:
            return
        s = s[: j - i] * g
        L[i:j] += s * np.sqrt(0.5 * (1 - pan))
        R[i:j] += s * np.sqrt(0.5 * (1 + pan))

    def env(n, a, d):
        k = np.arange(n) / sr
        return np.minimum(1, k / max(a, 1e-4)) * np.exp(-k / d)

    def tone(f, d_, d, h=0.3):
        n = int(d_ * sr)
        t = np.arange(n) / sr
        return (np.sin(2 * np.pi * f * t) + h * np.sin(4 * np.pi * f * t)) * env(n, 0.001, d)

    def click():
        n = int(0.02 * sr)
        return rng.standard_normal(n) * env(n, 0.0003, 0.003) * 0.6

    def whoosh(d_):
        n = int(d_ * sr)
        x = np.cumsum(rng.standard_normal(n))
        x -= np.convolve(x, np.ones(200) / 200, "same")
        w = np.sin(np.pi * np.arange(n) / n) ** 2
        return x / (np.abs(x).max() + 1e-9) * w * 0.5

    def glitch(d_):
        n = int(d_ * sr)
        t = np.arange(n) / sr
        sq = np.sign(np.sin(2 * np.pi * (90 + 400 * rng.random()) * t))
        return (sq * 0.4 + rng.standard_normal(n) * 0.3) * env(n, 0.001, d_ / 3)

    def thump(ln=0.6):
        k = int(ln * sr)
        tt = np.arange(k) / sr
        f = 46 + 40 * np.exp(-tt / 0.05)
        return np.sin(2 * np.pi * np.cumsum(f) / sr) * env(k, 0.002, 0.16)

    b1 = BD[1]
    for i in range(5):  # boot typing, inside the open act
        for k in range(10):
            at = 0.3 + i * min(0.32, (b1 - 0.8) / 5) + k * 0.035
            if at < b1 - 0.2:
                place(click(), at, pan=0.3)
    place(whoosh(0.5), b1 - 0.15, g=0.8)
    chaos = BD[2] - BD[1]
    for k in range(14):
        at = b1 + 0.02 + k * chaos * 0.8 / 14
        place(glitch(0.08 + 0.05 * rng.random()), at, pan=rng.uniform(-0.6, 0.6), g=0.5)
    place(tone(55, 1.2, 0.5, 0.6), T(3.76), g=0.7)
    place(whoosh(0.4), BD[2] - 0.16)
    pent = [523.25, 587.33, 659.25, 783.99, 880, 1046.5, 1174.66]
    for i, s in enumerate(plan["spans"]):
        place(tone(pent[i % len(pent)], 0.3, 0.06), s["t"], pan=-0.2 + 0.06 * i, g=0.5)
    n_ch = len(plan["content"]["checks"])
    step = min(0.05, max(0.02, (BD[4] - BD[3]) * 0.5 / n_ch))
    for i in range(n_ch):
        place(tone(1046.5 * 2 ** (i / 24), 0.4, 0.09), BD[3] + i * step, pan=-0.5 + i / n_ch, g=0.25)
    place(tone(41.2, 1.6, 0.6, 0.2), BD[4], g=1.0)
    n = int(1.7 * sr)
    t = np.arange(n) / sr
    dr = (np.sin(2 * np.pi * 73.4 * t) + np.sin(2 * np.pi * 77.8 * t)) * np.minimum(1, t / 0.4) \
        * np.exp(-np.maximum(0, t - 1.1) / 0.25) * 0.3
    place(dr, T(11.1))
    place(whoosh(0.6), BD[5] - 0.2, g=0.6)
    ec = plan["endcard"]["start"]
    place(whoosh(0.3), ec - 0.3, g=0.5)
    place(thump(), ec, g=0.9)

    st = np.stack([L, R], 1)
    peak = float(np.max(np.abs(st)))
    if peak > 0.99:
        st *= 0.99 / peak
    pcm = (st * 32767).astype("<i2")
    with wave.open(str(out), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    return out


# ----------------------------------------------------------------------------- build

def build(wav: Path, words_path: Path, out: Path, *, duration: float | None = None,
          brand: dict | None = None, content: dict | None = None, slug: str | None = None) -> dict:
    wav, words_path, out = Path(wav), Path(words_path), Path(out)
    if not wav.is_file():
        raise BuildError(f"wav not found: {wav}")
    words = load_words(words_path)
    narr = float(duration) if duration is not None else probe_duration(wav)
    if narr <= 0:
        raise BuildError("duration must be > 0")
    plan = build_plan(words, narr, slug or wav.stem, dict(brand or {}), dict(content or {}))

    proj = out / "project"
    for sub in ("kit", "assets/fonts", "assets/brand"):
        (proj / sub).mkdir(parents=True, exist_ok=True)
    shutil.copyfile(PLUGIN / "kit" / "vendor" / "gsap.min.js", proj / "kit" / "gsap.min.js")
    shutil.copyfile(PLUGIN / "kit" / "endcard.js", proj / "kit" / "endcard.js")
    for f in FONTS:
        shutil.copyfile(PLUGIN / "kit" / "fonts" / f, proj / "assets" / "fonts" / f)
    shutil.copyfile(PLUGIN / "kit" / "brand" / "keller-ai-logo.svg", proj / "assets" / "brand" / "keller-ai-logo.svg")

    raw = synth_sfx(plan, out / "sfx_raw.wav")
    voice_src = wav
    limit = min(narr, plan["endcard"]["start"])
    if probe_duration(wav) > limit + 1e-3:
        voice_src = out / "voice_trim.wav"
        fade = min(0.05, limit / 2)
        r = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-i", str(wav),
                            "-t", f"{limit:.6f}", "-af", f"afade=t=out:st={limit - fade:.6f}:d={fade:.6f}",
                            "-c:a", "pcm_s24le", str(voice_src)], capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            raise BuildError(f"ffmpeg voice trim failed: {r.stderr.strip()}")
    plan["voice_source"] = str(voice_src)
    try:
        bus = sfx_bus.build(str(voice_src), str(raw), str(proj / "assets"))
    except sfx_bus.SfxBusError as e:
        raise BuildError(f"sfx_bus: {e}") from e
    plan["audio"] = {"voice": bus["voice"], "sfx": bus["sfx"], "mix": bus["mix"], "bus_passed": bus["passed"],
                     "loudness": bus["loudness"]["metrics"]}

    html = (TEMPLATE / "index.html").read_text()
    for token in ("__H_DURATION__", "__H_VO_DURATION__", "/*__H_DATA__*/null", "__H_TITLE__"):
        if token not in html:
            raise BuildError(f"template is missing placeholder {token}")
    inject = {k: v for k, v in plan.items() if k not in ("audio", "voice_source")}
    vo_dur = f"{min(probe_duration(Path(bus['voice'])), plan['duration']):.6f}"
    html = (html.replace("__H_VO_DURATION__", vo_dur)
                .replace("__H_DURATION__", f"{plan['duration']:.6f}")
                .replace("/*__H_DATA__*/null", json.dumps(inject, ensure_ascii=False).replace("</", "<\\/"))
                .replace("__H_TITLE__", (slug or wav.stem).replace("<", "")))
    (proj / "index.html").write_text(html)
    plan["project"] = str(proj)
    (out / "plan.json").write_text(json.dumps(plan, indent=1, ensure_ascii=False))
    return plan


def _load_obj(path: str | None, what: str) -> dict | None:
    if not path:
        return None
    obj = json.loads(Path(path).read_text())
    if not isinstance(obj, dict):
        raise BuildError(f"--{what} must hold a JSON object")
    return obj


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Instantiate the style H template.")
    ap.add_argument("--wav", required=True)
    ap.add_argument("--words", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--duration", type=float)
    ap.add_argument("--brand")
    ap.add_argument("--content")
    ap.add_argument("--slug")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        plan = build(Path(a.wav), Path(a.words), Path(a.out), duration=a.duration,
                     brand=_load_obj(a.brand, "brand"), content=_load_obj(a.content, "content"), slug=a.slug)
    except (BuildError, OSError, ValueError) as e:
        print(f"build.py: {e}", file=sys.stderr)
        return 2
    if a.json:
        print(json.dumps(plan, indent=1, ensure_ascii=False))
    else:
        print(f"built {plan['project']} duration={plan['duration']:.3f}s bounds={plan['bounds']}")
    return 0 if plan["audio"]["bus_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
