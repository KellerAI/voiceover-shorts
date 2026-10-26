#!/usr/bin/env python3
"""Instantiate the style P (VHS camcorder tape) template for any narration.

Canonical look: the reference lookdev (read only). The template keeps its system: home-video tape
playback under a persistent OSD (REC, timecode, SP/STOP, date stamp), CRT scanlines, seeded per-frame
noise, chroma bleed and a tracking-error roll with an FF/REW glyph at every cut, over six scenes:
a blue PLAY screen, a cassette label with the title written on in red marker, a chapter headline over
a REPEAT COUNTER, a headline over two CRT TVs with the sloppy "LGTM!!" reviewer gag, a brief card with
a headline stack and one boxed slam word, and a final panel whose key word slams in. The hard-coded
S49 cuts and copy are replaced by a plan derived from the words file.

Usage:
  python3 styles/P/build.py --wav VOICE.wav --words WORDS.json --out DIR
                            [--duration S] [--brand BRAND.json] [--content CONTENT.json]
                            [--slug S49] [--json]

Inputs (script-agnostic):
  --wav       narration WAV (any rate/channels; ffmpeg reads it).
  --words     word timings: a list of {text, start, end} (lookdev format) or {"words": [...]}.
  --duration  narration length in seconds; default: the WAV duration (ffprobe). Every word must
              end inside it. The composition runs past it to hold the end card.
  --brand     optional JSON {logo, name, line, bg, fg, accent} for KAEndCard.mount. P's defaults:
              name "KELLER AI", bg cream #FBF7F0, fg navy #10243E, accent #8B9090 (the cream card
              is a hard scene change from the navy tape scenes). "name" also prints on the label.
  --content   optional JSON overriding default copy, keys of DEFAULT_CONTENT.
  --slug      optional short id (default: the WAV stem); used in the page title.

How the words drive the tape:
  * Chapters: the words split into five groups at clause ends ([.,;:?!]); when there are more,
    the split with the most even chapter lengths wins; when there are fewer, the largest pauses
    are used. Every chapter is at least MIN_CHAPTER seconds. Each later chapter cuts in 0.05 s
    before its first word, with a tracking roll (REW on chapter 4, FF elsewhere).
  * Chapter 1 (cassette): the last two content words are the title, written on in red marker on
    their first word; up to four words before them are the label line above it.
  * Chapter 2 (counter): headline = the clause from its first content word through the second;
    the rest is typed as the OSD sub-line on its first word.
  * Chapter 3 (TVs): headline = the clause from its first content word; the magnifier sweeps at
    35 % of the chapter and the stamp lands on the word nearest 60 %.
  * Chapter 4 (brief card): the lead phrase (through its second content word) is typed on the
    card; then the stack: words before the slam word, the boxed slam word (the first negation,
    else the longest content word) on its spoken word, and the words after it.
  * Chapter 5 (final panel): the header is the clause before its last content word, which slams
    in on its spoken word; a TAPE REMAINING panel counts down to the end card.
  * The shared Keller AI end card (kit/endcard.js) cuts in 0.1 s after the last word and holds
    2 s with a slow push (no frozen frames). OSD switches to STOP 0.15 s before the last word ends.

Output (DIR):
  DIR/project/   a HyperFrames project: index.html, kit/{gsap.min.js,endcard.js},
                 assets/{voice_norm.wav,sfx_bus.wav,mix.wav,fonts/,brand/}
  DIR/plan.json  the plan injected into index.html (also printed with --json)
  DIR/sfx_raw.wav the synthesised deck SFX before the bus
Render it with:
  cd DIR && npm_config_offline=true sandbox-exec -f <repo>/kit/offline.sb \\
      npx hyperframes@0.8.183 render project -w 2 -q delivery -o look.mp4

Exit: 0 built, 1 the SFX bus loudness gate failed, 2 usage or IO error (message on stderr).
"""
from __future__ import annotations

