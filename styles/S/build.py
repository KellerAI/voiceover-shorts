#!/usr/bin/env python3
"""Instantiate the style S (Teletext pages) template for any narration.

Canonical look: the reference lookdev (read only). The template keeps its system: a 1980s text
service (KTEXT) on a 40x24 grid, one page per narration chapter, painted on a canvas from a paused
GSAP clock, with a fastext key press, a rolling page counter, a top-down repaint and a CRT jolt on
every page turn. Five pages: an index page (cover card, then the title types in in the 5x7 block
mosaic face), a chart page (headline on its words, bars rise), a "model checks model" page (sloppy
note, LGTM stamp), a stack page (one word per line, the negation flashes) and a final page (two
mosaic lines and a column-by-column icon). The hard-coded S49 flips and copy are replaced by a
plan derived from the words file.

Usage:
  python3 styles/S/build.py --wav VOICE.wav --words WORDS.json --out DIR
                            [--duration S] [--brand BRAND.json] [--content CONTENT.json]
                            [--slug S49] [--json]

Inputs (script-agnostic):
  --wav       narration WAV (any rate/channels; ffmpeg reads it).
  --words     word timings: a list of {text, start, end} (lookdev format) or {"words": [...]}.
  --duration  narration length in seconds; default: the WAV duration (ffprobe). Every word must
              end inside it. The composition runs past it to hold the end card.
  --brand     optional JSON {logo, name, line, bg, fg, accent} for KAEndCard.mount. S's defaults:
              name "KELLER AI", line = the title, bg cream #FBF7F0, fg navy #10243E, accent
              #8B9090 (the canonical revision's full-contrast cream end page). "name" also feeds
              the cover card, a caption and the last fastext key.
  --content   optional JSON overriding default copy, keys of DEFAULT_CONTENT.
  --slug      optional short id (default: the WAV stem); used in the title and a caption.

How the words drive the pages:
  * Chapters: the words split into five groups at clause ends ([.,;:?!]); when there are more,
    the split with the most even chapter lengths wins; when there are fewer, the largest pauses
    are used. Every chapter is at least MIN_CHAPTER seconds. Pages 2..5 flip CUT_LEAD s before
    their first word.
  * Page 1: the last one or two adjacent content words are the title, typed in mosaic on their
    spoken words; the words before it are the body lines; until the title the cover card (mosaic
    logo + brand name) holds. The index lists the four later pages.
  * Page 2: headline = from its first content word through the second, each word on its start;
    header = the content words after it; the bars rise after the headline.
  * Page 3: header = its content words; model B, the arrow, the note and the stamp land on the
    words nearest 46/39/61/77 % of the page.
  * Page 4: the stack is its last (up to) four words, each on its word; the first negation (else
    the longest word) is the flashing one; header = the last content word + "?".
  * Page 5: its last (up to) four words in two mosaic lines; header = the last content word;
    the icon is a bank temple when a bank word is spoken, else the mosaic logo.
  * The shared Keller AI end card (kit/endcard.js) cuts in 0.1 s after the last word and holds
    2 s with a slow push (no frozen frames).

Output (DIR):
  DIR/project/   a HyperFrames project: index.html, kit/{gsap.min.js,endcard.js},
                 assets/{voice_norm.wav,sfx_bus.wav,mix.wav,fonts/,brand/}
  DIR/plan.json  the plan injected into index.html (also printed with --json)
  DIR/sfx_raw.wav the synthesised teletext SFX before the bus
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
CUT_LEAD = 0.04     # a page flips this long before its first word
MIN_CHAPTER = 1.6   # seconds on screen per page
N_CHAPTERS = 5
FONTS = ["LilitaOne.woff2", "ComicShanns.woff2", "Virgil.woff2"]
ROW = 38            # body text chars per teletext row (cols 2..39, 25 px pitch)
MOSAIC = 13         # mosaic chars per line at full size (6 * 13 * 12.5 - 12.5 <= 1000 px)
HEAD = 18           # header mosaic chars at full size (pw 9)
INDEX = 24          # index entry chars (cols 3..27, page number at col 31)
KEY = 9             # fastext key label chars
CAPS = 34           # combined chars of one caption row (46 px Comic Shanns: 0.55 em + 0.04 em = 27.1 px/char, 1000 px)
MOSAIC_OK = re.compile(r"[^A-Z0-9?&!.\-' ]")

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
BANK_WORDS = {"bank", "banks", "banking", "banker", "bankers", "finance", "financial", "money", "loan", "loans"}

DEFAULT_CONTENT = {
    "service": "KTEXT",
    "date": "Thu 22 Oct",
    "clock": [12, 4, 7],
    "pages": ["100", "217", "218", "219", "300"],
    "index": [],                                   # default: one entry per later page
    "fastext": [],                                 # default: Index, page 2 word, page 5 word, brand
    "cap_top": ["CH 4 . TEXT", ""],                # "" = "BRIEF <slug>"
    "chart": ["WHERE IT SHOWS UP", "(not to scale. obviously.)"],
    "bars": ["GRADE", "REVIEW", "EVALS", "SAFETY", "JUDGE"],
    "chart_foot": "Seen everywhere. Rarely questioned.",
    "labels": ["MODEL A", "MODEL B"],
    "verb": "CHECKS",
    "work_label": "Model B's work:",
    "note": ["2 + 2 = 5", "(probly)", "source: trust me bro"],
    "stamp": "LGTM",
    "stamp_foot": "Model A: looks fine to me!",
    "stack_foot": "Same training. Same blind spots.",
    "final_foot": "Independent. Informed. Able to say no.",
    "icon": "",                                    # "bank" | "logo"; default by the spoken words
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


def words_text(ws: list[dict], upper: bool = False) -> str:
    s = " ".join(strip_punct(w["text"]) for w in ws).strip()
    s = re.sub(r"[\"“”]", "", s).replace("’", "'")
    return s.upper() if upper else s


def mosaic(s: str) -> str:
    """Upper-case, drop trailing punctuation per word, keep only glyphs the 5x7 mosaic face has."""
    s = " ".join(strip_punct(tok) for tok in s.split())
    return re.sub(r"\s+", " ", MOSAIC_OK.sub("", s.upper().replace("’", "'"))).strip()


def cap_words(ws: list[dict], n: int, *, keep: str = "head") -> list[dict]:
    """Longest run of ws (from the head, or the tail) whose text fits n chars."""
    ws = list(ws)
    while len(ws) > 1 and len(words_text(ws)) > n:
        ws = ws[:-1] if keep == "head" else ws[1:]
    return ws


def trunc(s: str, n: int) -> str:
    s = s.strip()
    return s if len(s) <= n else s[: max(1, n - 1)].rstrip() + "~"


def balanced_lines(ws: list[dict], n_lines: int, width: int) -> list[list[dict]]:
    """Split ws into at most n_lines consecutive groups, each <= width chars, most even first."""
    if len(words_text(ws)) <= width or len(ws) == 1 or n_lines == 1:
        return [ws]
    best = None
    for k in range(1, len(ws)):
        a, b = ws[:k], ws[k:]
        la, lb = len(words_text(a)), len(words_text(b))
        if la <= width and lb <= width and (best is None or max(la, lb) < best[0]):
            best = (max(la, lb), a, b)
    if best is None:
        return [cap_words(ws, width)]
    return [best[1], best[2]]


def from_first_content(ws: list[dict]) -> list[dict]:
    """Drop leading function words, keeping one article/numeral/negation right before the first content word."""
    idx = next((i for i, w in enumerate(ws) if is_content(w["text"])), None)
    if idx is None:
        return ws
    if idx > 0 and low(ws[idx - 1]["text"]) in LEADING_BACKUP:
        idx -= 1
    return ws[idx:]


def sentence(s: str) -> str:
    return s[:1].upper() + s[1:] if s else s


def endcard_plan(last_word_end: float) -> dict:
    """Mirror of KAEndCard.plan() with default gap/hold (the template re-checks it in JS)."""
    start = round(last_word_end + END_GAP, 6)
    end = math.ceil((max(start, last_word_end) + END_HOLD) * FPS - 1e-6) / FPS
    return {"start": start, "end": round(end, 6), "hold": END_HOLD}


def near(ws: list[dict], target: float) -> float:
    return min((w["start"] for w in ws), key=lambda s: abs(s - target))


# ----------------------------------------------------------------------------- chapters

def split_chapters(words: list[dict], ec_start: float) -> list[int]:
    """Return the 4 word indices where pages 2..5 begin."""
    n = len(words)

    def cut_at(i: int) -> float:
        return words[i]["start"] - CUT_LEAD

    def ok(bounds: tuple[int, ...]) -> bool:
        cuts = [0.0] + [cut_at(i) for i in bounds] + [ec_start]
        return all(b - a >= MIN_CHAPTER - 1e-9 for a, b in zip(cuts, cuts[1:]))

    def cost(bounds: tuple[int, ...], punct: set[int]) -> float:
        cuts = [0.0] + [cut_at(i) for i in bounds] + [ec_start]
        durs = [b - a for a, b in zip(cuts, cuts[1:])]
        mean = sum(durs) / len(durs)
        return sum((d - mean) ** 2 for d in durs) + 4.0 * sum(1 for i in bounds if i not in punct)

    punct = {i + 1 for i in range(n - 1) if ends_clause(words[i]["text"])}
    gaps = sorted((i + 1 for i in range(n - 1)), key=lambda i: words[i]["start"] - words[i - 1]["end"], reverse=True)
    for pool in (sorted(punct), sorted(punct | set(gaps[:12])), list(range(1, n))):
        pool = [i for i in pool if cut_at(i) >= MIN_CHAPTER - 1e-9]
        if len(pool) > 28:  # keep the search small: clause ends first, then the largest pauses
            ranked = sorted(pool, key=lambda i: (i not in punct, -(words[i]["start"] - words[i - 1]["end"])))
            pool = sorted(ranked[:28])
        best = None
        for combo in itertools.combinations(pool, N_CHAPTERS - 1):
            if ok(combo):
                c = cost(combo, punct)
                if best is None or c < best[0]:
                    best = (c, combo)
        if best:
            return list(best[1])
    raise BuildError(f"narration too short for five {MIN_CHAPTER} s pages "
                     f"(end card at {ec_start:.2f} s, {len(words)} words)")


# ----------------------------------------------------------------------------- plan

def word_items(ws: list[dict], lines: list[list[dict]]) -> list[dict]:
    out = []
    for li, line in enumerate(lines):
        for w in line:
            m = mosaic(w["text"])
            if m:
                out.append({"text": m, "t": round(w["start"], 4), "line": li})
    return out


def build_plan(words: list[dict], duration: float, slug: str, brand: dict, content: dict) -> dict:
    lwe = max(w["end"] for w in words)
    if lwe > duration + 1e-3:
        raise BuildError(f"last word ends at {lwe:.3f} s, after the narration ({duration:.3f} s)")
    unknown = set(content) - set(DEFAULT_CONTENT)
    if unknown:
        raise BuildError(f"--content has unknown keys: {sorted(unknown)}")
    co = dict(DEFAULT_CONTENT)
    co.update(content)
    for key, n in (("pages", 5), ("bars", 5), ("labels", 2), ("note", 3), ("chart", 2), ("cap_top", 2), ("clock", 3)):
        if not isinstance(co[key], list) or len(co[key]) != n:
            raise BuildError(f"--content {key} must be a list of {n}")
    for key, n in (("index", 4), ("fastext", 4)):
        if co[key] and (not isinstance(co[key], list) or len(co[key]) != n):
            raise BuildError(f"--content {key} must be empty or a list of {n}")
    if not all(str(p).isdigit() and len(str(p)) == 3 for p in co["pages"]):
        raise BuildError("--content pages must be five 3-digit page numbers")
    if co["icon"] not in ("", "bank", "logo"):
        raise BuildError("--content icon must be 'bank' or 'logo'")
    br = dict(DEFAULT_BRAND)
    br.update({k: v for k, v in brand.items() if v is not None})
    ec = endcard_plan(lwe)

    bounds = split_chapters(words, ec["start"])
    edges = [0] + bounds + [len(words)]
    G = [words[a:b] for a, b in zip(edges, edges[1:])]
    flips = [round(words[i]["start"] - CUT_LEAD, 4) for i in bounds]
    spans = list(zip([0.0] + flips, flips + [ec["start"]]))

    # page 1: body lines, then the title typed in mosaic on its words
    g1 = G[0]
    cont1 = [i for i, w in enumerate(g1) if is_content(w["text"]) and mosaic(w["text"])]
    if cont1:
        ti = cont1[-2:] if len(cont1) >= 2 and cont1[-1] - cont1[-2] <= 2 else cont1[-1:]
        title_ws = [w for w in g1[ti[0]:ti[-1] + 1] if mosaic(w["text"])]
    else:
        title_ws = [w for w in g1 if mosaic(w["text"])][-1:] or g1[-1:]
    t0 = g1.index(title_ws[0])
    if len(title_ws) <= 2:
        tlines = [[w] for w in title_ws]
    else:
        tlines = balanced_lines(title_ws, 2, MOSAIC)
    title = []
    for ln in tlines:
        text = mosaic(words_text(ln)) or "?"
        title.append({"text": text, "t": round(ln[0]["start"], 4),
                      "dur": round(min(0.9, max(0.4, ln[-1]["end"] - ln[0]["start"])), 3)})
    body_ws = cap_words(g1[:t0], 2 * ROW, keep="tail") if t0 > 0 else []
    body = [{"text": words_text(ln), "t": round(0.0 if i == 0 else ln[0]["start"], 4)}
            for i, ln in enumerate(balanced_lines(body_ws, 2, ROW))] if body_ws else []
    title_text = " ".join(x["text"] for x in title)
    name_words = mosaic(str(br.get("name") or "KELLER AI")).split(" ") or ["KELLER", "AI"]
    cover = [name_words[0], " ".join(name_words[1:])] if len(name_words) > 1 else [name_words[0], ""]

    # page 2: headline through the second content word; header from the rest
    g2 = from_first_content(G[1])
    lead2 = G[1][:len(G[1]) - len(g2)]
    ci = [i for i, w in enumerate(g2) if is_content(w["text"])]
    cut2 = ci[1] + 1 if len(ci) >= 2 else len(g2)
    head2_ws, rest2 = g2[:cut2], g2[cut2:]
    head2_ws = cap_words(head2_ws, 2 * MOSAIC)
    hl2 = word_items(head2_ws, balanced_lines(head2_ws, 2, 14))
    rest2_c = [w for w in rest2 if is_content(w["text"])]
    hdr2 = mosaic(words_text(cap_words(rest2_c, HEAD))) if rest2_c else ""
    hdr2 = hdr2 or (hl2[-1]["text"] if hl2 else "PAGE 2")
    k2, e2 = spans[1]
    bar_t = min(max((head2_ws[-1]["end"] if head2_ws else k2) + 0.2, k2 + 0.3), e2 - 1.0)
    p2 = {
        "head": trunc(hdr2, HEAD), "lead": (trunc(words_text(lead2), ROW - 3) + "...") if lead2 else "",
        "headline": hl2, "chart": [trunc(str(c), ROW) for c in co["chart"]],
        "bars": [trunc(str(b), 6) for b in co["bars"]], "barT": round(bar_t, 4),
        "foot": trunc(str(co["chart_foot"]), ROW), "footT": round(min(bar_t + 0.35, e2 - 0.4), 4),
    }

    # page 3: model A checks model B; cues on the words nearest fixed fractions of the page
    g3 = G[2]
    k3, e3 = spans[2]
    d3 = e3 - k3
    arrow_t = max(near(g3, k3 + 0.39 * d3), k3 + 0.3)
    b_t = max(near(g3, k3 + 0.46 * d3), arrow_t + 0.15)
    note_t = max(near(g3, k3 + 0.61 * d3), b_t + 0.3)
    stamp_t = max(near(g3, k3 + 0.77 * d3), note_t + 0.35)
    stamp_t = min(stamp_t, e3 - 0.55)
    note_t = min(note_t, stamp_t - 0.35)
    b_t = min(b_t, note_t - 0.3)
    arrow_t = min(arrow_t, b_t - 0.15)
    head3 = mosaic(words_text(cap_words([w for w in g3 if is_content(w["text"])] or g3, HEAD)))
    p3 = {
        "head": trunc(head3 or "PAGE 3", HEAD), "labelA": trunc(str(co["labels"][0]), 12), "labelB": trunc(str(co["labels"][1]), 12),
        "verb": trunc(str(co["verb"]), 12), "workLabel": trunc(str(co["work_label"]), ROW),
        "note": [trunc(str(n), 28) for n in co["note"]], "stamp": mosaic(str(co["stamp"]))[:8] or "OK",
        "stampFoot": trunc(str(co["stamp_foot"]), ROW),
        "arrowT": round(arrow_t, 4), "bT": round(b_t, 4), "noteT": round(note_t, 4), "stampT": round(stamp_t, 4),
    }

    # page 4: the stack, one word per line on its word
    g4 = G[3]
    k4, e4 = spans[3]
    stack_ws = [w for w in g4 if mosaic(w["text"])][-4:]
    lead4 = g4[:g4.index(stack_ws[0])] if stack_ws else g4
    neg = [i for i, w in enumerate(stack_ws) if low(w["text"]) in NEGATIONS]
    hot = neg[0] if neg else max(range(len(stack_ws)), key=lambda i: len(clean(stack_ws[i]["text"])))
    stack = [{"text": trunc(mosaic(w["text"]), MOSAIC), "t": round(max(w["start"], k4 + 0.2), 4), "hot": i == hot}
             for i, w in enumerate(stack_ws)]
    last4 = [w for w in g4 if is_content(w["text"]) and mosaic(w["text"])]
    head4 = trunc((mosaic(last4[-1]["text"]) if last4 else stack[-1]["text"]), HEAD - 1) + "?"
    p4 = {
        "head": head4, "lead": (trunc(words_text(lead4), ROW - 3) + "...") if lead4 else "",
        "stack": stack, "eqT": round(min(stack[-1]["t"] + 0.02, e4 - 0.3), 4),
        "foot": trunc(str(co["stack_foot"]), ROW), "footT": round(k4 + 0.43, 4),
    }

    # page 5: last words in two mosaic lines, then the icon builds
    g5 = G[4]
    k5, e5 = spans[4]
    tail = [w for w in g5 if mosaic(w["text"])][-4:]
    lead5 = g5[:g5.index(tail[0])] if tail else g5
    l5 = balanced_lines(tail, 2, MOSAIC) if len(tail) > 1 else [tail]
    if len(l5) == 1 and len(tail) > 1:
        l5 = [tail[:len(tail) // 2], tail[len(tail) // 2:]]
    l1 = {"text": mosaic(words_text(l5[0])), "t": round(max(l5[0][0]["start"], k5 + 0.2), 4)}
    if len(l5) > 1:
        l2 = {"text": mosaic(words_text(l5[1])), "t": round(max(l5[1][0]["start"], l1["t"] + 0.05), 4),
              "dur": round(min(0.6, max(0.2, l5[1][-1]["end"] - l5[1][0]["start"])), 3)}
    else:
        l2 = {"text": "", "t": 0, "dur": 0.2}
    last5 = [w for w in g5 if is_content(w["text"]) and mosaic(w["text"])]
    icon = co["icon"] or ("bank" if any(low(w["text"]) in BANK_WORDS for w in g5) else "logo")
    build_t = k5 + 0.3
    step = max(0.075, min(0.25, (lwe - 0.6 - build_t) / 5))
    p5 = {
        "head": trunc(mosaic(last5[-1]["text"]) if last5 else "PAGE 5", HEAD),
        "lead": ("..." + trunc(words_text(lead5), ROW - 3)) if lead5 else "",
        "l1": l1, "l2": l2, "icon": icon, "buildT": round(build_t, 4), "buildStep": round(step, 4),
        "foot": trunc(str(co["final_foot"]), ROW), "footT": round(l2["t"] if l2["text"] else l1["t"], 4),
    }

    # index page entries, fastext keys, captions
    index = co["index"] or [sentence(words_text(cap_words(from_first_content(g), INDEX))) for g in G[1:]]
    pages = [str(p) for p in co["pages"]]
    p1 = {
        "head": "BRIEFS & ANALYSIS", "body": body, "title": title, "cover": [c[:8] for c in cover],
        "index": [[trunc(str(e), INDEX).ljust(INDEX + 2, "."), pages[i + 1]] for i, e in enumerate(index)],
        "hiT": round(max(0.2, flips[0] - 0.41), 4),
    }
    brand_disp = sentence(str(br.get("name") or "KELLER AI").title().replace("Ai", "AI"))
    fast = co["fastext"] or ["Index", sentence(p2["head"].split(" ")[0].lower()).replace("Ai", "AI"),
                             sentence(p5["head"].lower()), brand_disp]
    cap_tr = co["cap_top"][1] or f"BRIEF {slug}"
    cap_bl, cap_br = title_text, brand_disp
    if len(cap_bl) + len(cap_br) + 2 > CAPS:
        cap_bl = trunc(cap_bl, max(4, CAPS - len(cap_br) - 2))
    caps = [trunc(str(co["cap_top"][0]), 14), trunc(cap_tr, CAPS - 14), cap_bl, cap_br]
    if not br.get("line"):
        br["line"] = sentence(title_text.title())

    return {
        "flips": flips,
        "pages": pages,
        "chapters": [{"first_word": words[a]["text"], "start": round(words[a]["start"], 4)} for a in edges[:-1]],
        "p1": p1, "p2": p2, "p3": p3, "p4": p4, "p5": p5,
        "cam": {"titleT": title[0]["t"], "punchT": round(stack[0]["t"] - 0.14, 4), "stampT": p3["stampT"]},
        "service": trunc(str(co["service"]), 5), "date": trunc(str(co["date"]), 10),
        "clock": [int(co["clock"][0]) % 24, int(co["clock"][1]) % 60, int(co["clock"][2]) % 60],
        "fastext": [trunc(str(f), KEY) for f in fast],
        "caps": caps,
        "firstWordStart": round(words[0]["start"], 4),
        "lastWordEnd": round(lwe, 4),
        "narration": round(duration, 6),
        "endcard": ec,
        "duration": ec["end"],
        "brand": br,
    }


# ----------------------------------------------------------------------------- sfx (port of r7-S49-S sfx_gen.py)

def synth_sfx(plan: dict, out: Path, sr: int = 48000) -> Path:
    """Remote beep, relay click, static, counter ticks and a row-paint rasp on every page turn, a
    stamp thunk, two push whooshes, flash blips on the hot stack word, an end chime on the card
    cut and a low mains hum. Seeded numpy, no samples."""
    import numpy as np

    n = int(round(plan["duration"] * sr))
    buf = np.zeros(n)
    rng = np.random.default_rng(49)

    def add(sig, t):
        i = int(t * sr)
        if i < 0 or i >= n:
            return
        j = min(n, i + len(sig))
        buf[i:j] += sig[: j - i]

    def env(length, a=0.002, d=0.05):
        tt = np.arange(int(length * sr)) / sr
        return np.minimum(1, tt / a) * np.exp(-tt / d)

    def tone(f, length, a=0.002, d=0.05, amp=1.0):
        tt = np.arange(int(length * sr)) / sr
        return amp * np.sign(np.sin(2 * np.pi * f * tt)) * 0.5 * env(length, a, d)

    tt = np.arange(n) / sr
    buf += 0.010 * np.sin(2 * np.pi * 50 * tt) + 0.006 * np.sin(2 * np.pi * 100 * tt)
    for f in plan["flips"]:
        add(tone(1320, 0.07, d=0.03, amp=0.35), f - 0.26)
        add(rng.standard_normal(int(0.012 * sr)) * env(0.012, 0.0005, 0.003) * 0.9, f)
        add(rng.standard_normal(int(0.10 * sr)) * env(0.10, 0.003, 0.03) * 0.25, f + 0.01)
        for r in range(3):
            add(tone(700 + 90 * r, 0.025, d=0.008, amp=0.25), f + 0.01 + r * 0.025)
        sl = 0.10
        st = np.arange(int(sl * sr)) / sr
        add(np.sign(np.sin(2 * np.pi * (300 + 900 * st / sl) * st)) * 0.06 * (1 - st / sl), f + 0.07)
    add(np.sin(2 * np.pi * 90 * np.arange(int(0.18 * sr)) / sr) * env(0.18, 0.001, 0.06) * 0.8, plan["p3"]["stampT"])
    for w0 in (plan["cam"]["titleT"] - 0.2, plan["cam"]["punchT"] - 0.03):
        L = int(0.30 * sr)
        x = np.arange(L) / sr
        add(rng.standard_normal(L) * np.sin(np.pi * x / 0.30) ** 2 * 0.18, w0)
    hot = next((s for s in plan["p4"]["stack"] if s["hot"]), plan["p4"]["stack"][-1])
    for b in (hot["t"] + 0.02, hot["t"] + 0.32):
        add(tone(440, 0.09, d=0.04, amp=0.3), b)
    for f, a in ((880, 0.35), (1320, 0.2)):
        add(np.sin(2 * np.pi * f * np.arange(int(0.8 * sr)) / sr) * env(0.8, 0.004, 0.3) * a, plan["endcard"]["start"] + 0.02)

    pk = float(np.max(np.abs(buf))) or 1.0
    buf = buf / pk * 10 ** (-18 / 20)
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
    slug = slug or wav.stem
    plan = build_plan(words, narr, slug, dict(brand or {}), dict(content or {}))

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
    tokens = ("__S_DURATION__", "__S_VO_DURATION__", "__S_SFX_DURATION__", "/*__S_DATA__*/null", "__S_TITLE__")
    for token in tokens:
        if token not in html:
            raise BuildError(f"template is missing placeholder {token}")
    inject = {k: v for k, v in plan.items() if k not in ("audio", "voice_source")}
    vo_dur = f"{min(probe_duration(Path(bus['voice'])), plan['duration']):.6f}"
    sfx_dur = f"{min(probe_duration(Path(bus['sfx'])), plan['duration']):.6f}"
    html = (html.replace("__S_VO_DURATION__", vo_dur)
                .replace("__S_SFX_DURATION__", sfx_dur)
                .replace("__S_DURATION__", f"{plan['duration']:.6f}")
                .replace("/*__S_DATA__*/null", json.dumps(inject, ensure_ascii=False).replace("</", "<\\/"))
                .replace("__S_TITLE__", slug.replace("<", "").replace("&", "")))
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
    ap = argparse.ArgumentParser(description="Instantiate the style S (Teletext pages) template.")
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
        print(f"built {plan['project']} duration={plan['duration']:.3f}s flips={plan['flips']}")
    return 0 if plan["audio"]["bus_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
