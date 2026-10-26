#!/usr/bin/env python3
"""Instantiate the style D (reel-native) template for any narration.

Usage:
  python3 styles/D/build.py --wav VOICE.wav --words WORDS.json --out DIR
                            [--duration S] [--brand BRAND.json] [--slug S49] [--json]

Inputs (script-agnostic):
  --wav       narration WAV (any rate/channels; ffmpeg reads it).
  --words     word timings: a list of {text, start, end} (lookdev format) or {"words": [...]}.
  --duration  narration length in seconds; default: the WAV duration (ffprobe). Every word
              must end inside it. The composition runs past it to hold the end card.
  --brand     optional JSON: {name, line, bg, fg, accent, footer}. name/line/bg/fg/accent go
              to KAEndCard.mount (kit/endcard.js defaults otherwise); footer is the D-style
              lower-third line on the end card (default "KELLER AI · SHORTS").
  --slug      optional short id shown in the type-on line (default: the WAV stem).

Output (DIR):
  DIR/project/              a HyperFrames project: index.html, kit/{gsap.min.js,endcard.js},
                            assets/{voice_norm.wav,sfx_bus.wav,mix.wav,fonts/,brand/}
  DIR/plan.json             the beat plan injected into index.html (also printed with --json)
  DIR/sfx_raw.wav           the synthesised SFX layer before the bus
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
ORIG = [0.0, 2.04, 3.72, 5.14, 6.62, 8.98, 10.16]  # dir-D act edges
END_GAP = 0.1      # KAEndCard.DEFAULT_GAP
END_HOLD = 2.0     # KAEndCard.DEFAULT_HOLD
MIN_ACT = 0.9
DEFAULT_FOOTER = "KELLER AI · SHORTS"
KICK_FALLBACK = ["BRIEF", "TRACE", "NOISE", "RIGOR", "LOCK", "CHECK"]

STOP = set("""a an the and or but if then so of in on at to for from by with as into onto over under about
this that these those it its it's i i'm i've me my we our you your he she they them their his her
is are was were be been being am do does did done have has had having will would can could should
may might must shall just very really also too than more most much many some any each every
there here what which who whom whose when where why how all both own same such only
s t don't doesn't isn't aren't wasn't weren't let's one
called based takes take took get gets got make makes made want wants say says said""".split())
# negations are content: "is not independence" must keep NOT
STOP -= {"not", "no", "never"}


class BuildError(Exception):
    pass


# ----------------------------------------------------------------------------- inputs

def load_words(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text())
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
    """True where a word closes a phrase (punctuation, or a pause of >= 0.2 s after it)."""
    res = []
    for i, w in enumerate(words):
        punct = bool(re.search(r"[.,;:?!—–]$", w["text"].strip()))
        gap = i + 1 < len(words) and words[i + 1]["start"] - w["end"] >= 0.2
        res.append(punct or gap)
    return res


MID_PHRASE_COST = 6.0  # s^2: cutting inside a phrase costs as much as a 2.4 s miss


def pick_bounds(words: list[dict], cut: float) -> list[float]:
    """Six acts like dir-D. The five inner edges are word starts chosen by dynamic
    programming to minimise sum((edge - proportional dir-D cut)^2) + MID_PHRASE_COST per
    edge that is not a phrase start, with every act >= MIN_ACT s. B[0]=0, B[6]=end-card cut."""
    if cut < 6 * MIN_ACT:
        raise BuildError(f"narration too short for six acts (cut at {cut:.2f} s)")
    ends = phrase_ends(words)
    cands = [(words[i]["start"], 0.0 if ends[i - 1] else MID_PHRASE_COST) for i in range(1, len(words))]
    cands = [c for c in cands if MIN_ACT <= c[0] <= cut - MIN_ACT]
    targets = [cut * ORIG[k] / ORIG[6] for k in range(1, 6)]
    INF = float("inf")
    # best[k][j] = (cost, prev index) with edge k at candidate j
    best = [[(INF, -1)] * len(cands) for _ in range(5)]
    for j, (t, pen) in enumerate(cands):
        if t >= MIN_ACT:
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
    else:  # too few words for word-aligned edges: use the proportional points
        inner = targets
    return [0.0] + [round(b, 4) for b in inner] + [round(cut, 4)]


def content(tokens: list[str]) -> list[str]:
    return [t for t in tokens if t and t.lower().strip("'’") not in STOP]


def headline(act_words: list[dict]) -> list[str]:
    """Two lines of the act's last content words (the phrase-final noun phrase), caps."""
    toks = [clean(w["text"]) for w in act_words]
    kw = content(toks) or [t for t in toks if t]
    end_p = ""
    if act_words:
        m = re.search(r"[.,?!]$", act_words[-1]["text"].strip())
        end_p = m.group(0) if m else ""
    pick: list[str] = []
    for t in reversed(kw):
        if len(pick) >= 3 or sum(len(p) + 1 for p in pick) + len(t) > 24:
            break
        pick.insert(0, t)
    if not pick and kw:
        pick = [kw[-1][:16]]
    pick = [p.upper() for p in pick]
    if len(pick) == 1:
        return [pick[0] + end_p]
    return [" ".join(pick[:-1]), pick[-1] + end_p]


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