import argparse
import itertools
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
END_GAP = 0.1       # KAEndCard.DEFAULT_GAP
END_HOLD = 2.0      # KAEndCard.DEFAULT_HOLD
INTRO = 0.6         # blue PLAY screen
CUT_LEAD = 0.05     # a chapter cuts in this long before its first word
MIN_CHAPTER = 1.6   # seconds on screen per chapter
N_CHAPTERS = 5
FONTS = ["LilitaOne.woff2", "ComicShanns.woff2", "Virgil.woff2"]
# line caps (chars) so every auto-fit stays above its floor with the bundled fonts
HEAD_LINE = 20      # Lilita headline line at 900 px, floor 64 px
HEAD_CHARS = 40
L1_CHARS = 22       # Virgil label line at 680 px, floor 48 px
TITLE_CHARS = 22    # Virgil title at 680 px, floor 52 px
NP_CHARS = 24       # Lilita NOW PLAYING at 828 px, floor 60 px
SUB_CHARS = 22      # Comic Shanns 66 px OSD line, 900 px
LEAD_CHARS = 22     # Lilita card lead at 820 px, floor 60 px
STACK_CHARS = 16    # Lilita stack lines at 900 px, floor 64 px
PRE2_CHARS = 8
SLAM_CHARS = 10
PH_CHARS = 24       # Comic Shanns 48 px panel header at ~860 px
CAP_CHARS = 26      # Comic Shanns 48 px counter caption at 800 px

STOP = set("""a an the and or but if then so of in on at to for from by with as into onto over under about
this that these those it its it's i i'm i've me my we our you your he she they them their his her
is are was were be been being am do does did done have has had having will would can could should
may might must shall just very really also too than more most much many some any each every
there here what which who whom whose when where why how all both own same such only
s t don't doesn't isn't aren't wasn't weren't let's one
called based takes take took get gets got make makes made want wants say says said""".split())
STOP -= {"not", "no", "never"}  # negations are content
NEGATIONS = {"not", "no", "never", "don't", "doesn't", "isn't", "aren't", "wasn't", "weren't", "can't", "won't"}
LEADING_BACKUP = {"a", "an", "the", "one", "not", "no", "this", "its", "our", "your", "their", "my"}
CONJ = {"and", "but", "so", "or", "then", "yet"}
ICON_WORDS = {"bank": ("bank", "banks", "banking", "banker", "bankers", "finance", "financial", "money", "loan", "loans")}

DEFAULT_CONTENT = {
    "tape_chip": "TAPE 01 / HOME VIDEO",
    "counter_caption": "",                      # default "SAME <last headline word>. AGAIN. AGAIN."
    "counter_end": 7108,
    "doc": [["2+2=", "5"], ["sky = ", "green"], ["cite: ", "trust me"]],
    "stamp": ["LGTM!!", "looks fine :)", "✔✔✔ 100%"],
    "reviewer": "REVIEWER: A LOOKALIKE",
    "tv_a": "MODEL A",
    "knob_a": "REVIEWER ◉",
    "knob_b": "MODEL B ◉ ◉",
    "card_head": "",                            # default "BRIEF · <TITLE>"
    "icon": "",                                 # "bank" | "tape"; default by the slam word
    "date": ["PM 4:12", "OCT.22 1994"],
}

DEFAULT_BRAND = {"name": "KELLER AI", "bg": "#FBF7F0", "fg": "#10243E", "accent": "#8B9090"}


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
            t = str(w["text"]).strip()
            s, e = float(w["start"]), float(w["end"])
        except (KeyError, TypeError, ValueError) as e:
            raise BuildError(f"{path}: bad word entry {w!r}") from e
        if t:
            out.append({"text": t, "start": s, "end": max(e, s)})
    if not out:
        raise BuildError(f"{path}: no non-empty words")
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


# ----------------------------------------------------------------------------- copy helpers

def clean(tok: str) -> str:
    return re.sub(r"[^\w'’\-%]", "", tok)


def low(tok: str) -> str:
    return clean(tok).lower().replace("’", "'").strip("'")


def is_content(tok: str) -> bool:
    c = low(tok)
    return bool(c) and c not in STOP


def ends_clause(tok: str) -> bool:
    return bool(re.search(r"[.,;:?!—–]['\"’”]?$", tok.strip()))


def strip_punct(s: str) -> str:
    return re.sub(r"[.,;:?!—–\"“”]+$", "", s.strip())


def words_text(ws: list[dict], upper: bool = True) -> str:
    s = " ".join(strip_punct(w["text"]) for w in ws).strip()
    s = re.sub(r"[\"“”]", "", s)
    return s.upper() if upper else s


