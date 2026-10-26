#!/usr/bin/env python3
"""Instantiate the style G (editorial print / riso collage) template for any narration.

Canonical source: the reference lookdev (read only; nothing there is changed).

Usage:
  python3 styles/G/build.py --wav VOICE.wav --words WORDS.json --out DIR
                            [--duration S] [--brand BRAND.json] [--slug S49] [--json]

Inputs (script-agnostic):
  --wav       narration WAV (any rate/channels; ffmpeg reads it).
  --words     word timings: a list of {text, start, end} (lookdev format) or {"words": [...]}.
  --duration  narration length in seconds; default: the WAV duration (ffprobe). Every word
              must end inside it. The composition runs past it to hold the end card.
  --brand     optional JSON: {name, line, bg, fg, accent, logo, masthead}. masthead is the
              magazine title (default "The Run Review"); the rest goes to KAEndCard.mount
              (kit/endcard.js defaults otherwise).
  --slug      optional short id shown as the issue number (default: the WAV stem).

Output (DIR):
  DIR/project/   a HyperFrames project: index.html, kit/{gsap.min.js,endcard.js},
                 assets/{voice_norm.wav,sfx_bus.wav,mix.wav,fonts/*.woff2,brand/keller-ai-logo.svg}
  DIR/plan.json  the page plan injected into index.html (also printed with --json)
  DIR/sfx_raw.wav the synthesised print-shop SFX layer before the bus
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
ORIG = [0.0, 3.36, 5.12, 8.86, 10.95, 12.2, 14.3]  # r2-15s-G page edges (p1..p6)
END_GAP = 0.1      # KAEndCard.DEFAULT_GAP
END_HOLD = 2.0     # KAEndCard.DEFAULT_HOLD
MIN_ACT = 0.9
DEFAULT_MASTHEAD = "The Run Review"
# D4: bundled OFL faces (kit/fonts) replace Comic Sans, Chalkduster and Marker Felt.
FONTS = ["LilitaOne.woff2", "ComicShanns.woff2", "Virgil.woff2"]
KICK_FALLBACK = ["In brief", "Exhibit A", "Method", "Approvals", "Outcome", "Coda"]

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
    """Mirror of KAEndCard.plan() with default gap/hold (the template re-checks it in JS)."""
    start = round(last_word_end + END_GAP, 6)
    end = math.ceil((max(start, last_word_end) + END_HOLD) * FPS - 1e-6) / FPS
    return {"start": start, "end": round(end, 6), "hold": END_HOLD}


def clean(tok: str) -> str:
    return re.sub(r"[^\w'’\-]", "", tok)


def is_content(tok: str) -> bool:
    t = clean(tok)
    return bool(t) and t.lower().strip("'’") not in STOP


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
    """Six pages like r2-15s-G. The five inner edges are word starts chosen by dynamic
    programming to minimise sum((edge - proportional G edge)^2) + MID_PHRASE_COST per edge
    that is not a phrase start, with every page >= MIN_ACT s. B[0]=0, B[6]=end-card cut."""
    if cut < 6 * MIN_ACT:
        raise BuildError(f"narration too short for six pages (cut at {cut:.2f} s)")
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
    else:  # too few words for word-aligned edges: use the proportional points
        inner = targets
    return [0.0] + [round(b, 4) for b in inner] + [round(cut, 4)]


def split_headline(idx: list[int], words: list[dict], max_words: int = 3, max_chars: int = 20) -> tuple[list[int], list[int]]:
    """(lead, hl): hl is the act's trailing words (<= max_words, <= max_chars) with leading
    stop words pushed back into the lead, so the headline starts on a content word."""
    hl: list[int] = []
    for i in reversed(idx):
        n = sum(len(words[j]["text"]) + 1 for j in hl) + len(words[i]["text"])
        if hl and (len(hl) >= max_words or n > max_chars):
            break
        hl.insert(0, i)
    while len(hl) > 1 and not is_content(words[hl[0]]["text"]):
        hl.pop(0)
    lead = [i for i in idx if i < hl[0]] if hl else list(idx)
    return lead, hl


def kicker(k: int) -> str:
    """Magazine section label for page k (the spoken words go in the lead and headline)."""
    return KICK_FALLBACK[k] + " —"


def pick_word(idx: list[int], words: list[dict], maxlen: int, fallback: list[str]) -> tuple[str, int | None]:
    """Longest content word of the act that fits maxlen letters (uppercase, stripped)."""
    best, bi = "", None
    for i in idx:
        t = re.sub(r"[^\w]", "", words[i]["text"])
        if is_content(t) and len(t) <= maxlen and len(t) > len(best):
            best, bi = t, i
    if not best:
        for t in fallback:
            if len(t) <= maxlen:
                return t.upper(), None
        return "RUN", None
    return best.upper(), bi


def keywords(words: list[dict]) -> list[str]:
    seen, out = set(), []
    for w in words:
        t = clean(w["text"]).strip("'’")
        if not t or not is_content(t) or len(t) < 4:
            continue
        if t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out


def spread(items: list[str], n: int, maxlen: int, offset: int = 0) -> list[str]:
    items = [i for i in items if len(i) <= maxlen] or [i[:maxlen] for i in items] or ["note"]
    if len(items) <= n:
        return [items[(i + offset) % len(items)] for i in range(n)]
    return [items[(round(i * (len(items) - 1) / (n - 1)) + offset) % len(items)] for i in range(n)]


def even_times(t0: float, t1: float, n: int) -> list[float]:
    if n <= 1:
        return [round(t0, 4)]
    return [round(t0 + (t1 - t0) * i / (n - 1), 4) for i in range(n)]


def caption_groups(words: list[dict]) -> list[list[int]]:
    """Caption chunks: break after a phrase end, at 4 words, or past 22 characters."""
    ends = phrase_ends(words)
    groups, cur, chars = [], [], 0
    for i, w in enumerate(words):
        if cur and (len(cur) >= 4 or chars + len(w["text"]) > 22):
            groups.append([cur[0], cur[-1] + 1])
            cur, chars = [], 0
        cur.append(i)
        chars += len(w["text"]) + 1
        if ends[i]:
            groups.append([cur[0], cur[-1] + 1])
            cur, chars = [], 0
    if cur:
        groups.append([cur[0], cur[-1] + 1])
    return groups


def build_plan(words: list[dict], duration: float, slug: str, brand: dict) -> dict:
    lwe = max(w["end"] for w in words)
    if lwe > duration + 1e-3:
        raise BuildError(f"last word ends at {lwe:.3f} s, after the narration ({duration:.3f} s)")
    ec = endcard_plan(lwe)
    B = pick_bounds(words, ec["start"])
    kws = keywords(words)
    pages = []
    for k in range(6):
        idx = [i for i, w in enumerate(words) if B[k] <= w["start"] < B[k + 1]]
        lead, hl = split_headline(idx, words, 2 if k == 4 else 3, 12 if k == 4 else 20)
        pages.append({"start": B[k], "end": B[k + 1], "idx": idx, "lead": lead, "hl": hl,
                      "kicker": kicker(k), "text": " ".join(words[i]["text"] for i in idx)})

    def t_of(i, dflt):
        return round(words[i]["start"], 4) if i is not None else dflt

    # p2: ransom-note word, letters slapped down from its spoken start
    r2, r2i = pick_word(pages[1]["idx"], words, 10, kws)
    r2t0 = max(B[1] + 0.3, min(t_of(r2i, B[1] + 0.4), B[2] - 0.8))
    r2step = round(min(0.065, 0.7 / max(1, len(r2))), 4)
    # p3: grid word snaps into cells; six plan rows tick across the page
    cells, _ = pick_word(pages[2]["idx"], words, 9, kws)
    snap0 = round(B[2] + 0.38, 4)
    ticks = even_times(B[2] + 0.75, B[3] - 0.25, 6)
    # p4: four approval stamps; the fourth lands late, crooked, in the wrong ink
    a, b = B[3] + 0.18, B[4]
    stamps = even_times(a, a + (b - a) * 0.55, 3) + [round(max(a + (b - a) * 0.55 + 0.25, b - 0.45), 4)]
    # p5: the act's last words return as a ransom note
    r5 = " ".join(re.sub(r"[^\w'’.,!?\-]", "", words[i]["text"]) for i in pages[4]["hl"]).strip() or "worse off."
    r5 = r5[:14]
    r5t0 = round(max(B[4] + 0.25, words[pages[4]["hl"][0]]["start"] - 0.05) if pages[4]["hl"] else B[4] + 0.4, 4)
    r5t0 = min(r5t0, round(B[5] - 0.6, 4))
    r5step = round(min(0.05, 0.5 / max(1, len(r5))), 4)
    masthead = str(brand.pop("masthead", DEFAULT_MASTHEAD))[:22]
    return {
        "bounds": B,
        "pages": pages,
        "words": [{"t": w["text"], "s": round(w["start"], 4), "e": round(w["end"], 4)} for w in words],
        "capGroups": caption_groups(words),
        "ransom2": {"text": r2, "t0": round(r2t0, 4), "step": r2step},
        "cells": {"text": cells, "t0": snap0},
        "rows": spread(kws, 6, 22),
        "ticks": ticks,
        "form": spread(kws, 4, 18, offset=1),
        "stamps": stamps,
        "ransom5": {"text": r5, "t0": r5t0, "step": r5step},
        "firstWordStart": round(words[0]["start"], 4),
        "lastWordEnd": round(lwe, 4),
        "narration": round(duration, 6),
        "endcard": ec,
        "duration": ec["end"],
        "slug": slug,
        "masthead": masthead,
        "brand": brand,
    }


# ----------------------------------------------------------------------------- sfx (port of r2-15s-G sfx_gen.py)

def synth_sfx(plan: dict, out: Path, sr: int = 48000) -> Path:
    """r2-15s-G's print-shop layer (paper swishes, scissor snips, rubber-stamp thuds),
    placed on this plan's event times. Seeded numpy, no samples."""
    import numpy as np

    rng = np.random.default_rng(7)
    n = int(round(sr * plan["duration"]))
    buf = np.zeros(n)

    def add(t, sig):
        i = int(t * sr)
        if i >= n:
            return
        j = min(n, i + len(sig))
        buf[i:j] += sig[: j - i]

    def env(k, a=0.002, d=0.1):
        tt = np.arange(k) / sr
        return np.minimum(tt / a, 1) * np.exp(-tt / d)

    def swish(length=0.42, gain=0.55):
        k = int(length * sr)
        x = rng.standard_normal(k)
        x = np.convolve(x, np.ones(18) / 18, "same") - np.convolve(x, np.ones(220) / 220, "same")
        return x * np.sin(np.linspace(0, np.pi, k)) ** 2 * gain

    def stamp(gain=1.0):
        k = int(0.28 * sr)
        tt = np.arange(k) / sr
        body = np.sin(2 * np.pi * (95 - 40 * tt / 0.28) * tt) * env(k, 0.001, 0.07)
        slap = rng.standard_normal(k) * env(k, 0.0005, 0.012) * 0.6
        return (body + slap) * gain

    def snip(gain=0.35):
        k = int(0.04 * sr)
        return rng.standard_normal(k) * env(k, 0.0003, 0.006) * gain

    B = plan["bounds"]
    for t in [0.05] + B[1:6]:
        add(max(0.0, t - 0.05), swish())
    r2 = plan["ransom2"]
    for i in range(len(r2["text"])):
        add(r2["t0"] + i * r2["step"], snip(0.5 + 0.2 * (i % 3)))
    c = plan["cells"]
    for i in range(len(c["text"])):
        add(c["t0"] + i * 0.045 + 0.3, snip(0.3))
    for t in plan["ticks"]:
        add(t, snip(0.45))
    for i, t in enumerate(plan["stamps"]):
        add(t, stamp(0.8 if i == 3 else (1.0 if i < 2 else 1.25)))
    r5 = plan["ransom5"]
    for i, ch in enumerate(r5["text"]):
        if ch != " ":
            add(r5["t0"] + i * r5["step"], snip(0.5))
    add(plan["endcard"]["start"], stamp(0.6))  # the end card lands like a page slapped flat

    peak = float(np.max(np.abs(buf))) or 1.0
    buf = buf / peak * 10 ** (-19 / 20)
    st = np.stack([buf, buf * 0.96], 1)
    with wave.open(str(out), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((st * 32767).astype("<i2").tobytes())
    return out


# ----------------------------------------------------------------------------- build

TOKENS = ("__G_DURATION__", "__G_VO_DURATION__", "/*__G_DATA__*/null", "__G_TITLE__")


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
    slug = (slug or wav.stem)[:16]
    plan = build_plan(words, narr, slug, brand)

    proj = out / "project"
    for d in ("kit", "assets/fonts", "assets/brand"):
        (proj / d).mkdir(parents=True, exist_ok=True)
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
    for token in TOKENS:
        if token not in html:
            raise BuildError(f"template is missing placeholder {token}")
    inject = {k: v for k, v in plan.items() if k not in ("audio", "voice_source")}
    vo_dur = f"{min(probe_duration(Path(bus['voice'])), plan['duration']):.6f}"
    html = (html.replace("__G_VO_DURATION__", vo_dur)
                .replace("__G_DURATION__", f"{plan['duration']:.6f}")
                .replace("/*__G_DATA__*/null", json.dumps(inject, ensure_ascii=False).replace("</", "<\\/"))
                .replace("__G_TITLE__", slug.replace("<", "")))
    (proj / "index.html").write_text(html)
    plan["project"] = str(proj)
    (out / "plan.json").write_text(json.dumps(plan, indent=1, ensure_ascii=False))
    return plan


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Instantiate the style G template.")
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