def spread(items: list[str], n: int, maxlen: int) -> list[str]:
    items = [i for i in items if len(i) <= maxlen] or [i[:maxlen] for i in items]
    if not items:
        return [""] * n
    if len(items) <= n:
        return [items[i % len(items)] for i in range(n)]
    return [items[round(i * (len(items) - 1) / (n - 1))] for i in range(n)]


def build_plan(words: list[dict], duration: float, slug: str, brand: dict) -> dict:
    lwe = max(w["end"] for w in words)
    if lwe > duration + 1e-3:
        raise BuildError(f"last word ends at {lwe:.3f} s, after the narration ({duration:.3f} s)")
    ec = endcard_plan(lwe)
    bounds = pick_bounds(words, ec["start"])
    acts = []
    ends = phrase_ends(words)
    for k in range(6):
        idx = [i for i, w in enumerate(words) if bounds[k] <= w["start"] < bounds[k + 1]]
        aw = [words[i] for i in idx]
        # an act that starts inside a phrase titles the whole phrase ("Effective | Challenge.")
        first = idx[0] if idx else 0
        while idx and first > 0 and not ends[first - 1]:
            first -= 1
        hw = words[first: idx[-1] + 1] if idx else []
        kw = content([clean(w["text"]).upper() for w in aw])
        kick = max(kw, key=len)[:12] if kw else KICK_FALLBACK[k]
        acts.append({"start": bounds[k], "end": bounds[k + 1], "text": " ".join(w["text"] for w in aw),
                     "kicker": kick, "lines": headline(hw)})
    kws = keywords(words)
    type1 = f"short.run --brief {slug.lower()}"[:30]
    type2 = f"{len(words)} words · {duration:.1f} s · 6 beats"
    footer = brand.pop("footer", DEFAULT_FOOTER)
    return {
        "bounds": bounds,
        "acts": acts,
        "route": spread(kws, 7, 11),
        "rows": spread(kws, 6, 13),
        "chips": spread(kws, 6, 9),
        "typeon": [type1, type2],
        "firstWordStart": round(words[0]["start"], 4),
        "lastWordEnd": round(lwe, 4),
        "narration": round(duration, 6),
        "endcard": ec,
        "duration": ec["end"],
        "brand": brand,
        "footer": footer,
    }


# ----------------------------------------------------------------------------- sfx (port of dir-D work/sfx.py)