def cap_words(ws: list[dict], n: int, *, keep: str = "head") -> list[dict]:
    """Longest run of ws (from the head, or the tail) whose text fits n chars."""
    ws = list(ws)
    while len(ws) > 1 and len(words_text(ws)) > n:
        ws = ws[:-1] if keep == "head" else ws[1:]
    return ws


def trunc(s: str, n: int) -> str:
    s = s.strip()
    return s if len(s) <= n else s[: max(1, n - 1)].rstrip() + "~"


def two_lines(ws: list[dict], line: int = HEAD_LINE, total: int = HEAD_CHARS) -> list[str]:
    """Split a headline into one or two balanced lines, each <= line chars."""
    ws = cap_words(ws, total)
    txt = [strip_punct(w["text"]).upper() for w in ws]
    if len(" ".join(txt)) <= line or len(txt) == 1:
        return [trunc(" ".join(txt), line)]
    best = None
    for k in range(1, len(txt)):
        a, b = " ".join(txt[:k]), " ".join(txt[k:])
        score = max(len(a), len(b))
        if best is None or score < best[0]:
            best = (score, a, b)
    if best is None:
        raise ValueError("two_lines: no split candidate found for headline")
    _, a, b = best
    while len(b) > line and " " in b:
        b = b.rsplit(" ", 1)[0]
    return [trunc(a, line), trunc(b, line)]


def from_first_content(ws: list[dict]) -> list[dict]:
    """Drop leading function words, keeping one article/numeral/negation right before the first content word."""
    idx = next((i for i, w in enumerate(ws) if is_content(w["text"])), None)
    if idx is None:
        return ws
    if idx > 0 and low(ws[idx - 1]["text"]) in LEADING_BACKUP:
        idx -= 1
    return ws[idx:]


def endcard_plan(last_word_end: float) -> dict:
    """Mirror of KAEndCard.plan() with default gap/hold (the template re-checks it in JS)."""
    start = round(last_word_end + END_GAP, 6)
    end = math.ceil((max(start, last_word_end) + END_HOLD) * FPS - 1e-6) / FPS
    return {"start": start, "end": round(end, 6), "hold": END_HOLD}


# ----------------------------------------------------------------------------- chapters

def split_chapters(words: list[dict], ec_start: float) -> list[int]:
    """Return the 4 word indices where chapters 2..5 begin."""
    n = len(words)

    def cut_at(i: int) -> float:
        return words[i]["start"] - CUT_LEAD

    def ok(bounds: tuple[int, ...]) -> bool:
        cuts = [INTRO] + [cut_at(i) for i in bounds] + [ec_start]
        return all(b - a >= MIN_CHAPTER - 1e-9 for a, b in zip(cuts, cuts[1:]))

    def cost(bounds: tuple[int, ...], punct: set[int]) -> float:
        cuts = [INTRO] + [cut_at(i) for i in bounds] + [ec_start]
        durs = [b - a for a, b in zip(cuts, cuts[1:])]
        mean = sum(durs) / len(durs)
        return sum((d - mean) ** 2 for d in durs) + 4.0 * sum(1 for i in bounds if i not in punct)

    punct = {i + 1 for i in range(n - 1) if ends_clause(words[i]["text"])}
    gaps = sorted((i + 1 for i in range(n - 1)), key=lambda i: words[i]["start"] - words[i - 1]["end"], reverse=True)
    for pool in (sorted(punct), sorted(punct | set(gaps[:12])), list(range(1, n))):
        pool = [i for i in pool if cut_at(i) >= INTRO + MIN_CHAPTER - 1e-9]
        if len(pool) > 28:  # keep the search small: clause ends first, then the largest pauses
            keep = set(i for i in pool if i in punct) | set(i for i in gaps if i in pool)
            pool = sorted(list(keep))[:28] if len(keep) <= 28 else sorted(
                sorted(keep, key=lambda i: (i not in punct, -(words[i]["start"] - words[i - 1]["end"])))[:28])
        best = None
        for combo in itertools.combinations(pool, N_CHAPTERS - 1):
            if ok(combo):
                c = cost(combo, punct)
                if best is None or c < best[0]:
                    best = (c, combo)
        if best:
            return list(best[1])
    raise BuildError(f"narration too short for five {MIN_CHAPTER} s chapters "
                     f"(end card at {ec_start:.2f} s, {n} words)")


