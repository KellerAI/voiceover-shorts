#!/usr/bin/env python3
"""Instantiate the style Q (handheld LCD) template for any narration.

Canonical look: the reference lookdev (read only). The template keeps its system: a 1989-style
dot-matrix handheld whose 112x192 LCD is drawn on a canvas at an exact 8x integer scale inside a
cream console shell (navy screen plate, LED, D-pad, A/B buttons, SELECT/START pills, the official
K tile and wordmark). The screen uses a strict 4-shade palette with Bayer dithering, a 5x7 bitmap
font, palette-flip inversions, 5-frame band wipes and seek-safe LCD ghosting. Its scenes keep their
devices: a boot screen (K tile + brand), a SELECT BRIEF menu whose cursor lands on the title, the
title slamming in, an RPG battle against a "wild" enemy, the check scene (model A, model B dropping
in, a magnifier over B's paper, the sloppy "LGTM!!" verdict bubble, B's label glitching to MODEL A),
a YES/NO verdict menu that jumps to NO, the slam-word screen with X-eyed robots, and an ITEM GET!
bank. The hard-coded S49 cue times and copy are replaced by a plan derived from the words file.

Usage:
  python3 styles/Q/build.py --wav VOICE.wav --words WORDS.json --out DIR
                            [--duration S] [--brand BRAND.json] [--content CONTENT.json]
                            [--slug S49] [--json]

Inputs (script-agnostic):
  --wav       narration WAV (any rate/channels; ffmpeg reads it).
  --words     word timings: a list of {text, start, end} (lookdev format) or {"words": [...]}.
  --duration  narration length in seconds; default: the WAV duration (ffprobe). Every word must
              end inside it. The composition runs past it to hold the end card.
  --brand     optional JSON {logo, name, line, bg, fg, accent} for KAEndCard.mount (defaults: the
              kit's navy card). "name" is also drawn in the bitmap font on the boot screen and the
              cartridge label.
  --content   optional JSON overriding default copy, keys of DEFAULT_CONTENT.
  --slug      optional short id (default: the WAV stem); used in the page title.

How the words drive the screens:
  * Chapters: the words split into five groups at clause ends ([.,;:?!]), choosing the most even
    split that meets each chapter's minimum (CH_MIN); when there are too few clause ends the
    largest pauses are used. Chapters 2, 3 and 5 cut in 0.05 s before their first word.
  * Chapter 1: boot (the K tile lands; the loading bar fills until the first word containing the
    brand name), the menu (0.72 s before the title; the cursor steps at most every 0.4 s and lands
    on the title on its first word), and the title slam on its last word. The title is the last
    two adjacent content words of the chapter.
  * Chapter 2 (battle): the enemy is the last word of the clause through its second content word;
    "A WILD <ENEMY> / APPEARED!" types on, then "<ENEMY>:" / the rest of the clause on its word.
  * Chapter 3 (check): B drops 0.5 s in, the magnifier comes in 1.1 s in, the sloppy verdict bubble
    lands on the chapter's last word.
  * Chapter 4 (verdict): the slam word is the first negation, else the longest content word. B's
    label glitches on the word nearest 65 % of the way to the verdict menu, which cuts in 0.64 s
    before the slam word; the cursor jumps to NO 0.24 s before it; the slam word lands on its word,
    the words after it on theirs.
  * Chapter 5 (bank): ITEM GET! on its first content word, a $ burst on its last content word.
  * The shared Keller AI end card (kit/endcard.js) cuts in 0.1 s after the last word and holds 2 s,
    with the title and a blinking PRESS START on the card (no frozen frames, no empty lower third).

Output (DIR):
  DIR/project/   a HyperFrames project: index.html, kit/{gsap.min.js,endcard.js},
                 assets/{voice_norm.wav,sfx_bus.wav,mix.wav,fonts/,brand/}
  DIR/plan.json  the plan injected into index.html (also printed with --json)
  DIR/sfx_raw.wav the synthesised LCD SFX before the bus
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
CUT_LEAD = 0.05     # a chapter cuts in this long before its first word
N_CHAPTERS = 5
CH_MIN = [2.4, 1.6, 2.0, 1.8, 1.2]   # seconds per chapter: boot+menu+title, battle, check, verdict, bank
FONTS = ["LilitaOne.woff2"]
LINE1 = 18          # 1x bitmap chars across the 112 px screen
LINE2 = 9           # 2x bitmap chars

# glyphs present in the template's 5x7 bitmap table
GLYPHS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.:!?-+%'/>,=)#$ ")

STOP = set("""a an the and or but if then so of in on at to for from by with as into onto over under about
this that these those it its it's i i'm i've me my we our you your he she they them their his her
is are was were be been being am do does did done have has had having will would can could should
may might must shall just very really also too than more most much many some any each every
there here what which who whom whose when where why how all both own same such only
s t don't doesn't isn't aren't wasn't weren't let's one
called based takes take took get gets got make makes made want wants say says said""".split())
STOP -= {"not", "no", "never"}
NEGATIONS = {"not", "no", "never", "don't", "doesn't", "isn't", "aren't", "wasn't", "weren't", "can't", "won't"}
LEADING_BACKUP = {"a", "an", "the", "one", "not", "no", "this", "its", "our", "your", "their", "my"}
BANK_WORDS = ("bank", "banks", "banking", "banker", "bankers", "finance", "financial", "money", "loan", "loans")

DEFAULT_CONTENT = {
    "plate_text": "",                 # default "<BRAND NAME> · DOT MATRIX"
    "wordmark": "Keller AI",
    "boot_line": "A BRIEF",
    "menu": ["MEMOS", "AUDITS", "MODELS"],
    "menu_head": "SELECT BRIEF",
    "title_head": "NEW BRIEF",
    "title_label": "BRIEF",
    "stamp": ["LGTM!!", "100% OK"],
    "model_a": "MODEL A",
    "model_b": "MODEL B",
    "verdict_q": "",                  # default "<first content word after the slam>?"
    "item_get": "ITEM GET!",
    "bank_label": "",                 # default BANK for money words, else the last content word
    "press": "PRESS START",
}

DEFAULT_BRAND = {"name": "KELLER AI"}


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


def bitmap(s: str) -> str:
    """Upper-case s and keep only characters the 5x7 bitmap table can draw."""
    s = str(s).upper().replace("’", "'").replace("·", "-").replace("—", "-").replace("–", "-")
    s = "".join(ch for ch in s if ch in GLYPHS)
    return re.sub(r"\s+", " ", s).strip()


def trunc(s: str, n: int) -> str:
    s = s.strip()
    return s if len(s) <= n else s[: max(1, n - 1)].rstrip() + "-"


def words_bitmap(ws: list[dict]) -> str:
    return bitmap(" ".join(strip_punct(w["text"]) for w in ws))


def wrap(s: str, n: int, lines: int) -> list[str]:
    """Greedy word wrap of s into at most `lines` lines of <= n chars; over-long words are split."""
    toks = []
    for t in s.split():
        while len(t) > n:
            toks.append(t[:n])
            t = t[n:]
        if t:
            toks.append(t)
    out: list[str] = []
    for t in toks:
        if out and len(out[-1]) + 1 + len(t) <= n:
            out[-1] += " " + t
        else:
            out.append(t)
    if len(out) > lines:
        out = out[:lines]
        out[-1] = trunc(out[-1] + " X", n) if len(out[-1]) >= n else out[-1]
    return out


def balanced_split(s: str, n: int) -> list[str]:
    """One line if it fits n, else two lines split near the middle (a word longer than n is cut)."""
    if len(s) <= n:
        return [s]
    if " " not in s:
        h = min(n, math.ceil(len(s) / 2) + 1)  # the canonical INDEPEN / DENCE split
        return [s[:h][:n], s[h:][:n]]
    parts = s.split()
    best = None
    for k in range(1, len(parts)):
        a, b = " ".join(parts[:k]), " ".join(parts[k:])
        sc = max(len(a), len(b))
        if best is None or sc < best[0]:
            best = (sc, a, b)
    if best is None:
        raise ValueError(f"balanced_split: cannot split {s!r} into two lines (fewer than two words)")
    return [trunc(best[1], n), trunc(best[2], n)]


def from_first_content(ws: list[dict]) -> list[dict]:
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
        cuts = [0.0] + [cut_at(i) for i in bounds] + [ec_start]
        return all(b - a >= m - 1e-9 for (a, b), m in zip(zip(cuts, cuts[1:]), CH_MIN))

    def cost(bounds: tuple[int, ...], punct: set[int]) -> float:
        cuts = [0.0] + [cut_at(i) for i in bounds] + [ec_start]
        durs = [(b - a) / m for (a, b), m in zip(zip(cuts, cuts[1:]), CH_MIN)]
        mean = sum(durs) / len(durs)
        return sum((d - mean) ** 2 for d in durs) + 4.0 * sum(1 for i in bounds if i not in punct)

    punct = {i + 1 for i in range(n - 1) if ends_clause(words[i]["text"])}
    gaps = sorted((i + 1 for i in range(n - 1)), key=lambda i: words[i]["start"] - words[i - 1]["end"], reverse=True)
    for pool in (sorted(punct), sorted(punct | set(gaps[:12])), list(range(1, n))):
        pool = [i for i in pool if cut_at(i) >= CH_MIN[0] - 1e-9]
        if len(pool) > 28:
            keep = sorted(pool, key=lambda i: (i not in punct, -(words[i]["start"] - words[i - 1]["end"])))[:28]
            pool = sorted(keep)
        best = None
        for combo in itertools.combinations(pool, N_CHAPTERS - 1):
            if ok(combo):
                c = cost(combo, punct)
                if best is None or c < best[0]:
                    best = (c, combo)
        if best:
            return list(best[1])
    raise BuildError(f"narration too short for five chapters of at least {CH_MIN} s "
                     f"(end card at {ec_start:.2f} s, {n} words)")


def need(cond: bool, what: str) -> None:
    if not cond:
        raise BuildError(f"narration too short for the {what} cues")


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
    for key, n in (("menu", 3), ("stamp", 2)):
        if not isinstance(co[key], list) or len(co[key]) != n:
            raise BuildError(f"--content {key} must be a list of {n}")
    br = dict(DEFAULT_BRAND)
    br.update({k: v for k, v in brand.items() if v is not None})
    brand_name = trunc(bitmap(br.get("name") or "KELLER AI"), LINE2) or "KELLER AI"
    ec = endcard_plan(lwe)

    bounds = split_chapters(words, ec["start"])
    edges = [0] + bounds + [len(words)]
    G = [words[a:b] for a, b in zip(edges, edges[1:])]
    cuts = [0.0] + [round(words[i]["start"] - CUT_LEAD, 4) for i in bounds]

    # ---- chapter 1: boot, menu, title
    g1 = G[0]
    cont1 = [i for i, w in enumerate(g1) if is_content(w["text"])]
    if cont1:
        ti = cont1[-2:] if len(cont1) >= 2 and cont1[-1] - cont1[-2] <= 2 else cont1[-1:]
        title_ws = g1[ti[0]:ti[-1] + 1]
    else:
        title_ws = g1[-1:]
    title_text = words_bitmap(title_ws) or "BRIEF"
    toks = title_text.split()
    if len(toks) == 2 and all(len(t) <= LINE1 for t in toks):
        title_lines = toks
    else:
        title_lines = wrap(title_text, LINE1, 2)
    sel_t = title_ws[0]["start"]
    title_t = title_ws[-1]["start"] if len(title_ws) >= 2 else sel_t + 0.5
    if title_t < sel_t + 0.3:
        title_t = sel_t + 0.5
    title_t = min(title_t, cuts[1] - 0.6)
    menu_t = max(1.0, sel_t - 0.72)
    need(sel_t - menu_t >= 0.3 and title_t - sel_t >= 0.25, "menu/title")
    span = sel_t - menu_t
    n_steps = max(3, math.ceil(span / 0.4))
    steps = [round(menu_t + span * i / n_steps, 4) for i in range(1, n_steps + 1)]
    brand_tok = low((br.get("name") or "keller").split()[0])
    load_w = next((w for w in g1 if brand_tok and brand_tok in low(w["text"]) and w["start"] < menu_t - 0.3), None)
    load_t = load_w["start"] if load_w else 0.65 * menu_t
    load_t = min(max(load_t, 0.75), menu_t - 0.25)

    # ---- chapter 2: battle
    g2 = from_first_content(G[1])
    ci = [i for i, w in enumerate(g2) if is_content(w["text"])]
    cut2 = ci[1] + 1 if len(ci) >= 2 else (ci[0] + 1 if ci else len(g2))
    head2, sub2 = g2[:cut2], g2[cut2:]
    enemy = trunc(words_bitmap(head2[-1:]) or "HABIT", LINE2)
    l1 = f"A WILD {enemy}" if len(f"A WILD {enemy}") <= LINE1 else enemy
    sub_txt = words_bitmap(sub2)
    battle_t = cuts[1]
    type_end = battle_t + 0.1 + (len(l1) + len("APPEARED!")) / 30.0
    sub_t = sub2[0]["start"] if sub2 else type_end + 0.4
    sub_t = min(max(sub_t, type_end + 0.3), cuts[2] - 0.5)
    need(sub_t > battle_t + 0.6, "battle")
    battle = {"t": battle_t, "enemy": enemy, "lv": "LV99", "l1": l1, "l2": "APPEARED!",
              "subT": round(sub_t, 4), "s1": trunc(f"{enemy}:", LINE1),
              "s2": trunc(sub_txt, LINE1) if sub_txt else "WHAT WILL YOU DO?"}

    # ---- chapter 4 first (the check scene runs into it): slam word, verdict menu, glitch
    g4 = G[3]
    neg = [i for i, w in enumerate(g4) if low(w["text"]) in NEGATIONS]
    if neg:
        si = neg[0]
    else:
        cand = [i for i, w in enumerate(g4) if is_content(w["text"])] or list(range(len(g4)))
        si = max(cand, key=lambda i: len(clean(g4[i]["text"])))
    slam_w, post = g4[si], g4[si + 1:]
    slam_t = slam_w["start"]
    slam_txt = trunc(words_bitmap([slam_w]) or "NOT", LINE1)
    post_txt = words_bitmap(post)
    post_lines = balanced_split(post_txt, LINE2) if post_txt else []
    post_t = post[0]["start"] if post else slam_t + 0.14
    post_t = max(post_t, slam_t + 0.12)
    post_content = next((w for w in post if is_content(w["text"])), None)
    q_word = words_bitmap([post_content]) if post_content else slam_txt
    verdict_q = trunc(bitmap(co["verdict_q"]) or f"{q_word}?", LINE1 - 1)
    verdict_t = slam_t - 0.64
    jump_t = slam_t - 0.24

    # ---- chapter 3: check
    g3 = G[2]
    check_t = cuts[2]
    drop_t = check_t + 0.5
    stamp_t = max(g3[-1]["start"], check_t + 1.4)
    mag_t = max(min(check_t + 1.1, stamp_t - 0.6), drop_t + 0.2)
    k4 = cuts[3]
    target = k4 + 0.65 * (verdict_t - k4)
    glitch_t = min((w["start"] for w in g4), key=lambda s: abs(s - target)) if g4 else target
    glitch_t = max(glitch_t, stamp_t + 0.6)
    need(mag_t < stamp_t - 0.3 and glitch_t <= verdict_t - 0.3 and verdict_t >= check_t + 2.0, "check/verdict")
    check = {"t": check_t, "dropT": round(drop_t, 4), "magT": round(mag_t, 4), "stampT": round(stamp_t, 4),
             "glitchT": round(glitch_t, 4),
             "stamp": [trunc(bitmap(s), 8) or "LGTM!!" for s in co["stamp"]],
             "a": trunc(bitmap(co["model_a"]), 7) or "MODEL A", "b": trunc(bitmap(co["model_b"]), 7) or "MODEL B"}
    verdict = {"t": round(verdict_t, 4), "q": verdict_q, "jumpT": round(jump_t, 4)}
    nots = {"t": round(slam_t, 4), "text": slam_txt, "post": post_lines, "postT": round(post_t, 4)}

    # ---- chapter 5: bank
    g5 = G[4]
    bank_t = cuts[4]
    ci5 = [w for w in g5 if is_content(w["text"])]
    item_t = (ci5[0]["start"] if ci5 else bank_t + 0.3)
    item_t = min(max(item_t, bank_t + 0.25), ec["start"] - 0.5)
    coin_t = ci5[-1]["start"] if ci5 else item_t + 0.4
    coin_t = min(max(coin_t, item_t + 0.3), ec["start"] - 0.2)
    need(item_t > bank_t and coin_t > item_t, "bank")
    money = any(low(w["text"]) in BANK_WORDS for w in g5)
    label = bitmap(co["bank_label"]) or ("BANK" if money else (words_bitmap(ci5[-1:]) or "BANK"))
    bank = {"t": bank_t, "itemT": round(item_t, 4), "coinT": round(coin_t, 4), "label": trunc(label, 16),
            "item": trunc(bitmap(co["item_get"]), LINE2) or "ITEM GET!"}

    plate_text = str(co["plate_text"] or f"{(br.get('name') or 'KELLER AI').upper()} · DOT MATRIX")
    return {
        "cuts": cuts,
        "chapters": [{"first_word": words[a]["text"], "start": round(words[a]["start"], 4)} for a in edges[:-1]],
        "boot": {"loadT": round(load_t, 4), "line": trunc(bitmap(co["boot_line"]), LINE2)},
        "menu": {"t": round(menu_t, 4), "steps": steps, "items": [trunc(bitmap(s), LINE2) for s in co["menu"]],
                 "head": trunc(bitmap(co["menu_head"]), LINE1)},
        "title": {"lines": title_lines, "t": round(title_t, 4), "selT": round(sel_t, 4),
                  "head": trunc(bitmap(co["title_head"]), LINE1), "label": trunc(bitmap(co["title_label"]), LINE1)},
        "battle": battle, "check": check, "verdict": verdict, "nots": nots, "bank": bank,
        "press": trunc(bitmap(co["press"]), LINE1),
        "brandName": brand_name,
        "plateText": trunc(plate_text, 30),
        "wordmark": trunc(str(co["wordmark"]), 16),
        "firstWordStart": round(words[0]["start"], 4),
        "lastWordEnd": round(lwe, 4),
        "narration": round(duration, 6),
        "endcard": ec,
        "duration": ec["end"],
        "brand": br,
    }


# ----------------------------------------------------------------------------- sfx (port of r7-S49-Q sfx_gen.py)

def synth_sfx(plan: dict, out: Path, sr: int = 48000) -> Path:
    """Handheld-LCD SFX, re-timed to the plan: pulse waves (12.5/25/50 % duty) and stepped noise
    only. Power click, boot chime, wipes, cursor blips, select, title slam, battle typing, B's drop,
    the stamp thud, the glitch, the NO buzzer and slam, ITEM GET, the coin and an end chime."""
    import numpy as np

    N = int(round(plan["duration"] * sr))
    out_buf = np.zeros(N)
    rng = np.random.default_rng(7)

    def sq(f, d, v=1.0, duty=0.5, slide=0.0):
        t = np.arange(int(d * sr)) / sr
        fr = f * (1 + slide * t / d)
        ph = np.cumsum(fr) / sr
        return v * np.where((ph % 1) < duty, 1.0, -1.0) * np.exp(-t * 3 / d)

    def noise(d, v=1.0, hold=8):
        n = int(d * sr)
        s = np.repeat(np.sign(rng.uniform(-1, 1, n // hold + 1)), hold)[:n]
        return v * s * np.exp(-np.arange(n) / sr * 6 / d)

    def add(t, s):
        i = int(t * sr)
        if i < 0 or i >= N:
            return
        s = s[: N - i]
        out_buf[i:i + len(s)] += s

    b, m, ti, ba, ch = plan["boot"], plan["menu"], plan["title"], plan["battle"], plan["check"]
    add(0.12, noise(0.06, 0.5))
    for i, f in enumerate([784, 1175, 1568]):
        add(b["loadT"] + i * 0.06, sq(f, 0.25 if i == 2 else 0.07, 0.6, 0.25))
    add(m["t"], sq(300, 0.18, 0.4, 0.125, 2.5))
    for tt in m["steps"][:-1]:
        add(tt, sq(1400, 0.04, 0.4, 0.125))
    add(ti["selT"], sq(1046, 0.05, 0.5, 0.25))
    add(ti["selT"] + 0.05, sq(1568, 0.12, 0.5, 0.25))
    add(ti["t"], noise(0.4, 1, 24))
    add(ti["t"], sq(90, 0.35, 0.8, 0.5, -0.5))
    add(ba["t"], sq(300, 0.2, 0.5, 0.125, 2.5))
    add(ba["t"] + 0.6, sq(200, 0.3, 0.6, 0.5, -0.5))
    for x in np.arange(ba["t"] + 0.1, ba["t"] + 0.1 + (len(ba["l1"]) + len(ba["l2"])) / 30.0, 0.033):
        add(x, sq(1800, 0.012, 0.2, 0.125))
    for x in np.arange(ba["subT"], ba["subT"] + len(ba["s2"]) / 30.0, 0.033):
        add(x, sq(1800, 0.012, 0.2, 0.125))
    add(ch["t"], sq(300, 0.2, 0.5, 0.125, 2.5))
    add(ch["dropT"], sq(600, 0.2, 0.5, 0.25, -0.7))
    add(ch["dropT"] + 0.3, noise(0.08, 0.6, 30))
    add(ch["magT"], sq(880, 0.06, 0.4, 0.25))
    add(ch["stampT"], noise(0.25, 0.9, 20))
    add(ch["stampT"], sq(70, 0.25, 0.8))
    add(ch["glitchT"], sq(400, 0.15, 0.4, 0.125, 1.5))
    add(ch["glitchT"] + 0.07, sq(400, 0.15, 0.4, 0.125, 1.5))
    v, nt, bk = plan["verdict"], plan["nots"], plan["bank"]
    add(v["t"], sq(1400, 0.04, 0.4, 0.125))
    add(v["jumpT"], sq(700, 0.06, 0.5, 0.25))
    add(nt["t"], sq(110, 0.4, 0.9, 0.5))
    add(nt["t"], sq(116, 0.4, 0.6, 0.25))
    add(nt["postT"], noise(0.3, 0.9, 24))
    add(bk["t"], sq(300, 0.2, 0.5, 0.125, 2.5))
    for i, f in enumerate([523, 659, 784, 1047]):
        add(bk["itemT"] + i * 0.06, sq(f, 0.1, 0.55, 0.5))
    add(bk["coinT"], sq(988, 0.06, 0.6, 0.5))
    add(bk["coinT"] + 0.06, sq(1319, 0.3, 0.6, 0.5))
    for i, f in enumerate([392, 523, 659, 784]):
        add(plan["endcard"]["start"] + i * 0.07, sq(f, 0.25, 0.4, 0.25))

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

PLACEHOLDERS = ("__Q_DURATION__", "__Q_VO_DURATION__", "/*__Q_DATA__*/null", "__Q_TITLE__",
                "__Q_PLATE__", "__Q_WORDMARK__")


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


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
    for token in PLACEHOLDERS:
        if token not in html:
            raise BuildError(f"template is missing placeholder {token}")
    inject = {k: v for k, v in plan.items() if k not in ("audio", "voice_source")}
    vo_dur = f"{min(probe_duration(Path(bus['voice'])), plan['duration']):.6f}"
    html = (html.replace("__Q_VO_DURATION__", vo_dur)
                .replace("__Q_DURATION__", f"{plan['duration']:.6f}")
                .replace("/*__Q_DATA__*/null", json.dumps(inject, ensure_ascii=False).replace("</", "<\\/"))
                .replace("__Q_TITLE__", _esc(slug or wav.stem))
                .replace("__Q_PLATE__", _esc(plan["plateText"]))
                .replace("__Q_WORDMARK__", _esc(plan["wordmark"])))
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
    ap = argparse.ArgumentParser(description="Instantiate the style Q (handheld LCD) template.")
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
