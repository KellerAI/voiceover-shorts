#!/usr/bin/env python3
"""Instantiate the style E (UI x kinetic type) template for any narration.

Canonical look: the reference lookdev (read only). The template keeps its system: C's glass
agent-UI world (terminal -> tasks -> checks, one perspective camera, travel blur, depth of field,
sheen, reel HUD, grain) carrying a screen-space kinetic type layer with two registers (heavy caps
slams and a hand-lettered second voice), the gleefully awful "sloppy" crash with a camera shake,
the orange tension band and the mint closing caps with the orange tell. Every beat is driven by
the words file instead of the hard-coded r2-15s timings.

Usage:
  python3 styles/E/build.py --wav VOICE.wav --words WORDS.json --out DIR
                            [--duration S] [--brand BRAND.json] [--content CONTENT.json]
                            [--slug S49] [--json]

Inputs (script-agnostic):
  --wav       narration WAV (any rate/channels; ffmpeg reads it).
  --words     word timings: a list of {text, start, end} (lookdev format) or {"words": [...]}.
  --duration  narration length in seconds; default: the WAV duration (ffprobe). Every word must
              end inside it. The voice is cut to min(duration, end-card start); the composition
              runs past it to hold the end card.
  --brand     optional JSON {logo, name, line, bg, fg, accent} for KAEndCard.mount (logo may be a
              local file; it is copied into the project). Default: the light official card
              (#FBF7F0 ground, #10243E ink) so the cut from the dark UI is a hard scene change.
  --content   optional JSON overriding style copy: kickers [3], hud [3], window_title, ctx_dir,
              ctx_branch, tasks_title, checks_title, button, merge, card_title, card_sub, stamp,
              endpill.
  --slug      optional short id (default: the WAV stem); used in the title and the terminal ctx.

How the words drive the picture:
  Nine anchors from the canonical beat map (fail slam 2.08, slop crash 3.76, flight 1 4.70, act-2
  slam 5.60, accent slam 7.04, button press 8.24, tension 10.28, orange word 11.52, closing caps
  12.29 of a 14.30 s cut) are scaled to the end-card start and snapped to word starts by dynamic
  programming (cost = squared miss + a penalty for a stop word on a slam / a mid-phrase word on a
  structural beat), with a minimum gap per beat. The words spoken inside each anchor window
  become that beat's kinetic text, so the type always echoes the voice at the moment it is said.
  Words before flight 1 type into the terminal as spoken; words of act 2 are the task rows (each
  ticks when its phrase ends); words after flight 2 are the check rows (all chime green, then the
  last row turns orange).

Fixes against the preflight defects (relative to r2-15s-E): every text run >= 44 px (legibility),
opaque card fills so the lower third is never empty, the shared end card (kit/endcard.js) cuts in
0.1 s after the last word and holds 2 s with grain + a logo push (no dead frames), the audio is
built by kit/sfx_bus.py (voice -14 LUFS, SFX 14 dB under the voice peak), and every font is a
bundled OFL face from kit/fonts (D4): Lilita One, Comic Shanns, Virgil. Canonical -> OFL map:
Avenir Next and Avenir Next Condensed Heavy -> Lilita One; Menlo and Comic Sans MS -> Comic Shanns;
Didot Italic, Marker Felt and Chalkduster -> Virgil (the serif-italic second register becomes a
hand-drawn one: a deliberate look change, because no OFL serif italic is bundled).

Output (DIR):
  DIR/project/   a HyperFrames project: index.html, kit/{gsap.min.js,endcard.js},
                 assets/{voice_norm.wav,sfx_bus.wav,mix.wav,fonts/,brand/}
  DIR/plan.json  the plan injected into index.html (also printed with --json)
  DIR/sfx_raw.wav the synthesised E sound layer before the bus
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
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN = HERE.parents[1]
KIT = PLUGIN / "kit"
TEMPLATE = HERE / "template"
sys.path.insert(0, str(PLUGIN))
sys.path.insert(0, str(HERE))

from kit import sfx_bus  # noqa: E402

FPS = 30
EC_GAP = 0.1      # KAEndCard.DEFAULT_GAP
EC_HOLD = 2.0     # KAEndCard.DEFAULT_HOLD (the scene detector needs >= 1.5)
ORIG_END = 14.30  # r2-15s-E composition end; the anchors scale to the end-card start
# (name, canonical time, min gap from the previous anchor, kind)
ANCHORS = [
    ("fail", 2.08, 0.9, "slam"),
    ("slop", 3.76, 1.0, "slam"),
    ("leg1", 4.70, 0.9, "struct"),
    ("rig", 5.60, 0.8, "slam"),
    ("exactly", 7.04, 0.8, "slam"),
    ("press", 8.24, 0.6, "struct"),
    ("tension", 10.28, 1.7, "struct"),
    ("worse", 11.52, 0.6, "slam"),
    ("end", 12.29, 0.6, "struct"),
]
TAIL_MIN = 1.3    # end (closing caps + pull-back) to the end-card cut
STOP_PEN = 1.5    # a slam anchored on a stop word
MID_PEN = 2.0     # a structural beat anchored mid-phrase
LEG1, LEG2 = 0.70, 0.46   # flight lengths (s)
ROW_CHARS = 24    # list row width (Lilita One 50 px in a 940 px card)
LINE_CHARS = 26   # terminal line width (Comic Shanns 48 px)
MAX_ROWS = 6

DEFAULT_TEXT = {
    "kickers": ["THE PLAN", "EXECUTION", "VERIFICATION"],
    "hud": ["PLAN · STREAM", "TASKS · EXECUTE", "CI · VERIFY · ???"],
    "window_title": "agent — zsh",
    "ctx_dir": "~/kellerai/",
    "ctx_branch": "main",
    "tasks_title": "Run",
    "checks_title": "Checks",
    "button": "Run checks →",
    "merge": "Merge",
    "card_title": "sloppy-run.py",
    "card_sub": "23 failing · 4 todo",
    "stamp": "LGTM!!1",
    "endpill": "acceptance · 0 tests",
}

STOP = set("""a an the and or but if then so of in on at to for from by with as into onto over under about
this that these those it its it's i i'm i've me my we our you your he she they them their his her
is are was were be been being am do does did done have has had having will would can could should
may might must shall just very really also too than more most much many some any each every
there here what which who whom whose when where why how all both own same such only
s t don't doesn't isn't aren't wasn't weren't let's one
called based takes take took get gets got make makes made want wants say says said""".split())
STOP -= {"not", "no", "never"}  # negations are content


class BuildError(Exception):
    """Usage or IO problem (CLI exit 2)."""


# ----------------------------------------------------------------------------- inputs

def load_words(src) -> list[dict]:
    if isinstance(src, (str, Path)):
        try:
            data = json.loads(Path(src).read_text())
        except (OSError, ValueError) as e:
            raise BuildError(f"cannot read words json {src}: {e}") from e
    else:
        data = src
    if isinstance(data, dict):
        data = data.get("words")
    if not isinstance(data, list) or not data:
        raise BuildError("words must be a non-empty list of {text, start, end}")
    out = []
    for w in data:
        try:
            out.append({"text": str(w["text"]).strip(), "start": round(float(w["start"]), 4),
                        "end": round(float(w["end"]), 4)})
        except (KeyError, TypeError, ValueError) as e:
            raise BuildError(f"bad word entry {w!r}") from e
    out = [w for w in out if w["text"]]
    out.sort(key=lambda w: w["start"])
    if len(out) < 3:
        raise BuildError("need at least 3 words")
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


def _load_obj(v, what) -> dict:
    if v is None:
        return {}
    if isinstance(v, dict):
        return dict(v)
    try:
        obj = json.loads(Path(v).read_text())
    except (OSError, ValueError) as e:
        raise BuildError(f"cannot read {what} {v}: {e}") from e
    if not isinstance(obj, dict):
        raise BuildError(f"--{what} must hold a JSON object")
    return obj


# ----------------------------------------------------------------------------- text helpers

def clean(tok: str) -> str:
    return re.sub(r"[^\w'’\-]", "", tok)


def is_content(tok: str) -> bool:
    c = clean(tok)
    return bool(c) and c.lower().strip("'’") not in STOP and len(c) >= 2


def ends_sentence(t: str) -> bool:
    return bool(re.search(r"[.!?][\"')\]]*$", t))


def ends_clause(t: str) -> bool:
    return bool(re.search(r"[,;:—–][\"')\]]*$", t))


def phrase_starts(words: list[dict]) -> list[bool]:
    """True where a word opens a phrase (first word, or the previous word ends with punctuation
    or a pause of >= 0.2 s)."""
    res = [True]
    for i in range(1, len(words)):
        p = words[i - 1]
        res.append(ends_sentence(p["text"]) or ends_clause(p["text"]) or words[i]["start"] - p["end"] >= 0.2)
    return res


def phrase_rows(words: list[dict], max_chars: int, max_rows: int = MAX_ROWS) -> list[dict]:
    """Greedy phrase chunks (break at punctuation or width); widen until there are <= max_rows."""
    width = max_chars
    while True:
        rows, cur = [], []
        for w in words:
            cand = " ".join(x["text"] for x in cur + [w])
            if cur and len(cand) > width:
                rows.append(cur)
                cur = []
            cur.append(w)
            if (ends_sentence(w["text"]) or ends_clause(w["text"])) and len(" ".join(x["text"] for x in cur)) >= 8:
                rows.append(cur)
                cur = []
        if cur:
            rows.append(cur)
        if len(rows) <= max_rows or width > 400:
            break
        width += 4
    return [{"text": " ".join(x["text"] for x in r), "start": round(r[0]["start"], 4), "end": round(r[-1]["end"], 4),
             "words": [{"text": x["text"], "start": round(x["start"], 4), "end": round(x["end"], 4)} for x in r]}
            for r in rows]


def default_head(words: list[dict]) -> list[str]:
    toks = [re.sub(r"[,;:—–]+$", "", w["text"]) for w in words[-2:]]
    toks = [t for t in toks if t]
    if len(toks) == 2 and len(" ".join(toks)) > 22:
        toks = toks[-1:]
    if toks and not ends_sentence(toks[-1]):
        toks[-1] += "."
    return toks or ["…"]


def _frame_ceil(t: float) -> float:
    return math.ceil(t * FPS - 1e-6) / FPS


# ----------------------------------------------------------------------------- plan

def endcard_plan(last_word_end: float) -> dict:
    """Mirror of KAEndCard.plan() with the default gap/hold (the page re-checks nothing; the
    scene-detector rule is asserted by the test against the render)."""
    start = round(last_word_end + EC_GAP, 4)
    end = round(_frame_ceil(max(start, last_word_end) + EC_HOLD), 6)
    return {"start": start, "end": end, "hold": EC_HOLD}


def pick_anchors(words: list[dict], ec_start: float) -> dict:
    """Snap the nine canonical anchors (scaled to ec_start) to word starts; see the module doc."""
    starts = phrase_starts(words)
    targets = [ec_start * a[1] / ORIG_END for a in ANCHORS]
    gaps = [a[2] for a in ANCHORS]
    kinds = [a[3] for a in ANCHORS]
    need = sum(gaps) + TAIL_MIN
    if ec_start < need:
        raise BuildError(f"narration too short for style E: end card at {ec_start:.2f} s, needs >= {need:.2f} s")
    cands = [(w["start"], i) for i, w in enumerate(words) if w["start"] > 0]

    def pen(k, i):
        if kinds[k] == "slam":
            return 0.0 if is_content(words[i]["text"]) else STOP_PEN
        return 0.0 if starts[i] else MID_PEN

    INF = float("inf")
    n, m = len(ANCHORS), len(cands)
    best = [[(INF, -1)] * m for _ in range(n)]
    for j, (t, i) in enumerate(cands):
        if t >= gaps[0]:
            best[0][j] = ((t - targets[0]) ** 2 + pen(0, i), -1)
    for k in range(1, n):
        for j, (t, i) in enumerate(cands):
            c0 = (t - targets[k]) ** 2 + pen(k, i)
            for jj in range(j):
                if best[k - 1][jj][0] < INF and t - cands[jj][0] >= gaps[k]:
                    c = best[k - 1][jj][0] + c0
                    if c < best[k][j][0]:
                        best[k][j] = (c, jj)
    last = [(best[n - 1][j][0], j) for j, (t, _) in enumerate(cands)
            if ec_start - t >= TAIL_MIN and best[n - 1][j][0] < INF]
    if last:
        j = min(last)[1]
        times = []
        for k in range(n - 1, -1, -1):
            times.insert(0, cands[j][0])
            j = best[k][j][1]
        snapped = True
    else:  # too few words for word-aligned anchors: the proportional points, pushed apart
        times, prev = [], 0.0
        for k in range(n):
            t = max(targets[k], prev + gaps[k])
            times.append(t)
            prev = t
        if ec_start - times[-1] < TAIL_MIN:
            raise BuildError("cannot fit the style E beats before the end card")
        snapped = False
    out = {a[0]: round(t, 4) for a, t in zip(ANCHORS, times)}
    out["_snapped"] = snapped
    return out


def plan_timeline(words: list[dict], narration: float, slug: str, text: dict) -> dict:
    lwe = max(w["end"] for w in words)
    if lwe > narration + 1e-3:
        raise BuildError(f"last word ends at {lwe:.3f} s, after the narration ({narration:.3f} s)")
    ec = endcard_plan(lwe)
    A = pick_anchors(words, ec["start"])
    snapped = A.pop("_snapped")

    def win(lo, hi):
        return [w for w in words if lo - 1e-6 <= w["start"] < hi - 1e-6]

    def content(ws):
        return [clean(w["text"]) for w in ws if is_content(w["text"])]

    def first_content(lo, hi):
        c = content(win(lo, hi))
        if c:
            return c[0]
        later = content(win(lo, ec["start"]))
        return later[0] if later else (clean(win(0, ec["start"])[-1]["text"]) or "check")

    # ---- derived beat times
    leg1 = [A["leg1"], round(A["leg1"] + LEG1, 4)]
    press, release = A["press"], round(A["press"] + 0.12, 4)
    leg2 = [round(press + 0.32, 4), round(press + 0.32 + LEG2, 4)]
    wcand = [w["start"] for w in win(A["fail"] + 0.45, A["slop"] - 0.35)]
    wtarget = A["fail"] + (2.72 - 2.08) / (3.76 - 2.08) * (A["slop"] - A["fail"])
    worry = round(min(wcand, key=lambda t: abs(t - wtarget)) if wcand else wtarget, 4)
    slop_out = round(A["leg1"] - 0.08, 4)

    act1 = win(0, A["leg1"])
    act2 = win(A["leg1"], leg2[0])
    act3 = win(leg2[0], ec["start"])
    rows1 = phrase_rows(act1, LINE_CHARS, max_rows=8) if act1 else []
    rows2 = phrase_rows(act2, ROW_CHARS) or [{"text": "execute", "start": leg1[1], "end": press, "words": []}]
    rows3 = phrase_rows(act3, ROW_CHARS) or [{"text": "acceptance", "start": leg2[1], "end": ec["start"], "words": []}]

    lo, hi = leg1[1] + 0.15, press - 0.3
    ticks = []
    for r in rows2:
        t = max(r["end"], lo, ticks[-1] + 0.12 if ticks else lo)
        ticks.append(t)
    nk = len(ticks)
    ticks = [round(min(t, hi - (nk - 1 - i) * 0.12), 4) for i, t in enumerate(ticks)]
    ticks = [max(t, round(lo + i * 0.06, 4)) for i, t in enumerate(ticks)]
    checks = [round(leg2[1] + 0.02 + i * 0.12, 4) for i in range(len(rows3))]
    all_pass = round(checks[-1] + 0.08, 4)
    scand = [w["start"] for w in win(A["tension"] + 0.15, A["worse"] - 0.2)]
    still = round(scand[0] if scand else A["tension"] + 0.28, 4)

    keys = []
    for r in rows1:
        for w in r["words"]:
            n = len(w["text"]) + 1
            keys += [w["start"] + (w["end"] - w["start"]) * c / n for c in range(n)]
    if len(keys) > 90:
        keys = [keys[int(k * len(keys) / 90)] for k in range(90)]

    T = {
        **{k: A[k] for k in ("fail", "slop", "rig", "exactly", "tension", "worse", "end")},
        "worry": worry, "slopOut": slop_out, "leg1": leg1, "leg2": leg2,
        "ticks": ticks, "match": round(ticks[-1] + 0.1, 4),
        "cursorIn": round(max(leg1[1] + 0.2, press - 0.74), 4), "press": press, "release": release,
        "checks": checks, "allPass": all_pass, "still": still,
        "ecStart": ec["start"], "END": ec["end"], "lastWordEnd": round(lwe, 4),
        "keys": [round(k, 4) for k in keys], "lineTicks": [r["start"] for r in rows1[1:]],
    }

    # ---- kinetic text: the words spoken inside each anchor window
    fail_c = content(win(A["fail"], worry))[:2] or [first_content(A["fail"], A["slop"])]
    worry_txt = " ".join(w["text"] for w in win(worry, A["slop"]))
    if len(worry_txt) > 28:
        cut = []
        for w in win(worry, A["slop"]):
            if len(" ".join(cut + [w["text"]])) > 28:
                break
            cut.append(w["text"])
        worry_txt = " ".join(cut)
    sc = content(win(A["slop"], A["leg1"]))
    if len(sc) < 2:  # too few words at the crash: lead with the words just before it
        sc = (content(win(A["fail"], A["slop"])) + sc)[-2:]
    if len(sc) > 2:  # the two longest, in spoken order
        keep = sorted(sorted(range(len(sc)), key=lambda i: -len(sc[i]))[:2])
        sc = [sc[i] for i in keep]
    slop = [(sc[0] if sc else "sloppy").lower(), ((sc[1] if len(sc) > 1 else "agents").lower() + "!!")]
    rc = content(win(A["rig"], A["exactly"]))
    rig = (max(rc, key=len) if rc else first_content(A["rig"], A["exactly"])).upper() + "."
    accent = first_content(A["exactly"], press)
    lead = content(win(leg1[0], A["exactly"]))
    lead = [x for x in lead if x.upper() + "." != rig] or lead
    head_b = ([lead[-1][:1].upper() + lead[-1][1:]] if lead and lead[-1].lower() != accent.lower() else []) + [accent.lower() + "."]
    still_ws = win(A["tension"], A["worse"])
    still_lines, cur = [], []
    for w in still_ws:
        if cur and len(" ".join(cur + [w["text"]])) > 20:
            still_lines.append(" ".join(cur))
            cur = []
            if len(still_lines) == 2:
                break
        cur.append(w["text"])
    if cur and len(still_lines) < 2:
        still_lines.append(" ".join(cur))
    worse_c = content(win(A["worse"], A["end"]))[-2:] or [first_content(A["worse"], A["end"])]
    worse = [" ".join(x.lower() for x in worse_c) + "."]
    green_c = content(win(A["end"], ec["start"]))[-2:] or [first_content(A["end"], ec["start"])]
    green_c = [g.upper() for g in green_c]
    green = [" ".join(green_c) + "."] if len(" ".join(green_c)) <= 10 else [green_c[0], " ".join(green_c[1:]) + "."]
    if len(green) == 2 and green[1] == ".":
        green = [green[0] + "."]

    first_phrase = []
    for i, w in enumerate(words):
        if i and phrase_starts(words)[i]:
            break
        first_phrase.append(w)
    head3 = win(leg2[0], A["tension"]) or act3
    acts = [
        {"start": act1[0]["start"] if act1 else 0.0, "end": act1[-1]["end"] if act1 else leg1[0],
         "head": default_head(first_phrase), "rows": rows1},
        {"start": leg1[0], "end": leg2[0], "head": head_b, "rows": rows2},
        {"start": leg2[0], "end": ec["start"], "head": default_head(head3), "rows": rows3},
    ]
    kinetic = {"fail": [c.upper() for c in fail_c], "worry": [worry_txt] if worry_txt else [],
               "slop": slop, "rig": [rig], "still": still_lines, "worse": worse, "green": green}
    return {
        "anchors": {a[0]: A[a[0]] for a in ANCHORS}, "snapped": snapped,
        "T": T, "acts": acts, "kinetic": kinetic, "text": text,
        "firstWordStart": words[0]["start"], "lastWordEnd": round(lwe, 4),
        "narration": round(narration, 6), "endcard": ec, "duration": ec["end"], "slug": slug,
    }


# ----------------------------------------------------------------------------- build

def build(wav, words, out, *, duration=None, brand=None, content=None, slug=None) -> dict:
    wav, out = Path(wav), Path(out)
    if not wav.is_file():
        raise BuildError(f"wav not found: {wav}")
    ws = load_words(words)
    narr = float(duration) if duration is not None else probe_duration(wav)
    if narr <= 0:
        raise BuildError("duration must be > 0")
    brand = _load_obj(brand, "brand")
    content = _load_obj(content, "content")
    slug = slug or wav.stem
    text = {**DEFAULT_TEXT, **{k: v for k, v in content.items() if k in DEFAULT_TEXT}}
    text.setdefault("ctx_branch", "main")
    if "ctx_branch" not in content:
        text["ctx_branch"] = re.sub(r"[^\w.\-]", "", slug.lower())[:18] or "main"
    for k in ("kickers", "hud"):
        if not (isinstance(text[k], list) and len(text[k]) == 3):
            raise BuildError(f"content.{k} must be a list of 3 strings")
    plan = plan_timeline(ws, narr, slug, text)

    proj = out / "project"
    for sub in ("kit", "assets/fonts", "assets/brand"):
        (proj / sub).mkdir(parents=True, exist_ok=True)
    shutil.copyfile(PLUGIN / "kit" / "vendor" / "gsap.min.js", proj / "kit" / "gsap.min.js")
    shutil.copyfile(KIT / "endcard.js", proj / "kit" / "endcard.js")
    for f in ("LilitaOne.woff2", "ComicShanns.woff2", "Virgil.woff2"):
        shutil.copyfile(KIT / "fonts" / f, proj / "assets" / "fonts" / f)
    logo = brand.get("logo")
    if logo and Path(logo).is_file():
        dst = proj / "assets" / "brand" / Path(logo).name
        shutil.copyfile(logo, dst)
        brand["logo"] = f"assets/brand/{dst.name}"
    else:
        shutil.copyfile(KIT / "brand" / "keller-ai-logo.svg", proj / "assets" / "brand" / "keller-ai-logo.svg")
        brand["logo"] = "assets/brand/keller-ai-logo.svg"
    brand.setdefault("name", "KELLER AI")
    brand.setdefault("bg", "#FBF7F0")
    brand.setdefault("fg", "#10243E")
    plan["brand"] = brand

    # audio: the E sound layer on this plan -> kit/sfx_bus (voice -14 LUFS, SFX 14 dB under)
    import sfx as sfx_mod  # styles/E/sfx.py
    raw = sfx_mod.render(plan["T"], out / "sfx_raw.wav")
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
    for token in ("__E_DURATION__", "__E_VO_DURATION__", "/*__E_DATA__*/null", "__E_TITLE__"):
        if token not in html:
            raise BuildError(f"template is missing placeholder {token}")
    inject = {k: v for k, v in plan.items() if k not in ("audio", "voice_source")}
    vo_dur = f"{min(probe_duration(Path(bus['voice'])), plan['duration']):.6f}"
    html = (html.replace("__E_VO_DURATION__", vo_dur)
                .replace("__E_DURATION__", f"{plan['duration']:.6f}")
                .replace("/*__E_DATA__*/null", json.dumps(inject, ensure_ascii=False).replace("</", "<\\/"))
                .replace("__E_TITLE__", slug.replace("<", "").replace("&", "")))
    (proj / "index.html").write_text(html)
    plan["project"] = str(proj)
    (out / "plan.json").write_text(json.dumps(plan, indent=1, ensure_ascii=False))
    return plan


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Instantiate the style E template.")
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
        plan = build(Path(a.wav), Path(a.words), Path(a.out), duration=a.duration, brand=a.brand,
                     content=a.content, slug=a.slug)
    except (BuildError, OSError, ValueError) as e:
        print(f"build.py: {e}", file=sys.stderr)
        return 2
    if a.json:
        print(json.dumps(plan, indent=1, ensure_ascii=False))
    else:
        print(f"built {plan['project']} duration={plan['duration']:.3f}s anchors={plan['anchors']}")
    return 0 if plan["audio"]["bus_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