# ----------------------------------------------------------------------------- plan

def build_plan(words: list[dict], duration: float, slug: str, brand: dict, content: dict) -> dict:
    del slug  # the slug only titles the page (build())
    lwe = max(w["end"] for w in words)
    if lwe > duration + 1e-3:
        raise BuildError(f"last word ends at {lwe:.3f} s, after the narration ({duration:.3f} s)")
    unknown = set(content) - set(DEFAULT_CONTENT)
    if unknown:
        raise BuildError(f"--content has unknown keys: {sorted(unknown)}")
    co = dict(DEFAULT_CONTENT)
    co.update(content)
    for key, n in (("doc", 3), ("stamp", 3), ("date", 2)):
        if not isinstance(co[key], list) or len(co[key]) != n:
            raise BuildError(f"--content {key} must be a list of {n}")
    if not all(isinstance(r, list) and len(r) == 2 for r in co["doc"]):
        raise BuildError("--content doc rows must be [plain, red] pairs")
    br = dict(DEFAULT_BRAND)
    br.update({k: v for k, v in brand.items() if v is not None})
    ec = endcard_plan(lwe)

    bounds = split_chapters(words, ec["start"])
    edges = [0] + bounds + [len(words)]
    G = [words[a:b] for a, b in zip(edges, edges[1:])]
    cuts = [INTRO] + [round(words[i]["start"] - CUT_LEAD, 4) for i in bounds]
    glyphs = ["&#9654;&#9654; FF"] * 5
    glyphs[3] = "&#9664;&#9664; REW"

    # chapter 1: cassette label line + title written on in marker
    g1 = G[0]
    cont1 = [i for i, w in enumerate(g1) if is_content(w["text"])]
    if cont1:
        ti = cont1[-2:] if len(cont1) >= 2 and cont1[-1] - cont1[-2] <= 2 else cont1[-1:]
        title_ws = g1[ti[0]:ti[-1] + 1]
    else:
        title_ws = g1[-1:]
    t0 = g1.index(title_ws[0])
    l1_ws = cap_words(g1[max(0, t0 - 4):t0], L1_CHARS - 1, keep="tail") if t0 > 0 else []
    title_text = trunc(words_text(title_ws, upper=False), TITLE_CHARS)
    title_t = max(title_ws[0]["start"], cuts[0] + 0.05)
    wipe = min(1.0, max(0.5, title_ws[-1]["end"] - title_ws[0]["start"]))
    np_pre_ws = [w for w in l1_ws if is_content(w["text"])] or l1_ws
    ch1 = {
        "l1": {"text": (words_text(l1_ws, upper=False) + "…") if l1_ws else "", "t": round(max(l1_ws[0]["start"], cuts[0]) if l1_ws else cuts[0], 4)},
        "title": {"text": title_text, "t": round(title_t, 4), "wipe": round(wipe, 3), "np": trunc(title_text.upper(), NP_CHARS)},
        "np": trunc(words_text(np_pre_ws) if np_pre_ws else "NOW PLAYING", NP_CHARS),
    }

    # chapter 2: headline through the second content word, typed sub-line for the rest
    g2 = from_first_content(G[1])
    ci = [i for i, w in enumerate(g2) if is_content(w["text"])]
    cut2 = ci[1] + 1 if len(ci) >= 2 else len(g2)
    head2, sub2 = g2[:cut2], g2[cut2:]
    head2_lines = two_lines(head2)
    sub_ws = cap_words(sub2, SUB_CHARS) if sub2 else []
    last_head = head2_lines[-1].split(" ")[-1]
    ch2 = {
        "head": head2_lines,
        "sub": {"text": words_text(sub_ws) if sub_ws else "", "t": round(sub_ws[0]["start"], 4) if sub_ws else 0},
        "caption": trunc(co["counter_caption"] or f"SAME {last_head}. AGAIN. AGAIN.", CAP_CHARS),
        "counterEnd": int(co["counter_end"]) % 10000,
        "counterT": round(cuts[1] + 0.5, 4),
        "counterDur": round(max(0.3, (cuts[2] - cuts[1]) - 0.6), 4),
    }

    # chapter 3: headline over the TVs; magnifier at 35 %, stamp on the word nearest 60 %
    g3 = G[2]
    k3, d3 = cuts[2], cuts[3] - cuts[2]
    target = k3 + 0.6 * d3
    stamp_t = min((w["start"] for w in g3), key=lambda s: abs(s - target))
    stamp_t = min(max(stamp_t, k3 + 0.9), cuts[3] - 0.5)
    ch3 = {
        "head": two_lines(from_first_content(g3)),
        "magT": round(min(k3 + 0.35 * d3, stamp_t - 0.5), 4),
        "stampT": round(stamp_t, 4),
        "doc": [[str(a), str(b)] for a, b in co["doc"]],
        "stamp": [str(s) for s in co["stamp"]],
        "reviewer": trunc(str(co["reviewer"]), 30),
        "tvA": trunc(str(co["tv_a"]), 12), "knobA": trunc(str(co["knob_a"]), 10), "knobB": trunc(str(co["knob_b"]), 20),
    }

    # chapter 4: lead phrase on the card, then the stack around the boxed slam word
    g4 = G[3]
    ci4 = [i for i, w in enumerate(g4) if is_content(w["text"])]
    lead_n = (ci4[1] + 1) if len(ci4) >= 3 else (ci4[0] + 1 if len(ci4) == 2 else 0)
    lead_n = min(lead_n, 4, max(0, len(g4) - 1))
    lead_ws, rest = g4[:lead_n], g4[lead_n:]
    neg = [i for i, w in enumerate(rest) if low(w["text"]) in NEGATIONS]
    if neg:
        si = neg[0]
    else:
        cand = [i for i, w in enumerate(rest) if is_content(w["text"])] or list(range(len(rest)))
        si = max(cand, key=lambda i: len(clean(rest[i]["text"])))
    pre, slam_w, post = rest[:si], rest[si], rest[si + 1:]
    pre1, pre2 = (pre[:-1], pre[-1:]) if len(pre) >= 2 else (pre, [])
    pre1 = cap_words(pre1, STACK_CHARS, keep="tail")
    pre2 = cap_words(pre2, PRE2_CHARS) if pre2 and len(words_text(pre2)) <= PRE2_CHARS else []
    post = cap_words(post, STACK_CHARS)
    title_up = title_text.upper()
    card_head = co["card_head"] or (f"BRIEF · {title_up}" if len(title_up) <= 16 else title_up)
    glide_t = (pre1 or pre2 or [slam_w])[0]["start"] - 0.08
    ch4 = {
        "cardHead": trunc(card_head, 24),
        "lead": (trunc(words_text(lead_ws), LEAD_CHARS - 1) + ":") if lead_ws else "",
        "pre1": {"text": words_text(pre1) if pre1 else "", "t": round(pre1[0]["start"], 4) if pre1 else 0},
        "pre2": {"text": words_text(pre2) if pre2 else "", "t": round(pre2[0]["start"], 4) if pre2 else 0},
        "slam": {"text": trunc(words_text([slam_w]), SLAM_CHARS), "t": round(slam_w["start"], 4)},
        "post": {"text": words_text(post) if post else "", "t": round(post[0]["start"], 4) if post else 0},
        "glideT": round(max(cuts[3] + 0.3, glide_t), 4),
    }
    ch4["pauseT"] = round(max(ch4["slam"]["t"], ch4["post"]["t"]) + 0.32, 4)

    # chapter 5: header before the last content word, which slams in on its word
    g5 = G[4]
    ci5 = [i for i, w in enumerate(g5) if is_content(w["text"])]
    s5 = ci5[-1] if ci5 else len(g5) - 1
    head5 = g5[:s5]
    while head5 and low(head5[0]["text"]) in CONJ:
        head5 = head5[1:]
    head5 = cap_words(head5, PH_CHARS, keep="tail") if head5 else []
    slam5 = g5[s5]
    icon = co["icon"] or next((k for k, ws in ICON_WORDS.items()
                               if any(low(w["text"]) in ws for w in g5)), "tape")
    if icon not in ("bank", "tape"):
        raise BuildError("--content icon must be 'bank' or 'tape'")
    ch5 = {
        "head": words_text(head5) if head5 else "FINAL CHAPTER",
        "slam": {"text": trunc(words_text([slam5]), 12), "t": round(max(slam5["start"], cuts[4] + 0.2), 4)},
        "icon": icon,
    }

    return {
        "cuts": cuts,
        "glyphs": glyphs,
        "chapters": [{"first_word": words[a]["text"], "start": round(words[a]["start"], 4)} for a in edges[:-1]],
        "ch1": ch1, "ch2": ch2, "ch3": ch3, "ch4": ch4, "ch5": ch5,
        "tapeChip": trunc(str(co["tape_chip"]), 24),
        "brandName": trunc(str(br.get("name") or "KELLER AI"), 16),
        "date": [trunc(str(d), 12) for d in co["date"]],
        "stopT": round(lwe - 0.15, 4),
        "firstWordStart": round(words[0]["start"], 4),
        "lastWordEnd": round(lwe, 4),
        "narration": round(duration, 6),
        "endcard": ec,
        "duration": ec["end"],
        "brand": br,
    }


