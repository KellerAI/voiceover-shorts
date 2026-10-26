#!/usr/bin/env python3
"""Instantiate the style J (hand-drawn sketchbook explainer) template for any narration.

Canonical look: the reference lookdev (read only; nothing there is changed). The template keeps
its system (a cream sketchbook strip of four pages under a camera, whip-page transitions, navy
ink drawn live with a rough displacement, one vermilion marker, glyph-by-glyph handwriting with a
riding nib, circled chapter numbers, highlight swipes, doodles, the sloppy full-page slam, paper
grain) and drives it from the words file instead of the hard-coded r4 timings and copy.

Usage:
  python3 styles/J/build.py --wav VOICE.wav --words WORDS.json --out DIR
                            [--duration S] [--brand BRAND.json] [--content CONTENT.json]
                            [--slug S49] [--json]

Inputs (script-agnostic):
  --wav       narration WAV (any rate/channels; ffmpeg reads it).
  --words     word timings: a list of {text, start, end} (lookdev format) or {"words": [...]}.
  --duration  narration length in seconds; default: the WAV duration (ffprobe). Every word
              must end inside it. The composition runs past it to hold the end card.
  --brand     optional JSON {name, line, bg, fg, accent} for KAEndCard.mount (kit defaults).
  --content   optional JSON overriding derived copy and choices. Keys:
                labels  [4 str]   chapter labels (default "the <hero word>")
                doodles [4 str]   from doc|robots|patched|bank (default the canonical order)
                emph    [4 str]   circle|ul, the marker on each page's hero (default ul,circle,ul,ul)
                slam    false | {page, word, big, bang, sub}: the sloppy slam. Default: on the
                        last word of page 2, big = that word in caps, bang "!!!", no sub lines.
                        word is a word index into the words file.
                footer  str       footer line (default "Keller AI / <page 1 hero>")
  --slug      optional short id for the HTML title (default: the WAV stem).

How the words drive the picture:
  * Four pages like the canonical (edges 4.22 / 10.12 / 12.84 of 15.06 s). The three inner edges
    are word starts picked by dynamic programming near those proportions, preferring phrase
    starts, every page >= 1.6 s. The whip into a page starts when the previous page's last word
    ends (at most 0.35 s before the page's first word).
  * Every spoken word is written on its page, glyph by glyph across its own spoken span. Each
    page's hero (the highest-scoring content word: length plus 4 per repeat in the script, with
    an equally strong neighbour joined) gets its own big line and a marker circle or underline.
    The words before it are the kicker lines, the words after it the ink2 sub lines.
  * A doodle fills the space under the text, bottom-aligned, drawn bottom-first across the page.
  * The shared Keller AI end card (kit/endcard.js) cuts in 0.1 s after the last word, holds 2 s.

Output (DIR):
  DIR/project/   a HyperFrames project: index.html, kit/{gsap.min.js,endcard.js},
                 assets/{voice_norm.wav,sfx_bus.wav,mix.wav,fonts/,brand/}
  DIR/plan.json  the page plan injected into index.html (also printed with --json)
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
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN = HERE.parents[1]
sys.path.insert(0, str(PLUGIN))

from kit import sfx_bus  # noqa: E402

TEMPLATE = HERE / "template"
FPS = 30
ORIG = [0.0, 4.22, 10.12, 12.84, 15.06]  # r4-S49-J page edges (whips) and end
END_GAP = 0.1      # KAEndCard.DEFAULT_GAP
END_HOLD = 2.0     # KAEndCard.DEFAULT_HOLD
MIN_PAGE = 1.6
MID_PHRASE_COST = 6.0
WHIP_LEAD = 0.35
DOODLES = ("doc", "robots", "patched", "bank")
EMPHS = ("circle", "ul")
DEFAULT_EMPH = ["ul", "circle", "ul", "ul"]
# D4: bundled OFL faces (kit/fonts). The canonical's Marker Felt / Comic Sans MS fallbacks are gone.
FONTS = ["Virgil.woff2", "ComicShanns.woff2", "LilitaOne.woff2"]
CONTENT_KEYS = {"labels", "doodles", "emph", "slam", "footer"}

STOP = set("""a an the and or but if then so of in on at to for from by with as into onto over under about
this that these those it its it's i i'm i've me my we our you your he she they them their his her
is are was were be been being am do does did done have has had having will would can could should
may might must shall just very really also too than more most much many some any each every
there here what which who whom whose when where why how all both own same such only
s t don't doesn't isn't aren't wasn't weren't let's one
called based takes take took get gets got make makes made want wants say says said""".split())
STOP -= {"not", "no", "never"}


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
    c = clean(tok).lower().strip("'’")
    return len(c) >= 3 and c not in STOP


def phrase_ends(words: list[dict]) -> list[bool]:
    res = []
    for i, w in enumerate(words):
        punct = bool(re.search(r"[.,;:?!—–]$", w["text"].strip()))
        gap = i + 1 < len(words) and words[i + 1]["start"] - w["end"] >= 0.2
        res.append(punct or gap)
    return res


def pick_bounds(words: list[dict], cut: float) -> list[float]:
    """Four pages. Inner edges are word starts minimising sum((edge - proportional canonical
    edge)^2) + MID_PHRASE_COST per edge inside a phrase, every page >= MIN_PAGE."""
    n_in = len(ORIG) - 2
    if cut < (n_in + 1) * MIN_PAGE:
        raise BuildError(f"narration too short for four pages (cut at {cut:.2f} s)")
    ends = phrase_ends(words)
    cands = [(words[i]["start"], 0.0 if ends[i - 1] else MID_PHRASE_COST) for i in range(1, len(words))]
    cands = [c for c in cands if MIN_PAGE <= c[0] <= cut - MIN_PAGE]
    targets = [cut * ORIG[k] / ORIG[-1] for k in range(1, n_in + 1)]
    INF = float("inf")
    best = [[(INF, -1)] * len(cands) for _ in range(n_in)]
    for jx, (t, pen) in enumerate(cands):
        best[0][jx] = ((t - targets[0]) ** 2 + pen, -1)
    for k in range(1, n_in):
        for jx, (t, pen) in enumerate(cands):
            c0 = (t - targets[k]) ** 2 + pen
            for i in range(jx):
                if best[k - 1][i][0] < INF and t - cands[i][0] >= MIN_PAGE:
                    c = best[k - 1][i][0] + c0
                    if c < best[k][jx][0]:
                        best[k][jx] = (c, i)
    last = [(best[n_in - 1][jx][0], jx) for jx, (t, _) in enumerate(cands)
            if cut - t >= MIN_PAGE and best[n_in - 1][jx][0] < INF]
    if not last:
        raise BuildError("no page split keeps every page >= 1.6 s on word starts")
    jx = min(last)[1]
    inner = []
    for k in range(n_in - 1, -1, -1):
        inner.insert(0, cands[jx][0])
        jx = best[k][jx][1]
    return [0.0] + [round(b, 4) for b in inner] + [round(cut, 4)]


def pick_hero(page_words: list[dict], counts: Counter) -> tuple[int, int, int]:
    """Index range [a, b) of the page's hero (and the index of its top word): the best content word (length + 4 per repeat in the
    script, ties to the later word), joined with an adjacent content word scoring within 1."""
    def score(w):
        c = clean(w["text"]).lower()
        return len(c) + 4 * (counts[c] - 1)
    cands = [i for i, w in enumerate(page_words) if is_content(w["text"])]
    if not cands:
        cands = [max(range(len(page_words)), key=lambda i: len(clean(page_words[i]["text"])))]
    hi = max(cands, key=lambda i: (score(page_words[i]), i))
    hs = score(page_words[hi])
    best = None
    for nb in (hi - 1, hi + 1):
        if 0 <= nb < len(page_words) and is_content(page_words[nb]["text"]) and score(page_words[nb]) >= hs - 1:
            joined = len(clean(page_words[nb]["text"])) + len(clean(page_words[hi]["text"]))
            if joined <= 20 and (best is None or score(page_words[nb]) > score(page_words[best])):
                best = nb
    if best is None:
        return hi, hi + 1, hi
    return min(hi, best), max(hi, best) + 1, hi


def wd(w: dict) -> dict:
    return {"text": w["text"], "start": round(w["start"], 4), "end": round(max(w["end"], w["start"] + 0.12), 4)}


def build_plan(words: list[dict], duration: float, brand: dict, content: dict) -> dict:
    lwe = max(w["end"] for w in words)
    if lwe > duration + 1e-3:
        raise BuildError(f"last word ends at {lwe:.3f} s, after the narration ({duration:.3f} s)")
    unknown = set(content) - CONTENT_KEYS
    if unknown:
        raise BuildError(f"--content has unknown keys: {sorted(unknown)}")
    for key, allowed in (("doodles", DOODLES), ("emph", EMPHS)):
        v = content.get(key)
        if v is not None and (not isinstance(v, list) or len(v) != 4 or any(x not in allowed for x in v)):
            raise BuildError(f"--content {key} must be 4 of {list(allowed)}")
    labels = content.get("labels")
    if labels is not None and (not isinstance(labels, list) or len(labels) != 4):
        raise BuildError("--content labels must be a list of 4 strings")

    ec = endcard_plan(lwe)
    bounds = pick_bounds(words, ec["start"])
    counts = Counter(clean(w["text"]).lower() for w in words)
    pw = [[w for w in words if bounds[k] <= w["start"] < bounds[k + 1]] for k in range(4)]
    if any(not p for p in pw):
        raise BuildError("a page has no words")

    # whips: when the previous page's last word ends, at most WHIP_LEAD before the page's first word
    whips = [0.0]
    for k in range(1, 4):
        prev_end = max(w["end"] for w in pw[k - 1])
        t = max(prev_end, pw[k][0]["start"] - WHIP_LEAD, whips[-1] + 0.8)
        whips.append(round(t, 4))
    page_end = whips[1:] + [ec["start"]]

    slam = None
    sc = content.get("slam", "auto")
    if sc is not False and sc is not None:
        sc = {} if sc == "auto" else sc
        if not isinstance(sc, dict):
            raise BuildError("--content slam must be false or an object")
        if "word" in sc:
            wi = int(sc["word"])
            if not 0 <= wi < len(words):
                raise BuildError("--content slam.word is out of range")
            w = words[wi]
            page = next(k for k in range(4) if bounds[k] <= w["start"] < bounds[k + 1])
        else:
            page = int(sc.get("page", 1))
            if len(pw[page]) < 3:
                w = None
            else:
                w = pw[page][-1]
        if w is not None:
            big = clean(w["text"]).upper()[:10] or "YES"
            slam = {"page": page, "t": round(w["start"], 4), "big": str(sc.get("big", big)),
                    "bang": str(sc.get("bang", "!!!")), "sub": [str(s) for s in sc.get("sub", [])][:2],
                    "until": page_end[page]}

    doodles = content.get("doodles") or list(DOODLES)
    emph = content.get("emph") or list(DEFAULT_EMPH)
    pages = []
    for k in range(4):
        ws = pw[k]
        a, b, top = pick_hero(ws, counts)
        hero_label = clean(ws[top]["text"]).lower()
        label = str(labels[k]) if labels else ("the " + hero_label)[:16]
        t0 = 0.0 if k == 0 else round(whips[k] + 0.1, 4)  # ink lands with the page
        t1 = page_end[k] - 0.3
        if slam and slam["page"] == k:
            t1 = min(t1, slam["t"] - 0.1)
        t1 = round(max(t0 + 0.5, t1), 4)
        pages.append({
            "start": bounds[k], "end": page_end[k], "trans": whips[k] if k else None,
            "chapterT": 0.04 if k == 0 else round(whips[k] + 0.3, 4),
            "label": label,
            "body": [wd(w) for w in ws[:a]], "hero": [wd(w) for w in ws[a:b]], "after": [wd(w) for w in ws[b:]],
            "heroMax": 290 if b - a == 1 else 200,
            "emph": emph[k], "emphBy": round(page_end[k] - 0.25, 4),
            "doodle": doodles[k], "doodleT0": t0, "doodleT1": t1, "logoT": round(max(t0, t1 - 0.1), 4),
        })
    if content.get("footer"):
        footer = str(content["footer"])
    else:
        hero0 = " ".join(clean(w["text"]) for w in pages[0]["hero"])
        footer = "Keller AI / " + hero0
        while len(footer) > 32 and " " in hero0:
            hero0 = hero0.rsplit(" ", 1)[0]
            footer = "Keller AI / " + hero0
        footer = footer[:32]
    return {
        "orig": ORIG,
        "bounds": bounds,
        "whips": whips,
        "pages": pages,
        "slam": slam,
        "footer": footer,
        "firstWordStart": round(words[0]["start"], 4),
        "lastWordEnd": round(lwe, 4),
        "narration": round(duration, 6),
        "endcard": ec,
        "duration": ec["end"],
        "brand": brand,
    }


# ----------------------------------------------------------------------------- sfx (port of r4-S49-J sfx_gen.py)

def synth_sfx(plan: dict, out: Path, sr: int = 48000) -> Path:
    """The canonical J sound layer on this plan's times: felt-tip scratches on every written word
    and through each doodle, paper slides on the whips, a thump and jagged scrawl on the slam,
    and a stamp thump at the end-card cut. Seeded numpy, no samples."""
    import numpy as np

    dur = plan["duration"]
    N = int(round(sr * dur))
    L = np.zeros(N)
    R = np.zeros(N)
    rng = np.random.default_rng(49)

    def place(sig, t, pan=0.0, gain=1.0):
        i = int(t * sr)
        jx = min(N, i + len(sig))
        if i >= N or i < 0 or jx <= i:
            return
        s = sig[: jx - i] * gain
        L[i:jx] += s * np.sqrt(0.5 * (1 - pan))
        R[i:jx] += s * np.sqrt(0.5 * (1 + pan))

    def bandnoise(n, lo, hi):
        spec = np.fft.rfft(rng.standard_normal(n))
        f = np.fft.rfftfreq(n, 1 / sr)
        spec[(f < lo) | (f > hi)] = 0
        x = np.fft.irfft(spec, n)
        return x / (np.abs(x).max() + 1e-9)

    def scratch(d, lo=2500, hi=7000):
        n = max(64, int(d * sr))
        x = bandnoise(n, lo, hi)
        k = np.arange(n) / n
        return x * np.sin(np.pi * k) ** 0.6 * (0.6 + 0.4 * np.abs(bandnoise(n, 8, 40)))

    def slide(d):
        n = int(d * sr)
        k = np.arange(n) / n
        return bandnoise(n, 300, 2500) * np.sin(np.pi * k) ** 2

    n_th = int(0.25 * sr)
    thump = np.sin(2 * np.pi * 70 * np.arange(n_th) / sr) * np.exp(-np.arange(n_th) / (0.06 * sr))

    for p in plan["pages"]:
        for w in p["body"] + p["hero"] + p["after"]:
            place(scratch(min(0.8, max(0.08, w["end"] - w["start"]))), w["start"], float(rng.uniform(-0.3, 0.3)), 0.5)
        t = p["doodleT0"]
        while t < p["doodleT1"]:
            place(scratch(0.22), t, float(rng.uniform(-0.4, 0.4)), 0.35)
            t += 0.3
        if p["trans"] is not None:
            place(slide(0.5), p["trans"] - 0.03, 0.0, 0.7)
    s = plan["slam"]
    if s:
        place(thump, s["t"], 0.0, 1.0)
        for i in range(9):
            place(scratch(0.06, 1800, 6000), s["t"] + i * 0.045, (i % 3 - 1) * 0.4, 0.9)
    place(thump, plan["endcard"]["start"], 0.0, 0.8)

    st = np.stack([L, R], 1)
    peak = float(np.max(np.abs(st)))
    if peak > 0:
        st *= 0.9 / peak
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
    plan = build_plan(words, narr, dict(brand or {}), dict(content or {}))

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
    for token in ("__J_DURATION__", "__J_VO_DURATION__", "/*__J_DATA__*/null", "__J_TITLE__"):
        if token not in html:
            raise BuildError(f"template is missing placeholder {token}")
    inject = {k: v for k, v in plan.items() if k not in ("audio", "voice_source")}
    vo_dur = f"{min(probe_duration(Path(bus['voice'])), plan['duration']):.6f}"
    html = (html.replace("__J_VO_DURATION__", vo_dur)
                .replace("__J_DURATION__", f"{plan['duration']:.6f}")
                .replace("/*__J_DATA__*/null", json.dumps(inject, ensure_ascii=False).replace("</", "<\\/"))
                .replace("__J_TITLE__", (slug or wav.stem).replace("<", "")))
    (proj / "index.html").write_text(html)
    plan["project"] = str(proj)
    (out / "plan.json").write_text(json.dumps(plan, indent=1, ensure_ascii=False))
    return plan


def _load_obj(path: str | None, what: str) -> dict | None:
    if not path:
        return None
    try:
        obj = json.loads(Path(path).read_text())
    except (OSError, ValueError) as e:
        raise BuildError(f"cannot read --{what} {path}: {e}") from e
    if not isinstance(obj, dict):
        raise BuildError(f"--{what} must hold a JSON object")
    return obj


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Instantiate the style J template.")
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
