#!/usr/bin/env python3
"""Instantiate the style F (isometric systems world) template for any narration.

Canonical look: the reference lookdev (read only). The template keeps that look's canvas
world and camera path on their original clock and warps real time onto it act by act, so
every beat-locked move (sloppy-pad dive, plan ticks, gates going green, tower failing) lands
on the new narration's six acts. Headlines are built from the narration's words.

Usage:
  python3 styles/F/build.py --wav VOICE.wav --words WORDS.json --out DIR
                            [--duration S] [--brand BRAND.json] [--slug S49] [--json]

Inputs (script-agnostic):
  --wav       narration WAV (any rate/channels; ffmpeg reads it).
  --words     word timings: a list of {text, start, end} (lookdev format) or {"words": [...]}.
  --duration  narration length in seconds; default: the WAV duration (ffprobe). Every word
              must end inside it. The voice is trimmed to min(duration, end-card cut); the
              composition runs past it to hold the end card.
  --brand     optional JSON object: {name, line, bg, fg, accent, logo} go to KAEndCard.mount
              (kit/endcard.js defaults otherwise); "footer" is the lower-third line on the end
              card (default "KELLER AI · SHORTS"); "slopsub" is the sloppy-pad sticker text
              (default "it compiled on my machine!!1").
  --slug      optional short id shown in the page title (default: the WAV stem).

Output (DIR):
  DIR/project/   a HyperFrames project: index.html, kit/{gsap.min.js,endcard.js},
                 assets/{voice_norm.wav,sfx_bus.wav,mix.wav,fonts/,brand/}
  DIR/plan.json  the plan injected into index.html (also printed with --json)
  DIR/sfx_raw.wav  the synthesised SFX layer before the bus
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
# r2-15s-F act edges on its own clock (word starts of more / its / passes / and / heres,
# then its own end-card cut at T.plain - 0.3). The template's ORIG array must match.
ORIG = [0.0, 3.40, 5.16, 9.04, 10.28, 12.287, 12.9]
END_GAP = 0.1      # KAEndCard.DEFAULT_GAP
END_HOLD = 2.0     # KAEndCard.DEFAULT_HOLD
MIN_ACT = 0.9
MID_PHRASE_COST = 6.0  # s^2: an act edge inside a phrase costs as much as a 2.4 s miss
DEFAULT_FOOTER = "KELLER AI · SHORTS"
KICK_FALLBACK = ["THE HOOK", "THE CONTRAST", "THE RUN", "THE CHECKS", "THE RESULT", "THE SETUP"]
PLACEHOLDERS = ("__F_DURATION__", "__F_VO_DURATION__", "/*__F_DATA__*/null", "__F_TITLE__")

STOP = set("""a an the and or but if then so of in on at to for from by with as into onto over under about
this that these those it its it's i i'm i've me my we our you your he she they them their his her
is are was were be been being am do does did done have has had having will would can could should
may might must shall just very really also too than more most much many some any each every
there here what which who whom whose when where why how all both own same such only
s t don't doesn't isn't aren't wasn't weren't let's one
called based takes take took get gets got make makes made want wants say says said""".split())
STOP -= {"not", "no", "never"}  # negations are content


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
    """Mirror of KAEndCard.plan() with the default gap/hold (the template re-checks it in JS)."""
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


def pick_bounds(words: list[dict], cut: float) -> list[float]:
    """Six acts like r2-15s-F. The five inner edges are word starts chosen by dynamic
    programming to minimise sum((edge - proportional F edge)^2) + MID_PHRASE_COST per edge
    that is not a phrase start, with every act >= MIN_ACT s. B[0]=0, B[6]=end-card cut."""
    if cut < 6 * MIN_ACT:
        raise BuildError(f"narration too short for six acts (cut at {cut:.2f} s)")
    ends = phrase_ends(words)
    cands = [(words[i]["start"], 0.0 if ends[i - 1] else MID_PHRASE_COST) for i in range(1, len(words))]
    cands = [c for c in cands if MIN_ACT <= c[0] <= cut - MIN_ACT]
    targets = [cut * ORIG[k] / ORIG[6] for k in range(1, 6)]
    inf = float("inf")
    best = [[(inf, -1)] * len(cands) for _ in range(5)]
    for j, (t, pen) in enumerate(cands):
        best[0][j] = ((t - targets[0]) ** 2 + pen, -1)
    for k in range(1, 5):
        for j, (t, pen) in enumerate(cands):
            c0 = (t - targets[k]) ** 2 + pen
            for i in range(j):
                if best[k - 1][i][0] < inf and t - cands[i][0] >= MIN_ACT:
                    c = best[k - 1][i][0] + c0
                    if c < best[k][j][0]:
                        best[k][j] = (c, i)
    last = [(best[4][j][0], j) for j, (t, _) in enumerate(cands) if cut - t >= MIN_ACT and best[4][j][0] < inf]
    if last:
        j = min(last)[1]
        inner = []
        for k in range(4, -1, -1):
            inner.insert(0, cands[j][0])
            j = best[k][j][1]
    else:  # too few words for word-aligned edges: use the proportional points
        inner = targets
    return [0.0] + [round(b, 4) for b in inner] + [round(cut, 4)]


def is_content(tok: str) -> bool:
    return bool(tok) and tok.lower().strip("'’") not in STOP


def headline(act_words: list[dict], *, max_words: int, max_chars: int) -> list[list[dict]]:
    """The act's phrase-final content words (<= max_words, <= max_chars), as one or two
    lines of {text, t}; t is the spoken start, so each word enters as it is said."""
    toks = [(clean(w["text"]), w) for w in act_words]
    kw = [(t, w) for t, w in toks if is_content(t)] or [(t, w) for t, w in toks if t]
    end_p = ""
    if act_words:
        m = re.search(r"[.?!]$", act_words[-1]["text"].strip())
        end_p = m.group(0) if m else ""
    pick: list[tuple[str, dict]] = []
    for t, w in reversed(kw):
        if len(pick) >= max_words or sum(len(p[0]) + 1 for p in pick) + len(t) > max_chars:
            break
        pick.insert(0, (t, w))
    if not pick and kw:
        pick = [(kw[-1][0][:max_chars], kw[-1][1])]
    items = [{"text": t[:1].upper() + t[1:] if i == 0 else t, "t": round(w["start"], 4)} for i, (t, w) in enumerate(pick)]
    if items:
        items[-1]["text"] += end_p
    if len(items) <= 1:
        return [items]
    return [items[:-1], items[-1:]]


def build_plan(words: list[dict], duration: float, brand: dict) -> dict:
    lwe = max(w["end"] for w in words)
    if lwe > duration + 1e-3:
        raise BuildError(f"last word ends at {lwe:.3f} s, after the narration ({duration:.3f} s)")
    ec = endcard_plan(lwe)
    bounds = pick_bounds(words, ec["start"])
    ends = phrase_ends(words)
    acts = []
    for k in range(6):
        idx = [i for i, w in enumerate(words) if bounds[k] <= w["start"] < bounds[k + 1]]
        aw = [words[i] for i in idx]
        kws = [clean(w["text"]).upper() for w in aw if is_content(clean(w["text"]))]
        kick = max(kws, key=len)[:14] if kws else KICK_FALLBACK[k]
        # an act that starts inside a phrase may title the phrase it finishes; keep only this act's words
        lines = headline(aw, max_words=2 if k == 1 else 3, max_chars=14 if k == 1 else 22) if aw else [[]]
        if not any(lines):
            lines = [[{"text": KICK_FALLBACK[k].title(), "t": bounds[k]}]]
        acts.append({"start": bounds[k], "end": bounds[k + 1], "text": " ".join(w["text"] for w in aw),
                     "kicker": kick, "chip": kick, "lines": lines,
                     "phrase_start": not idx or idx[0] == 0 or ends[idx[0] - 1]})
    footer = brand.pop("footer", DEFAULT_FOOTER)
    slopsub = brand.pop("slopsub", "it compiled on my machine!!1")
    return {
        "bounds": bounds,
        "orig_bounds": ORIG,
        "acts": acts,
        "firstWordStart": round(words[0]["start"], 4),
        "lastWordEnd": round(lwe, 4),
        "narration": round(duration, 6),
        "endcard": ec,
        "duration": ec["end"],
        "brand": brand,
        "footer": footer,
        "slopsub": slopsub,
    }


def warp(plan: dict):
    """Original r2-15s-F clock -> real time (same piecewise map as the template's fwd())."""
    B = plan["bounds"]

    def f(t: float) -> float:
        for i in range(1, 7):
            if t <= ORIG[i]:
                return B[i - 1] + (t - ORIG[i - 1]) * (B[i] - B[i - 1]) / (ORIG[i] - ORIG[i - 1])
        return B[6] + t - ORIG[6]
    return f


# ----------------------------------------------------------------------------- sfx (port of r2-15s-F work/sfx.py)

def synth_sfx(plan: dict, out: Path, sr: int = 48000) -> Path:
    """r2-15s-F's sound layer (camera whooshes, slide whistle + boing, pentatonic plan ticks,
    gate chimes, the worse-off thud, a resolve chord), each event placed through warp().
    Level is NOT set here: kit/sfx_bus.py puts the bus 14 dB under the voice peak."""
    import numpy as np

    T = warp(plan)
    dur = plan["duration"]
    n_all = int(round(sr * dur))
    L = np.zeros(n_all)
    R = np.zeros(n_all)
    rng = np.random.default_rng(91)

    def env(n, a=0.003, d=0.2):
        t = np.arange(n) / sr
        return np.minimum(1, t / a) * np.exp(-t / d)

    def tone(f, d_s, d=0.2, a=0.003):
        n = int(d_s * sr)
        t = np.arange(n) / sr
        return np.sin(2 * np.pi * f * t) * env(n, a, d)

    def place(sig, t, pan=0.0, g=1.0):
        i = int(t * sr)
        j = min(n_all, i + len(sig))
        if i >= n_all or j <= i:
            return
        s = sig[: j - i] * g
        L[i:j] += s * np.sqrt(0.5 * (1 - pan))
        R[i:j] += s * np.sqrt(0.5 * (1 + pan))

    def whoosh(d_s, g=1.0):
        n = int(d_s * sr)
        x = rng.standard_normal(n)
        y = np.convolve(x, np.ones(24) / 24, "same")
        return y * np.hanning(n) * g

    def slide(f0, f1, d_s):
        n = int(d_s * sr)
        t = np.arange(n) / sr
        ph = 2 * np.pi * np.cumsum(np.linspace(f0, f1, n)) / sr
        return (np.sin(ph) + 0.3 * np.sin(2 * ph)) * np.hanning(n) * (1 + 0.3 * np.sin(2 * np.pi * 7 * t))

    def boing():
        n = int(0.45 * sr)
        t = np.arange(n) / sr
        f = 180 + 120 * np.exp(-t * 6) * np.sin(2 * np.pi * 9 * t)
        return np.sin(2 * np.pi * np.cumsum(f) / sr) * env(n, 0.002, 0.18)

    for t in (3.05, 4.96, 8.24, 10.13, 12.19):          # camera moves
        place(whoosh(0.6, 0.5), T(t), pan=0.2)
    place(slide(400, 1100, 0.32), T(3.76), -0.3, 0.35)  # sloppy
    place(boing(), T(4.16), 0.3, 0.6)
    place(slide(900, 250, 0.4), T(4.30), 0.4, 0.3)       # a bot falls over
    for i, t in enumerate((5.60, 6.00, 6.44, 6.84, 7.04, 7.92)):  # plan steps
        f = [523.25, 587.33, 659.25, 783.99, 880.0, 1046.5][i]
        place(tone(f, 0.35, 0.09) + 0.3 * tone(f * 2, 0.35, 0.05), T(t), (i - 2.5) / 4, 0.35)
    for i, t in enumerate((9.04, 9.20, 9.36, 9.56, 9.76)):        # gates
        f = [1046.5, 1174.7, 1318.5, 1568.0, 1760.0][i]
        place(tone(f, 0.8, 0.35) + 0.25 * tone(f * 3.01, 0.8, 0.12), T(t), (i - 2) / 3, 0.3)
    th = tone(62, 0.9, 0.25, 0.004) + 0.6 * tone(41, 0.9, 0.35, 0.01)  # worse off
    place(th, T(11.52), 0, 0.9)
    k = int(0.12 * sr)
    place(np.convolve(rng.standard_normal(k), np.ones(6) / 6, "same") * env(k, 0.001, 0.03), T(11.60), 0.3, 0.5)
    ec = plan["endcard"]["start"]                          # end-card cut: soft resolve
    place(whoosh(0.5, 0.4), ec - 0.45, -0.2)
    for f in (392.0, 523.25, 659.25):
        place(tone(f, 1.8, 0.7, 0.02), ec, 0, 0.18)

    st = np.stack([L, R], 1)
    peak = float(np.max(np.abs(st)))
    if peak > 0:
        st *= 0.9 / peak
    with wave.open(str(out), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((np.clip(st, -1, 1) * 32767).astype("<i2").tobytes())
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
    plan = build_plan(words, narr, dict(brand or {}))

    proj = out / "project"
    for d in (proj / "kit", proj / "assets" / "fonts", proj / "assets" / "brand"):
        d.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(PLUGIN / "kit" / "vendor" / "gsap.min.js", proj / "kit" / "gsap.min.js")
    shutil.copyfile(PLUGIN / "kit" / "endcard.js", proj / "kit" / "endcard.js")
    for f in ("LilitaOne.woff2", "ComicShanns.woff2"):
        shutil.copyfile(PLUGIN / "kit" / "fonts" / f, proj / "assets" / "fonts" / f)
    shutil.copyfile(PLUGIN / "kit" / "brand" / "keller-ai-logo.svg", proj / "assets" / "brand" / "keller-ai-logo.svg")

    # audio: F's SFX layer -> kit/sfx_bus (voice to -14 LUFS, SFX 14 dB under, gated by the loudness check)
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
    for token in PLACEHOLDERS:
        if token not in html:
            raise BuildError(f"template is missing placeholder {token}")
    inject = {k: v for k, v in plan.items() if k not in ("audio", "voice_source")}
    vo_dur = f"{min(probe_duration(Path(bus['voice'])), plan['duration']):.6f}"
    html = (html.replace("__F_VO_DURATION__", vo_dur)
                .replace("__F_DURATION__", f"{plan['duration']:.6f}")
                .replace("/*__F_DATA__*/null", json.dumps(inject, ensure_ascii=False).replace("</", "<\\/"))
                .replace("__F_TITLE__", (slug or wav.stem).replace("<", "")))
    (proj / "index.html").write_text(html)
    plan["project"] = str(proj)
    (out / "plan.json").write_text(json.dumps(plan, indent=1, ensure_ascii=False))
    return plan


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Instantiate the style F template.")
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