def synth_sfx(plan: dict, out: Path, sr: int = 48000) -> Path:
    """dir-D's sound layer (drone, route ticks, glitch bursts, ledger ticks, chime, whoosh,
    thump, chip ticks), placed on this plan's act times. Seeded numpy, no samples."""
    import numpy as np

    B = plan["bounds"]

    def T(t: float) -> float:
        for i in range(1, 7):
            if t <= ORIG[i]:
                return B[i - 1] + (t - ORIG[i - 1]) * (B[i] - B[i - 1]) / (ORIG[i] - ORIG[i - 1])
        return B[6] + t - ORIG[6]

    dur = plan["duration"]
    n = int(round(sr * dur))
    t_all = np.arange(n) / sr
    L = np.zeros(n)
    Rc = np.zeros(n)
    rng = np.random.default_rng(61)

    def add(sig, at, pan=0.0, gain=1.0):
        i = int(at * sr)
        j = min(n, i + len(sig))
        if j <= i:
            return
        s = sig[: j - i] * gain
        L[i:j] += s * np.sqrt(0.5 * (1 - pan))
        Rc[i:j] += s * np.sqrt(0.5 * (1 + pan))

    def env(k, a, d):
        tt = np.arange(k) / sr
        e = np.exp(-tt / d)
        na = max(1, int(a * sr))
        e[:na] *= np.linspace(0, 1, na)
        return e

    def tick(freq=3200, d=0.012, ln=0.05):
        k = int(ln * sr)
        return np.sin(2 * np.pi * freq * np.arange(k) / sr) * env(k, 0.0005, d)

    def burst(ln=0.09, lo=900, hi=6000, d=0.03):
        k = int(ln * sr)
        X = np.fft.rfft(rng.standard_normal(k))
        f = np.fft.rfftfreq(k, 1 / sr)
        X[(f < lo) | (f > hi)] = 0
        y = np.fft.irfft(X, k)
        y /= np.max(np.abs(y)) + 1e-9
        gate = (np.floor(np.arange(k) / (sr * 0.011)) % 3 != 1).astype(float)
        return y * env(k, 0.001, d) * gate

    def whoosh(ln=0.3):
        k = int(ln * sr)
        x = rng.standard_normal(k)
        # one-pole low-pass opening up
        y = np.zeros(k)
        a = 0.0
        cut = np.linspace(0.02, 0.35, k)
        for i in range(k):
            a += cut[i] * (x[i] - a)
            y[i] = a
        y /= np.max(np.abs(y)) + 1e-9
        return y * np.linspace(0, 1, k) ** 2.2

    def thump(ln=0.6):
        k = int(ln * sr)
        tt = np.arange(k) / sr
        f = 46 + 40 * np.exp(-tt / 0.05)
        return np.sin(2 * np.pi * np.cumsum(f) / sr) * env(k, 0.002, 0.16)

    def chime(ln=0.9):
        k = int(ln * sr)
        tt = np.arange(k) / sr
        return (np.sin(2 * np.pi * 880 * tt) + 0.6 * np.sin(2 * np.pi * 1320 * tt)
                + 0.25 * np.sin(2 * np.pi * 2640 * tt)) * env(k, 0.002, 0.22) / 1.85

    bed = (0.5 * np.sin(2 * np.pi * 55 * t_all) + 0.3 * np.sin(2 * np.pi * 82.5 * t_all + 0.7)
           + 0.12 * np.sin(2 * np.pi * 110.3 * t_all))
    bed *= 0.55 - 0.45 * np.sin(2 * np.pi * t_all / max(dur, 1.0) - np.pi / 2)
    fade = np.minimum(1, t_all / 0.4) * np.minimum(1, np.maximum(0, dur - t_all) / 0.25)
    L += bed * fade * 0.10
    Rc += bed * fade * 0.10

    for i, at in enumerate([0.2, 1.21, 1.89, 2.57, 3.25, 3.93, 4.61]):
        add(tick(2600 + 180 * i), T(at), pan=(-0.4 if i % 2 else 0.4), gain=0.35)
    for at, g in [(B[1] - 0.08, 0.22), (B[2] - 0.10, 0.22), (B[3] - 0.10, 0.42), (B[4] - 0.12, 0.22), (B[5] - 0.08, 0.18)]:
        add(burst(0.12 if g > 0.3 else 0.08), at, gain=g)
    k = int(1.1 * sr)
    scr = rng.standard_normal(k)
    scr = np.convolve(scr, np.ones(6) / 6, mode="same") * (np.abs(np.sin(np.arange(k) / sr * 2 * np.pi * 9)) ** 3)
    scr *= np.minimum(1, np.arange(k) / (0.15 * sr)) * np.minimum(1, (k - np.arange(k)) / (0.3 * sr))
    add(scr / (np.max(np.abs(scr)) + 1e-9), T(3.78), gain=0.07)
    tP, dP = T(5.6), T(6.55) - T(5.6)
    for i, y in enumerate([385, 550, 715, 880, 1045, 1210]):
        add(tick(2000 + 220 * i, d=0.02), tP + dP * (y - 385) / (1210 - 385), gain=0.3)
    add(chime(), T(8.15), gain=0.22)
    add(whoosh(0.3), B[5] - 0.22, gain=0.25)
    add(thump(), B[5] + 0.06, gain=0.28)
    add(tick(1500, d=0.03, ln=0.12), B[5] + 0.30, gain=0.3)
    b5 = B[5] + 0.06
    step = max(0.03, min(0.12, (B[6] - 0.3 - (b5 + 0.32)) / 6))
    for i in range(6):
        add(tick(3000 + 140 * i), b5 + 0.32 + i * step, pan=-0.6 + 0.2 * i, gain=0.25)
    # end-card cut: whoosh into a sub thump
    ec = plan["endcard"]["start"]
    add(whoosh(0.3), ec - 0.3, gain=0.22)
    add(thump(), ec, gain=0.3)

    st = np.stack([L, Rc], 1)
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
          brand: dict | None = None, slug: str | None = None) -> dict:
    wav, words_path, out = Path(wav), Path(words_path), Path(out)
    if not wav.is_file():
        raise BuildError(f"wav not found: {wav}")
    words = load_words(words_path)
    narr = float(duration) if duration is not None else probe_duration(wav)
    if narr <= 0:
        raise BuildError("duration must be > 0")
    brand = dict(brand or {})
    plan = build_plan(words, narr, slug or wav.stem, brand)

    proj = out / "project"
    (proj / "kit").mkdir(parents=True, exist_ok=True)
    (proj / "assets" / "fonts").mkdir(parents=True, exist_ok=True)
    (proj / "assets" / "brand").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(PLUGIN / "kit" / "vendor" / "gsap.min.js", proj / "kit" / "gsap.min.js")
    shutil.copyfile(PLUGIN / "kit" / "endcard.js", proj / "kit" / "endcard.js")
    shutil.copyfile(PLUGIN / "kit" / "fonts" / "LilitaOne.woff2", proj / "assets" / "fonts" / "LilitaOne.woff2")
    shutil.copyfile(PLUGIN / "kit" / "brand" / "keller-ai-logo.svg", proj / "assets" / "brand" / "keller-ai-logo.svg")

    # audio: dir-D SFX layer -> kit/sfx_bus (voice to -14 LUFS, SFX 14 dB under, gated by the loudness check)
    raw = synth_sfx(plan, out / "sfx_raw.wav")
    # --duration shapes the audio: the voice never runs past min(duration, end-card cut)
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

    dur = f"{plan['duration']:.6f}"
    html = (TEMPLATE / "index.html").read_text()
    for token in ("__D_DURATION__", "__D_VO_DURATION__", "/*__D_DATA__*/null", "__D_TITLE__"):
        if token not in html:
            raise BuildError(f"template is missing placeholder {token}")
    inject = {k: v for k, v in plan.items() if k != "audio"}
    # the voice slot is the narration's own length (HyperFrames shortens a longer slot and warns)
    vo_dur = f"{min(probe_duration(Path(bus['voice'])), plan['duration']):.6f}"
    html = (html.replace("__D_VO_DURATION__", vo_dur)
                .replace("__D_DURATION__", dur)
                .replace("/*__D_DATA__*/null", json.dumps(inject, ensure_ascii=False).replace("</", "<\\/"))
                .replace("__D_TITLE__", (slug or wav.stem).replace("<", "")))
    (proj / "index.html").write_text(html)
    plan["project"] = str(proj)
    (out / "plan.json").write_text(json.dumps(plan, indent=1, ensure_ascii=False))
    return plan


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Instantiate the style D template.")
    ap.add_argument("--wav", required=True)
    ap.add_argument("--words", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--duration", type=float)
    ap.add_argument("--brand")
    ap.add_argument("--slug")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        brand = json.loads(Path(a.brand).read_text()) if a.brand else None
        if brand is not None and not isinstance(brand, dict):
            raise BuildError("--brand must hold a JSON object")
        plan = build(Path(a.wav), Path(a.words), Path(a.out), duration=a.duration, brand=brand, slug=a.slug)
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
