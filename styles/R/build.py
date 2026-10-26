#!/usr/bin/env python3
"""Instantiate the style R (90s desktop OS) template for any narration.

Canonical look: the reference lookdev (read only). The template keeps its system: a navy dithered
desktop with grey bevelled windows, navy title bars, a taskbar with a Keller "Menu" button and a
clock, an outline zoom when a window opens, stepped (steps()) eases, a mouse cursor that presses
buttons and turns into an hourglass while things load. The story is told through window contents
in five scenes: a BRIEF.TXT reader that types the title on the spoken words, a scanner whose
result names the habit, MODEL_A's sloppy answers reviewed by MODEL_B (sloppy "looks good" stamp),
an error dialog whose OK button spawns a cascade of copies and then a "Required file not found:
<WORD>.DLL" system error, and a dial-up "Connect To" dialog whose CONNECTED hero repeats the title.
The hard-coded S49 cue times and copy are replaced by a plan derived from the words file.

Usage:
  python3 styles/R/build.py --wav VOICE.wav --words WORDS.json --out DIR
                            [--duration S] [--brand BRAND.json] [--content CONTENT.json]
                            [--slug S49] [--json]

Inputs (script-agnostic):
  --wav       narration WAV (any rate/channels; ffmpeg reads it).
  --words     word timings: a list of {text, start, end} (lookdev format) or {"words": [...]}.
  --duration  narration length in seconds; default: the WAV duration (ffprobe). Every word must
              end inside it. The composition runs past it to hold the end card.
  --brand     optional JSON {logo, name, line, bg, fg, accent} for KAEndCard.mount. R's defaults:
              the kit navy card (bg #10243E, fg cream) with line "Brief: <title>".
  --content   optional JSON overriding default copy, keys of DEFAULT_CONTENT.
  --slug      optional short id (default: the WAV stem); used in the page title.

How the words drive the desktop:
  * Scenes: the words split into five groups at clause ends ([.,;:?!]); when there are more,
    the split with the most even scene lengths wins; when there are fewer, the largest pauses
    are used. Every scene is at least MIN_SCENE seconds. Scenes 2..5 cut in 0.05 s before their
    first word; scene 1 (the brief) is open at frame 0 and starts typing at 0.1 s.
  * Scene 1 (BRIEF.TXT): the last one or two content words are the title, typed one line per
    word over each spoken word; the memo lines type before it.
  * Scene 2 (scanner): result = the clause from its first content word through the second
    ("a common habit" -> "1 common habit found"), shown on the last of those words; the rest of the
    clause is the scan label and the C:\\ path (shown on its first word).
  * Scene 3 (models): MODEL_B's header is the clause from its first content word; Check is pressed
    on the word nearest 40 %, the stamp lands on the last content word.
  * Scene 4 (error): OK is pressed, copies cascade, and the system error names <SLAM>.DLL on the
    slam word's start (the first content word after a negation, else the longest content word).
  * Scene 5 (dial-up): "Connect To - <last content word>"; the CONNECTED hero (the title) lands on
    that word.
  * The shared Keller AI end card (kit/endcard.js) cuts in 0.1 s after the last word and holds
    2 s with a slow push under the grain, with the taskbar still on top.

Output (DIR):
  DIR/project/   a HyperFrames project: index.html, kit/{gsap.min.js,endcard.js},
                 assets/{voice_norm.wav,sfx_bus.wav,mix.wav,fonts/,brand/}
  DIR/plan.json  the plan injected into index.html (also printed with --json)
  DIR/sfx_raw.wav the synthesised UI SFX before the bus
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
CUT_LEAD = 0.05     # a scene cuts in this long before its first word
MIN_SCENE = 1.8     # seconds on screen per scene
N_SCENES = 5
FONTS = ["LilitaOne.woff2", "ComicShanns.woff2", "Virgil.woff2"]
# char caps so every nowrap line fits its box with the bundled fonts (the page also shrinks to fit
# with a floor; these caps keep the floor from being reached)
TITLE_WORD = 12     # Lilita title line, 150 px, page ~860 px
META_CHARS = 26     # Comic Shanns 52 px memo line
RESULT_CHARS = 30   # Lilita 66 px result, wraps to two lines
SCAN_CHARS = 28     # Comic Shanns 46 px
LOC_CHARS = 24      # Comic Shanns 48 px path
WIN_CHARS = 24      # Lilita 46 px title bar
HEAD_CHARS = 40     # Lilita 50 px MODEL_B header, wraps to two lines
DLL_WORD = 12       # Comic Shanns 78 px <WORD>.DLL
MSG_CHARS = 64      # Comic Shanns 46 px, wraps
DIAL_CHARS = 22     # Comic Shanns 48 px dial line
D2_CHARS = 40       # Comic Shanns 48 px, wraps

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

DEFAULT_CONTENT = {
    "meta": ["TO: you", "FROM: KellerAI Research", "TYPE: Brief No. 07"],
    "severity": "Severity: everywhere",
    "slop": ["2 + 2 = 5", "capital of France: Pairs", "risk level: lol none", "source: trust me bro"],
    "checking": "Checking MODEL_A's work...",
    "done": "Done. Same training, same opinion.",
    "stamp": "\u2713 looks good 2 me!!",
    "error": "Same blind spot.",
    "error_msg": "MODEL_B made the same mistakes as MODEL_A.",
    "why": "Same kind of training. Same kind of miss.",
    "hero_head": "CONNECTED:",
    "menu": "Menu",
}

DEFAULT_BRAND = {"name": "KELLER AI", "bg": "#10243E", "fg": "#FBF7F0", "accent": "#8B9090"}


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


def words_text(ws: list[dict]) -> str:
    s = " ".join(strip_punct(w["text"]) for w in ws).strip()
    return re.sub(r"[\"“”]", "", s)


def sentence(ws: list[dict]) -> str:
    s = words_text(ws)
    return s[:1].upper() + s[1:] if s else s


def trunc(s: str, n: int) -> str:
    s = s.strip()
    return s if len(s) <= n else s[: max(1, n - 1)].rstrip() + "~"


def cap_words(ws: list[dict], n: int, *, keep: str = "head") -> list[dict]:
    """Longest run of ws (from the head, or the tail) whose text fits n chars."""
    ws = list(ws)
    while len(ws) > 1 and len(words_text(ws)) > n:
        ws = ws[:-1] if keep == "head" else ws[1:]
    return ws


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


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x)) if lo <= hi else lo


def r4(x: float) -> float:
    return round(float(x), 4)


def char_times(t0: float, t1: float, n: int) -> list[float]:
    """When each of n typed chars appears (the template spreads them the same way)."""
    step = (t1 - t0) / max(1, n)
    return [t0 + i * step for i in range(n)]


# ----------------------------------------------------------------------------- scenes

def split_scenes(words: list[dict], ec_start: float) -> list[int]:
    """Return the 4 word indices where scenes 2..5 begin."""
    n = len(words)

    def cut_at(i: int) -> float:
        return words[i]["start"] - CUT_LEAD

    def ok(bounds: tuple[int, ...]) -> bool:
        cuts = [0.0] + [cut_at(i) for i in bounds] + [ec_start]
        return all(b - a >= MIN_SCENE - 1e-9 for a, b in zip(cuts, cuts[1:]))

    def cost(bounds: tuple[int, ...], punct: set[int]) -> float:
        cuts = [0.0] + [cut_at(i) for i in bounds] + [ec_start]
        durs = [b - a for a, b in zip(cuts, cuts[1:])]
        mean = sum(durs) / len(durs)
        return sum((d - mean) ** 2 for d in durs) + 4.0 * sum(1 for i in bounds if i not in punct)

    punct = {i + 1 for i in range(n - 1) if ends_clause(words[i]["text"])}
    gaps = sorted((i + 1 for i in range(n - 1)), key=lambda i: words[i]["start"] - words[i - 1]["end"], reverse=True)
    for pool in (sorted(punct), sorted(punct | set(gaps[:12])), list(range(1, n))):
        pool = [i for i in pool if cut_at(i) >= MIN_SCENE - 1e-9]
        if len(pool) > 28:  # keep the search small: clause ends first, then the largest pauses
            keep = sorted(pool, key=lambda i: (i not in punct, -(words[i]["start"] - words[i - 1]["end"])))[:28]
            pool = sorted(keep)
        best = None
        for combo in itertools.combinations(pool, N_SCENES - 1):
            if ok(combo):
                c = cost(combo, punct)
                if best is None or c < best[0]:
                    best = (c, combo)
        if best:
            return list(best[1])
    raise BuildError(f"narration too short for five {MIN_SCENE} s scenes "
                     f"(end card at {ec_start:.2f} s, {n} words)")


def nearest_start(ws: list[dict], target: float) -> float:
    return min((w["start"] for w in ws), key=lambda s: abs(s - target))


def scene1(g: list[dict], nxt: float, co: dict) -> dict:
    cont = [i for i, w in enumerate(g) if is_content(w["text"])]
    if cont:
        ti = cont[-2:] if len(cont) >= 2 and cont[-1] - cont[-2] <= 2 else cont[-1:]
        title_ws = [g[i] for i in ti]
    else:
        title_ws = g[-1:]
    lines = []
    for w in title_ws:
        t0 = w["start"]
        t1 = max(t0 + 0.2, min(w["end"], t0 + 0.6))
        lines.append({"text": trunc(strip_punct(w["text"]), TITLE_WORD), "t0": r4(t0), "t1": r4(t1)})
    meta_txt = [trunc(str(m), META_CHARS) for m in co["meta"]] + ["TITLE:"]
    w0, w1 = 0.1, lines[0]["t0"] - 0.08
    meta = []
    if w1 - w0 < 0.5:  # no room to type: the memo is already there at frame 0
        meta = [{"text": m, "t0": 0.0, "t1": 0.0} for m in meta_txt]
    else:
        total = sum(len(m) for m in meta_txt)
        gap = min(0.2, 0.3 * (w1 - w0) / len(meta_txt))
        span = (w1 - w0) - gap * (len(meta_txt) - 1)
        t = w0
        for m in meta_txt:
            d = span * len(m) / total
            meta.append({"text": m, "t0": r4(t), "t1": r4(t + d)})
            t += d + gap
    hl = min(lines[-1]["t1"] + 0.02, nxt - 0.25)
    return {"meta": meta, "title": lines, "hlT": r4(hl), "titleText": " ".join(ln["text"] for ln in lines)}


def scene2(g: list[dict], cut: float, nxt: float, co: dict) -> dict:
    g2 = from_first_content(g)
    ci = [i for i, w in enumerate(g2) if is_content(w["text"])]
    k = ci[1] + 1 if len(ci) >= 2 else len(g2)
    head, rest = g2[:k], g2[k:]
    if head and low(head[0]["text"]) in ("a", "an", "one"):
        result = "1 " + words_text(head[1:]) + " found"
    else:
        result = "Found: " + words_text(head)
    last_head = strip_punct(head[-1]["text"]) if head else "Habit"
    rest_c = [w for w in rest if is_content(w["text"])]
    path_parts = [clean(w["text"]).upper() for w in (rest_c or head[-1:])]
    loc = "C:\\" + "\\".join(path_parts)
    while len(loc) + 4 > LOC_CHARS and "\\" in loc[3:]:
        loc = loc.rsplit("\\", 1)[0]
    loc = trunc(loc, LOC_CHARS - 4) + "\\*.*"
    prog0 = cut + 0.27
    res_t = clamp(head[-1]["start"] if head else cut + 1.0, prog0 + 0.3, nxt - 0.6)
    loc_t = clamp(rest[0]["start"] if rest else res_t + 0.4, res_t + 0.2, nxt - 0.35)
    scan_ws = cap_words(from_first_content(rest), SCAN_CHARS - 12) if rest else []
    return {
        "win": trunc(f"{last_head[:1].upper()}{last_head[1:]} Scanner 1.0", WIN_CHARS),
        "scan": trunc("Scanning " + (words_text(scan_ws) if scan_ws else "systems") + "...", SCAN_CHARS),
        "result": trunc(result, RESULT_CHARS),
        "severity": trunc(str(co["severity"]), RESULT_CHARS),
        "loc": loc,
        "progT": r4(prog0), "resT": r4(res_t), "locT": r4(loc_t),
    }


def scene3(g: list[dict], cut: float, nxt: float, co: dict) -> dict:
    d = nxt - cut
    cont = [w for w in g if is_content(w["text"])] or g
    stamp_t = clamp(cont[-1]["start"], cut + 1.4, nxt - 0.45)
    press_t = clamp(nearest_start(g, cut + 0.4 * d), cut + 0.85, stamp_t - 0.55)
    return {
        "head": trunc(sentence(cap_words(from_first_content(g), HEAD_CHARS)), HEAD_CHARS),
        "slop": [trunc(str(s), 28) for s in co["slop"]],
        "slopT": r4(cut + 0.37),
        "pressT": r4(press_t), "stampT": r4(stamp_t),
        "checking": trunc(str(co["checking"]), 30), "done": trunc(str(co["done"]), 40),
        "stamp": trunc(str(co["stamp"]), 22),
    }


def scene4(g: list[dict], cut: float, nxt: float, co: dict) -> dict:
    neg = [i for i, w in enumerate(g) if low(w["text"]) in NEGATIONS]
    after = [w for w in g[neg[0] + 1:] if is_content(w["text"])] if neg else []
    if after:
        slam = after[0]
    else:
        cand = [w for w in g if is_content(w["text"]) and low(w["text"]) not in NEGATIONS] or g
        slam = max(cand, key=lambda w: len(clean(w["text"])))
    dll_t = clamp(slam["start"], cut + 1.1, nxt - 0.5)
    why_t = cut + 0.4
    press_t = clamp(cut + 0.45 * (dll_t - cut), why_t + 0.2, dll_t - 0.4)
    copies, t, i = [], press_t + 0.07, 0
    while t < dll_t - 0.02 and i < 14:
        copies.append(r4(t))
        t += 0.12 * 0.8 ** i + 0.02
        i += 1
    word = re.sub(r"[^A-Z0-9_\-]", "", clean(slam["text"]).upper()) or "INDEPENDENCE"
    return {
        "error": trunc(str(co["error"]), 22), "errorMsg": trunc(str(co["error_msg"]), 60),
        "why": trunc(str(co["why"]), 48),
        "whyT": r4(why_t), "pressT": r4(press_t), "copies": copies, "dllT": r4(dll_t),
        "dll": trunc(word, DLL_WORD) + ".DLL", "slamWord": strip_punct(slam["text"]),
        "msg": trunc(sentence(from_first_content(g)) + ".", MSG_CHARS),
    }


def scene5(g: list[dict], cut: float, ec_start: float, co: dict, title_lines: list[dict]) -> dict:
    ci = [i for i, w in enumerate(g) if is_content(w["text"])]
    s = ci[-1] if ci else len(g) - 1
    target = clean(g[s]["text"]) or "Network"
    head = g[:s]
    while head and low(head[0]["text"]) in CONJ:
        head = head[1:]
    head = cap_words(head, D2_CHARS - 1, keep="tail") if head else []
    hero_t = clamp(g[s]["start"], cut + 0.7, ec_start - 0.35)
    d2_t = clamp(head[0]["start"] if head else cut + 0.45, cut + 0.3, hero_t - 0.2)
    return {
        "win": trunc(f"Connect To - {target[:1].upper()}{target[1:].lower()}", WIN_CHARS),
        "d1": trunc(f"Dialing {target.upper()}...", DIAL_CHARS), "d1T": r4(cut + 0.05),
        "d2": (sentence(head) + "...") if head else "", "d2T": r4(d2_t),
        "heroT": r4(hero_t), "heroHead": trunc(str(co["hero_head"]), 16),
        "hero": [ln["text"] for ln in title_lines], "target": target,
    }


# ----------------------------------------------------------------------------- plan

def build_plan(words: list[dict], duration: float, brand: dict, content: dict) -> dict:
    lwe = max(w["end"] for w in words)
    if lwe > duration + 1e-3:
        raise BuildError(f"last word ends at {lwe:.3f} s, after the narration ({duration:.3f} s)")
    unknown = set(content) - set(DEFAULT_CONTENT)
    if unknown:
        raise BuildError(f"--content has unknown keys: {sorted(unknown)}")
    co = dict(DEFAULT_CONTENT)
    co.update(content)
    for key, n in (("meta", 3), ("slop", 4)):
        if not isinstance(co[key], list) or len(co[key]) != n:
            raise BuildError(f"--content {key} must be a list of {n}")
    ec = endcard_plan(lwe)
    bounds = split_scenes(words, ec["start"])
    edges = [0] + bounds + [len(words)]
    G = [words[a:b] for a, b in zip(edges, edges[1:])]
    cuts = [0.0] + [r4(words[i]["start"] - CUT_LEAD) for i in bounds]
    nxt = cuts[1:] + [ec["start"]]

    s1 = scene1(G[0], nxt[0], co)
    br = dict(DEFAULT_BRAND)
    br["line"] = trunc(f"Brief: {s1['titleText']}", 30)
    br.update({k: v for k, v in brand.items() if v is not None})
    plan = {
        "cuts": cuts,
        "scenes": [{"first_word": words[a]["text"], "start": r4(words[a]["start"])} for a in edges[:-1]],
        "s1": s1,
        "s2": scene2(G[1], cuts[1], nxt[1], co),
        "s3": scene3(G[2], cuts[2], nxt[2], co),
        "s4": scene4(G[3], cuts[3], nxt[3], co),
        "s5": scene5(G[4], cuts[4], ec["start"], co, s1["title"]),
        "menu": trunc(str(co["menu"]), 6),
        "firstWordStart": r4(words[0]["start"]),
        "lastWordEnd": r4(lwe),
        "narration": round(duration, 6),
        "endcard": ec,
        "duration": ec["end"],
        "brand": br,
    }
    return plan


# ----------------------------------------------------------------------------- sfx (port of r7-S49-R sfx_gen.py)

def synth_sfx(plan: dict, out: Path, sr: int = 48000) -> Path:
    """Key clicks on every typed char, window-open blips, progress ticks, a chord on each result,
    the stamp, the error chord, square blips per cascading copy, the DLL hit, DTMF and a modem hiss
    into the dial-up, and a chime on the end-card cut. Seeded numpy, no samples."""
    import numpy as np

    n_total = int(round(plan["duration"] * sr))
    buf = np.zeros(n_total)
    rng = np.random.default_rng(49)

    def add(t: float, s: "np.ndarray") -> None:
        i = int(t * sr)
        if i < 0 or i >= n_total:
            return
        s = s[: n_total - i]
        buf[i:i + len(s)] += s

    def click(t: float, amp: float = 0.6) -> None:
        n = int(0.012 * sr)
        add(t, amp * rng.uniform(-1, 1, n) * np.exp(-np.arange(n) / (0.0015 * sr)))

    def tone(t: float, freqs: list[float], dur: float, amp: float = 0.3, decay: float = 4.0, square: bool = False) -> None:
        x = np.arange(int(dur * sr)) / sr
        env = np.minimum(1.0, x / 0.004) * np.exp(-decay * x)
        s = np.zeros_like(x)
        for f in freqs:
            v = np.sin(2 * np.pi * f * x)
            s += np.where(v >= 0, 0.5, -0.5) if square else v
        add(t, amp * env * s / len(freqs))

    def noise(t: float, dur: float, amp: float = 0.15) -> None:
        n = int(dur * sr)
        w = rng.uniform(-1, 1, n)
        y = np.zeros(n)
        prev = 0.0
        for k in range(n):  # one-pole low-pass, like the canonical
            prev = 0.7 * prev + 0.3 * w[k]
            y[k] = prev
        add(t, amp * np.sin(np.pi * np.arange(n) / max(1, n)) * y)

    s1, s2, s3, s4, s5 = (plan[k] for k in ("s1", "s2", "s3", "s4", "s5"))
    cuts = plan["cuts"]
    tone(0.02, [880, 1320], 0.08, 0.18, 30)
    for m in s1["meta"]:
        if m["t1"] > m["t0"]:
            for t in char_times(m["t0"], m["t1"], len(m["text"])):
                click(t, 0.22)
    for ln in s1["title"]:
        for t in char_times(ln["t0"], ln["t1"], len(ln["text"])):
            click(t, 0.28)
    tone(s1["hlT"], [660, 990], 0.2, 0.25, 9)
    # scanner
    tone(cuts[1], [880, 1320], 0.08, 0.18, 30)
    for t in char_times(s2["progT"], s2["resT"], 12):
        click(t, 0.18)
    tone(s2["resT"], [660, 990], 0.25, 0.3, 8)
    click(s2["locT"], 0.3)
    # models
    noise(cuts[2] - 0.05, 0.2, 0.12)
    for t in (cuts[2] + 0.35, cuts[2] + 0.65):
        tone(t, [880, 1320], 0.08, 0.18, 30)
    for k in range(4):
        click(s3["slopT"] + k * 0.12, 0.3)
    click(s3["pressT"])
    for t in char_times(s3["pressT"] + 0.18, s3["stampT"] - 0.1, 14):
        click(t, 0.15)
    tone(s3["stampT"], [523, 659, 784], 0.5, 0.3, 5)
    noise(s3["stampT"] + 0.18, 0.15, 0.2)
    # error
    tone(cuts[3], [440, 554, 659], 0.45, 0.35, 6)
    click(s4["pressT"])
    for i, t in enumerate(s4["copies"]):
        tone(t, [300 + 25 * i], 0.06, 0.14, 25, square=True)
    noise(s4["dllT"], 0.25, 0.5)
    tone(s4["dllT"], [110, 165], 0.6, 0.6, 4)
    tone(s4["dllT"], [440, 554, 659], 0.5, 0.35, 6)
    # dial-up
    digits = [(697, 1209), (770, 1336), (852, 1477), (697, 1336), (941, 1336)]
    for k, d in enumerate(digits):
        tone(cuts[4] + 0.04 + k * 0.09, list(d), 0.07, 0.25, 2)
    hiss_t = cuts[4] + 0.52
    if hiss_t < s5["heroT"] - 0.1:
        tone(hiss_t, [2100], min(0.25, s5["heroT"] - hiss_t), 0.10, 2)
        noise(hiss_t + 0.2, max(0.05, min(0.3, s5["heroT"] - hiss_t - 0.2)), 0.14)
    tone(s5["heroT"], [784, 1175], 0.3, 0.3, 7)
    # end card
    ec = plan["endcard"]["start"]
    tone(ec + 0.03, [392, 587, 784, 988], 0.9, 0.35, 3.0)
    tone(ec + 0.8, [784, 988], 0.5, 0.15, 5)

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
    for token in ("__R_DURATION__", "__R_VO_DURATION__", "/*__R_DATA__*/null", "__R_TITLE__"):
        if token not in html:
            raise BuildError(f"template is missing placeholder {token}")
    inject = {k: v for k, v in plan.items() if k not in ("audio", "voice_source")}
    vo_dur = f"{min(probe_duration(Path(bus['voice'])), plan['duration']):.6f}"
    html = (html.replace("__R_VO_DURATION__", vo_dur)
                .replace("__R_DURATION__", f"{plan['duration']:.6f}")
                .replace("/*__R_DATA__*/null", json.dumps(inject, ensure_ascii=False).replace("</", "<\\/"))
                .replace("__R_TITLE__", (slug or wav.stem).replace("<", "")))
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
    ap = argparse.ArgumentParser(description="Instantiate the style R (90s desktop OS) template.")
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
