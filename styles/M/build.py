#!/usr/bin/env python3
"""Instantiate the style M (blueprint draft) template for any narration.

Canonical look: the reference lookdev (read only). The template keeps its system: a navy drafting
sheet with a two-level cream grid, one pen head that inks every stroke (labels snap on as the pen
lifts), a title that starts big mid-sheet and drops to one compact line on its spoken word, two
component boxes with CHECKS / OK arrows, a shared bar, a big callout, a sticky-note gag, a red
stamp, a FIX arrow into the payoff panel with an icon, a REV letter that ticks up, grain, vignette
and a slow camera push. The hard-coded S49 times and copy are replaced by a plan derived from the
words file.

Usage:
  python3 styles/M/build.py --wav VOICE.wav --words WORDS.json --out DIR
                            [--duration S] [--brand BRAND.json] [--content CONTENT.json]
                            [--slug S49] [--json]

Inputs (script-agnostic):
  --wav       narration WAV (any rate/channels; ffmpeg reads it).
  --words     word timings: a list of {text, start, end} (lookdev format) or {"words": [...]}.
  --duration  narration length in seconds; default: the WAV duration (ffprobe). Every word must
              end inside it. The composition runs past it to hold the end card.
  --brand     optional JSON {logo, name, line, bg, fg, accent} for KAEndCard.mount (kit defaults);
              "name" is also the title-block wordmark (default "Keller AI").
  --content   optional JSON overriding derived or default copy, keys of DEFAULT_CONTENT.
  --slug      optional short id printed on the sheet header (default: the WAV stem).

How the words drive the sheet:
  * Words are split into clauses (punctuation or a pause >= 0.35 s).
  * Title: the last one or two content words of the first sentence that ends inside the first
    45 % of the narration (else the first two content words). It drops to the top 0.1 s before
    its first word is spoken; the pen underlines it.
  * The closing clause (last clause, merged with the one before when it is thin and in the same
    sentence) is the payoff: its first content word is anchor B; the first clause after the title
    sentence is anchor A. The canonical stroke schedule (boxes, arrows, bar, callout, sticky, FIX
    arrow, payoff panel, icon, REV ticks) is mapped linearly from the canonical anchors
    (4.40 s, 13.20 s) onto (A, B), so the diagram resolves before the payoff is spoken.
  * The stamp lands on the first negation word ("not", "no", "never", ...) after the bar, else
    at the mapped canonical time; the payoff sub-line prints on anchor B; the panel pulses on the
    last word. The shared Keller AI end card (kit/endcard.js) cuts in 0.1 s after the last word
    and holds 2 s with a slow push (no frozen frames).
  * Default copy is derived from the clauses (box labels = last content words of each middle
    clause, payoff = the title, payoff sub-line = the closing clause). --content overrides it.

Output (DIR):
  DIR/project/   a HyperFrames project: index.html, kit/{gsap.min.js,endcard.js},
                 assets/{voice_norm.wav,sfx_bus.wav,mix.wav,fonts/,brand/}
  DIR/plan.json  the plan injected into index.html (also printed with --json)
  DIR/sfx_raw.wav the synthesised drafting SFX before the bus
Render it with:
  cd DIR && npm_config_offline=true sandbox-exec -f <repo>/kit/offline.sb \\
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
END_GAP = 0.1      # KAEndCard.DEFAULT_GAP
END_HOLD = 2.0     # KAEndCard.DEFAULT_HOLD
FONTS = ["LilitaOne.woff2", "ComicShanns.woff2", "Virgil.woff2"]
PAUSE = 0.35
MIN_SPAN = 3.0     # anchor B - anchor A must leave room for the diagram

# Canonical r5M-S49-M v2 schedule (index.html #sched and the GSAP calls), in seconds.
CANON_A, CANON_B = 4.40, 13.20
CANON_STROKES = [  # id, t, dur  (between the anchors; mapped)
    ("boxA", 4.50, 0.90), ("dimA", 5.50, 0.35), ("boxB", 6.10, 0.75), ("arrAB", 7.05, 0.35),
    ("arrBA", 8.15, 0.35), ("drops", 8.70, 0.28), ("bar", 9.00, 0.58), ("lead", 9.62, 0.25),
    ("fix", 10.12, 0.36), ("boxK", 10.50, 0.52), ("icon", 11.05, 0.70),
]
CANON_REVS = [4.50, 7.05, 9.00, 10.50]
CANON_STICKY, CANON_STAMP, CANON_KFILL = 9.80, 11.90, 11.00
NEGATIONS = {"not", "no", "never", "isn't", "aren't", "wasn't", "weren't", "don't", "doesn't", "can't",
             "won't", "nothing", "none", "without"}

STOP = set("""a an the and or but if then so of in on at to for from by with as into onto over under about
this that these those it its it's i i'm i've me my we our you your he she they them their his her
is are was were be been being am do does did done have has had having will would can could should
may might must shall just very really also too than more most much many some any each every
there here what which who whom whose when where why how all both own same such only
s t let's one called based takes take took get gets got make makes made want wants say says said
argues argue""".split())
STOP -= {"not", "no", "never"}

DEFAULT_CONTENT = {
    "head": "SYSTEM DIAGRAM",     # header rule, left
    "sheet": "",                  # header rule, right; default "SHEET <slug>"
    "brief": "",                  # under the title underline; default "A <BRAND NAME> BRIEF"
    "title": [],                  # 1-2 lines; default derived from the first sentence
    "a": "", "a_sub": "STEP 01",  # box A label (default derived) and sub-label
    "b": "", "b_sub": "STEP 02",  # box B
    "dim": "SCOPE",               # dimension line over box A
    "checks": "FEEDS",            # A -> B arrow label
    "ok": "“OK”",       # B -> A return arrow label
    "bar": "",                    # shared bar; default derived
    "callout": "",                # big callout word; default: longest content word late in the script
    "callout_value": "?",         # the 170 px yellow line under it
    "fix": "FIX",
    "payoff": [],                 # 1-2 lines in the payoff panel; default: the title
    "payoff_sub": "",             # yellow line under it; default: the closing clause
    "sticky": ["trust me,", "it works!!"],
    "sign": "~ the intern :)",
    "stamp": "NOT TO SPEC",
    "icon": "seal",               # "seal" or "bank"
    "wordmark": "",               # title block; default brand name or "Keller AI"
}
# Characters the bundled faces lack -> covered stand-ins (checked again with fontTools below).
GLYPH_MAP = {"≈": "~", "→": ">", "←": "<", "—": "-", "–": "-"}
MONO_IDS = {"L_head", "L_head2", "L_brief", "L_A2", "L_dimA", "L_B2", "L_chk", "L_ok", "L_fix", "L_K3"}
MONO_MAP = {"·": "-"}  # Comic Shanns has no middle dot


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


def is_content(tok: str) -> bool:
    c = clean(tok).lower().strip("'’")
    return bool(c) and c not in STOP


def base(tok: str) -> str:
    return re.sub(r"['’]s$", "", clean(tok))


def ends_sentence(tok: str) -> bool:
    return bool(re.search(r"[.?!]['\"’”]?$", tok.strip()))


def ends_clause(tok: str) -> bool:
    return bool(re.search(r"[.,;:?!—–]['\"’”]?$", tok.strip()))


def fit(s: str, n: int) -> str:
    s = s.strip()
    return s if len(s) <= n else s[: max(1, n - 1)].rstrip() + "~"


def kw(clause: dict | None, n: int) -> str:
    if not clause:
        return ""
    return " ".join(base(w["text"]).upper() for w in clause["content"][-n:])


def endcard_plan(last_word_end: float) -> dict:
    """Mirror of KAEndCard.plan() with default gap/hold (the template re-checks it in JS)."""
    start = round(last_word_end + END_GAP, 6)
    end = math.ceil((max(start, last_word_end) + END_HOLD) * FPS - 1e-6) / FPS
    return {"start": start, "end": round(end, 6), "hold": END_HOLD}


def clauses_of(words: list[dict]) -> list[dict]:
    out, cur = [], []
    for i, w in enumerate(words):
        cur.append(w)
        nxt = words[i + 1] if i + 1 < len(words) else None
        if nxt is None or ends_clause(w["text"]) or nxt["start"] - w["end"] >= PAUSE:
            out.append({"words": cur, "start": cur[0]["start"], "end": cur[-1]["end"],
                        "content": [x for x in cur if is_content(x["text"])],
                        "sentence_end": ends_sentence(cur[-1]["text"])})
            cur = []
    return out


# ----------------------------------------------------------------------------- plan

def build_plan(words: list[dict], duration: float, slug: str, brand: dict, content: dict) -> dict:
    lwe = max(w["end"] for w in words)
    if lwe > duration + 1e-3:
        raise BuildError(f"last word ends at {lwe:.3f} s, after the narration ({duration:.3f} s)")
    unknown = set(content) - set(DEFAULT_CONTENT)
    if unknown:
        raise BuildError(f"--content has unknown keys: {sorted(unknown)}")
    co = dict(DEFAULT_CONTENT)
    co.update(content)
    if co["icon"] not in ("seal", "bank"):
        raise BuildError("--content icon must be 'seal' or 'bank'")
    ec = endcard_plan(lwe)
    C = clauses_of(words)

    # closing clause: the last clause (+ the one before when it is thin and in the same sentence)
    tot_first = len(C) - 1
    if tot_first > 0 and len(C[tot_first]["content"]) < 2 and not C[tot_first - 1]["sentence_end"]:
        tot_first -= 1
    closing_words = [w for c in C[tot_first:] for w in c["words"]]
    closing_content = [w for c in C[tot_first:] for w in c["content"]] or closing_words
    anchor_b = closing_content[0]["start"]

    # title sentence: the first sentence that ends inside the first 45 %
    title_words, title_last = [], -1
    s0 = 0
    for i, w in enumerate(words):
        if not ends_sentence(w["text"]):
            continue
        if w["end"] > 0.45 * lwe or w["start"] >= anchor_b:
            break
        cids = [j for j in range(s0, i + 1) if is_content(words[j]["text"])]
        if cids:
            title_words = [words[j] for j in cids[-2:]]
            title_last = i
            break
        s0 = i + 1
    if not title_words:  # no early sentence: the first two content words; the diagram starts after them
        cw = [w for w in words if is_content(w["text"])][:2] or words[:1]
        title_words = cw
        title_last = words.index(cw[-1])
    after_title = [c for c in C[:tot_first] if c["start"] > words[title_last]["end"] - 1e-6]
    anchor_a = after_title[0]["start"] if after_title else words[title_last]["end"] + 0.3
    if anchor_b - anchor_a < MIN_SPAN:
        raise BuildError(f"narration too short for a blueprint: {anchor_b - anchor_a:.2f} s between the title "
                         f"sentence and the closing clause (needs >= {MIN_SPAN} s)")
    middle: list[dict] = []
    for c in after_title:  # a thin clause (< 2 content words) joins the clause before it in its sentence
        if middle and len(c["content"]) < 2 and not middle[-1]["sentence_end"]:
            prev = middle[-1]
            middle[-1] = {"words": prev["words"] + c["words"], "start": prev["start"], "end": c["end"],
                          "content": prev["content"] + c["content"], "sentence_end": c["sentence_end"]}
        elif c["content"]:
            middle.append(c)

    k = (anchor_b - anchor_a) / (CANON_B - CANON_A)

    def mp(t: float) -> float:
        return round(anchor_a + (t - CANON_A) * k, 4)

    dk = min(1.0, k)
    drop = round(min(max(title_words[0]["start"] - 0.10, 0.6), anchor_a - 1.2), 4)
    strokes = [
        {"id": "border", "t": 0.05, "dur": round(min(0.95, max(0.3, drop - 0.3)), 4)},
        {"id": "headrule", "t": round(min(1.0, drop - 0.25), 4), "dur": 0.22},
        {"id": "uline", "t": round(drop + 0.68, 4), "dur": 0.40},
    ]
    for sid, t, d in CANON_STROKES:
        strokes.append({"id": co["icon"] if sid == "icon" else sid, "t": mp(t), "dur": round(d * dk, 4)})
    bar_t = mp(9.00)
    stamp_t = mp(CANON_STAMP)
    for w in words:
        if bar_t + 0.6 <= w["start"] < anchor_b - 0.2 and clean(w["text"]).lower() in NEGATIONS:
            stamp_t = round(w["start"] + 0.02, 4)
            break
    sticky_t = mp(CANON_STICKY)
    stamp_t = max(stamp_t, round(sticky_t + 0.8, 4))
    k3 = round(anchor_b, 4)
    pulse = round(max(words[-1]["start"] - 0.02, k3 + 0.4), 4)
    if pulse > ec["start"] - 0.25:
        pulse = round(max(k3 + 0.2, ec["start"] - 0.25), 4)
    revs = [[0, "A"]] + [[mp(t), "BCDE"[i]] for i, t in enumerate(CANON_REVS)]

    # ---- copy
    name = (brand.get("name") or "Keller AI").strip()
    title_lines = [str(x) for x in co["title"]][:2] or [fit(base(w["text"]).upper(), 12) for w in title_words]
    m0 = middle[0] if middle else None
    m1 = middle[1] if len(middle) > 1 else None
    rest = middle[2:] or middle[-1:]
    m2 = max(rest, key=lambda c: len(c["content"])) if rest else None
    late = [w for c in middle[len(middle) // 2:] for w in c["content"]] or [w for c in middle for w in c["content"]]
    hero = max(late, key=lambda w: len(base(w["text"])), default=None)
    payoff = [str(x) for x in co["payoff"]][:2] or title_lines
    lead_in = closing_words.index(closing_content[0]) if closing_content[0] in closing_words else 0
    sub = co["payoff_sub"] or " ".join(base(w["text"]).upper() for w in closing_words[lead_in:] if base(w["text"]))
    text = {
        "L_head": fit(co["head"], 22),
        "L_head2": fit(co["sheet"] or f"SHEET {slug.upper()}", 14),
        "L_brief": fit(co["brief"] or f"A {name.upper()} BRIEF", 34),
        "L_A1": fit(co["a"] or kw(m0, 2) or "INPUT", 16), "L_A2": fit(co["a_sub"], 13),
        "L_dimA": fit(co["dim"], 14),
        "L_B1": fit(co["b"] or kw(m1, 2) or "REVIEW", 16), "L_B2": fit(co["b_sub"], 13),
        "L_chk": fit(co["checks"], 8), "L_ok": fit(co["ok"], 8),
        "L_bar": fit(co["bar"] or " · ".join(base(w["text"]).upper() for w in (m2["content"] if m2 else [])[-4:]), 36),
        "L_ind1": fit(co["callout"] or (base(hero["text"]).upper() if hero else "CHECK"), 14),
        "L_ind2": fit(co["callout_value"], 6),
        "L_fix": fit(co["fix"], 14),
        "L_K1": fit(payoff[0], 13), "L_K2": fit(payoff[1], 13) if len(payoff) > 1 else "",
        "L_K3": fit(sub, 26),
        "L_word": fit(co["wordmark"] or name, 12),
        "L_tb2a": fit(title_lines[0], 12), "L_tb2b": fit(title_lines[1], 12) if len(title_lines) > 1 else "",
    }
    sticky = [str(x) for x in co["sticky"]][:2]
    if len(sticky) != 2:
        raise BuildError("--content sticky must be [line1, line2]")
    plan = {
        "strokes": strokes,
        "revs": revs,
        "title": {"lines": [fit(x, 12) for x in title_lines], "drop": drop, "words": [w["start"] for w in title_words]},
        "text": text,
        "sticky": {"t": sticky_t, "lines": [fit(x, 13) for x in sticky], "sign": fit(str(co["sign"]), 15)},
        "stamp": {"t": stamp_t, "text": fit(str(co["stamp"]), 12)},
        "kfill": mp(CANON_KFILL),
        "k3": k3,
        "pulse": pulse,
        "anchors": {"a": round(anchor_a, 4), "b": round(anchor_b, 4), "k": round(k, 4)},
        "firstWordStart": round(words[0]["start"], 4),
        "lastWordEnd": round(lwe, 4),
        "narration": round(duration, 6),
        "endcard": ec,
        "duration": ec["end"],
        "brand": brand,
    }
    sanitize_glyphs(plan)
    last = max(s["t"] + s["dur"] for s in strokes)
    if last > ec["start"] - 0.2:
        raise BuildError(f"diagram strokes end at {last:.2f} s, past the end card ({ec['start']:.2f} s)")
    return plan


def sanitize_glyphs(plan: dict) -> None:
    """Map characters the bundled faces lack to stand-ins, then check coverage with fontTools."""
    def fix(s: str, mono: bool) -> str:
        for a, b in GLYPH_MAP.items():
            s = s.replace(a, b)
        if mono:
            for a, b in MONO_MAP.items():
                s = s.replace(a, b)
        return s
    for k_, v in plan["text"].items():
        plan["text"][k_] = fix(v, k_ in MONO_IDS)
    plan["title"]["lines"] = [fix(x, False) for x in plan["title"]["lines"]]
    plan["sticky"]["lines"] = [fix(x, True) for x in plan["sticky"]["lines"]]
    plan["sticky"]["sign"] = fix(plan["sticky"]["sign"], False)
    plan["stamp"]["text"] = fix(plan["stamp"]["text"], False)
    try:
        from fontTools.ttLib import TTFont
    except ImportError:
        plan["glyph_check"] = "skipped: fontTools not installed"
        return
    cmaps: dict[str, dict[int, str]] = {}
    for f in ("LilitaOne.woff2", "ComicShanns.woff2"):
        cm = TTFont(str(PLUGIN / "kit" / "fonts" / f)).getBestCmap()
        if cm is None:
            raise BuildError(f"font {f} has no usable unicode cmap")
        cmaps[f] = cm
    runs = [(v, "ComicShanns.woff2" if k_ in MONO_IDS else "LilitaOne.woff2") for k_, v in plan["text"].items()]
    runs += [(x, "LilitaOne.woff2") for x in plan["title"]["lines"] + [plan["sticky"]["sign"], plan["stamp"]["text"]]]
    runs += [(x, "ComicShanns.woff2") for x in plan["sticky"]["lines"]]
    missing = sorted({(ch, f) for s, f in runs for ch in s if not ch.isspace() and ord(ch) not in cmaps[f]})
    if missing:
        raise BuildError("copy uses glyphs the bundled fonts lack: " + ", ".join(f"{c!r} ({f})" for c, f in missing))
    plan["glyph_check"] = "ok"


# ----------------------------------------------------------------------------- sfx (port of r5M-S49-M sfx_gen.py)

def synth_sfx(plan: dict, out: Path, sr: int = 48000) -> Path:
    """Pen scratch per stroke, a soft tick on each label snap, a chime per REV tick, a slap for the
    sticky, a thud for the stamp, a whoosh on the title drop, pings on the payoff line and pulse,
    and a soft thud on the end-card cut. Seeded numpy, no samples. Cue times come from the plan."""
    import numpy as np
    from scipy.signal import lfilter

    N = int(round(plan["duration"] * sr))
    out_buf = np.zeros(N)
    rng = np.random.default_rng(49)

    def place(sig, t):
        i = int(t * sr)
        if i < 0 or i >= N:
            return
        j = min(N, i + len(sig))
        out_buf[i:j] += sig[: j - i]

    def lp(x, a):  # one-pole low-pass, same as the canonical loop
        return np.asarray(lfilter([a], [1.0, a - 1.0], x))

    def env(n, att, rel):
        e = np.ones(n)
        a, r = int(att * sr), int(rel * sr)
        if a:
            e[:a] = np.linspace(0, 1, a)
        if r:
            e[-r:] *= np.linspace(1, 0, r)
        return e

    labs = {"border": False, "drops": False, "bank": False, "seal": False}
    for s in plan["strokes"]:
        n = max(1, int(s["dur"] * sr))
        w = rng.standard_normal(n)
        hp = w - lp(w, 0.25)
        wob = 0.6 + 0.4 * np.sin(2 * np.pi * (23 + rng.random() * 9) * np.arange(n) / sr) ** 2
        place(0.10 * hp * wob * env(n, min(0.03, s["dur"] / 3), min(0.06, s["dur"] / 3)), s["t"])
        if labs.get(s["id"], True):
            m = int(0.03 * sr)
            tt = np.arange(m) / sr
            place(0.35 * np.sin(2 * np.pi * 2200 * tt) * np.exp(-tt * 160), s["t"] + s["dur"])
    for t, _ in plan["revs"][1:]:
        m = int(0.12 * sr)
        tt = np.arange(m) / sr
        place(0.30 * (np.sin(2 * np.pi * 880 * tt) + 0.5 * np.sin(2 * np.pi * 1320 * tt)) * np.exp(-tt * 30), t)
    m = int(0.25 * sr)
    tt = np.arange(m) / sr
    place(0.9 * lp(rng.standard_normal(m), 0.35) * np.exp(-tt * 28), plan["sticky"]["t"] + 0.12)
    thud = (np.sin(2 * np.pi * (70 + 60 * np.exp(-tt * 30)) * tt) * np.exp(-tt * 14)
            + 0.6 * lp(rng.standard_normal(m), 0.2) * np.exp(-tt * 40))
    place(1.0 * thud, plan["stamp"]["t"])
    m2 = int(0.5 * sr)
    t2 = np.arange(m2) / sr
    wh = rng.standard_normal(m2)
    wh = lp(wh, 0.04) - lp(wh, 0.004)
    place(0.6 * wh * np.sin(np.pi * t2 / 0.5) ** 2 * 3, plan["title"]["drop"])
    for tp in (plan["k3"], plan["pulse"]):
        mm = int(0.4 * sr)
        q = np.arange(mm) / sr
        place(0.3 * np.sin(2 * np.pi * 1760 * q) * np.exp(-q * 9), tp)
    place(0.6 * thud, plan["endcard"]["start"])

    pk = float(np.max(np.abs(out_buf))) or 1.0
    out_buf = out_buf / pk * 10 ** (-20 / 20)
    pcm = (np.stack([out_buf, out_buf], 1) * 32767).astype("<i2")
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
    for token in ("__M_DURATION__", "__M_VO_DURATION__", "/*__M_DATA__*/null", "__M_TITLE__"):
        if token not in html:
            raise BuildError(f"template is missing placeholder {token}")
    inject = {k: v for k, v in plan.items() if k not in ("audio", "voice_source")}
    vo_dur = f"{min(probe_duration(Path(bus['voice'])), plan['duration']):.6f}"
    html = (html.replace("__M_VO_DURATION__", vo_dur)
                .replace("__M_DURATION__", f"{plan['duration']:.6f}")
                .replace("/*__M_DATA__*/null", json.dumps(inject, ensure_ascii=False).replace("</", "<\\/"))
                .replace("__M_TITLE__", (slug or wav.stem).replace("<", "")))
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
    ap = argparse.ArgumentParser(description="Instantiate the style M (blueprint draft) template.")
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
        print(f"built {plan['project']} duration={plan['duration']:.3f}s strokes={len(plan['strokes'])}")
    return 0 if plan["audio"]["bus_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