# ----------------------------------------------------------------------------- sfx (port of r7-S49-P sfx_gen.py)

def synth_sfx(plan: dict, out: Path, sr: int = 48000) -> Path:
    """Tape clunks, a motor whir into every FF/REW roll, a stamp thud, the slam hits with a 55 Hz
    drop, a STOP clunk and a soft clunk on the end-card cut. Seeded numpy, no samples."""
    import numpy as np

    N = int(round(plan["duration"] * sr))
    buf = np.zeros(N)
    rng = np.random.default_rng(7)

    def env(n, dec):
        return np.exp(-np.arange(n) / sr * dec)

    def clunk(v=1.0):
        n = int(0.12 * sr)
        return v * (rng.uniform(-1, 1, n) * env(n, 60) + np.sin(2 * np.pi * 90 * np.arange(n) / sr) * env(n, 30))

    def whir(d, f0, f1, v=0.5):
        n = int(d * sr)
        t = np.arange(n) / sr
        fr = np.linspace(f0, f1, n)
        s = np.sin(2 * np.pi * np.cumsum(fr) / sr)
        return v * (s * 0.6 + rng.uniform(-1, 1, n) * 0.3) * np.minimum(1, np.minimum(t / 0.03, (d - t) / 0.06))

    def add(t, s):
        i = int(t * sr)
        if i < 0 or i >= N:
            return
        s = s[: N - i]
        buf[i:i + len(s)] += s

    add(0.02, clunk())
    add(0.05, whir(0.5, 60, 120, 0.3))
    for c, g in zip(plan["cuts"], plan["glyphs"]):
        a, b = (900, 300) if "REW" in g else (300, 900)
        add(c - 0.2, whir(0.45, a, b))
        add(c + 0.25, clunk(0.6))
    add(plan["ch3"]["stampT"], clunk(1.2))
    st = plan["ch4"]["slam"]["t"]
    add(st, clunk(1.5))
    n = int(0.4 * sr)
    add(st, np.sin(2 * np.pi * 55 * np.arange(n) / sr) * env(n, 8))
    add(plan["ch5"]["slam"]["t"], clunk(1.3))
    add(plan["stopT"], clunk(0.8))
    add(plan["endcard"]["start"], clunk(0.7))

    pk = float(np.max(np.abs(buf))) or 1.0
    buf = buf / pk * 10 ** (-12 / 20)
    pcm = (np.stack([buf, buf], 1) * 32767).astype("<i2")
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
    for token in ("__P_DURATION__", "__P_VO_DURATION__", "/*__P_DATA__*/null", "__P_TITLE__"):
        if token not in html:
            raise BuildError(f"template is missing placeholder {token}")
    inject = {k: v for k, v in plan.items() if k not in ("audio", "voice_source")}
    vo_dur = f"{min(probe_duration(Path(bus['voice'])), plan['duration']):.6f}"
    html = (html.replace("__P_VO_DURATION__", vo_dur)
                .replace("__P_DURATION__", f"{plan['duration']:.6f}")
                .replace("/*__P_DATA__*/null", json.dumps(inject, ensure_ascii=False).replace("</", "<\\/"))
                .replace("__P_TITLE__", (slug or wav.stem).replace("<", "")))
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
    ap = argparse.ArgumentParser(description="Instantiate the style P (VHS camcorder tape) template.")
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
        print(f"built {plan['project']} duration={plan['duration']:.3f}s cuts={plan['cuts']}")
    return 0 if plan["audio"]["bus_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
