#!/usr/bin/env python3
"""Instantiate the style T (dot-matrix printout) template for any narration.

Canonical look: the reference lookdev (read only). The template keeps its system: a cream
continuous-feed sheet with green bars, sprocket strips and perforations rising out of a beige
printer; every narration line prints dot by dot from a hand-coded 5x7 column bitmap, the head
carriage tracking it and alternating direction, with a stepped tractor feed before each line. The
title words are pre-printed as solid dot banners, the reviewer gag prints in crooked red Comic
Shanns and is X-ed out and VOID-stamped when the negation line prints, and the negation and final
lines print double height. The hard-coded S49 lines are replaced by a plan derived from the words.

Usage:
  python3 styles/T/build.py --wav VOICE.wav --words WORDS.json --out DIR
                            [--duration S] [--brand BRAND.json] [--content CONTENT.json]
                            [--slug S49] [--json]

Inputs (script-agnostic):
  --wav       narration WAV (any rate/channels; ffmpeg reads it).
  --words     word timings: a list of {text, start, end} (lookdev format) or {"words": [...]}.
  --duration  narration length in seconds; default: the WAV duration (ffprobe). Every word must
              end inside it. The composition runs past it to hold the end card.
  --brand     optional JSON {logo, name, line, bg, fg, accent} for KAEndCard.mount (defaults: the
              kit's navy card). "name" also prints in the sheet header.
  --content   optional JSON overriding default copy, keys of DEFAULT_CONTENT.
  --slug      optional short id (default: the WAV stem); used in the page title.

How the words drive the printout:
  * Title: the last one or two adjacent content words of the first clause (e.g. "Effective
    Challenge"); each prints as a pre-printed banner at the top of the sheet, and as its own
    large-pitch line (12.4 px dots) when spoken.
  * Verdict: from the first negation (not/no/never/...) through its clause, printed double height.
  * Final: the trailing words that fit FINAL_CHARS, printed double height at a 10.4 px pitch.
  * Body: the remaining words, grouped greedily into lines of at most BODY_CHARS characters,
    breaking at clause ends and pauses longer than PAUSE_BREAK. Each line prints on its first
    word's start, for the span of its words clamped to [0.28, 0.6] s and ending FEED s before the
    next line (the stepped paper feed).
  * Gag: inserted at the line boundary in 40-75 % of the narration with the most free time; it is
    voided on the verdict line's start (else two lines after the gag).
  * Outro: an eased form feed of blank paper and a tear-off fill the time after the last line;
    the shared Keller AI end card (kit/endcard.js) cuts in 0.1 s after the last word and holds 2 s
    with a slow push (no frozen frames).

Output (DIR):
  DIR/project/   a HyperFrames project: index.html, kit/{gsap.min.js,endcard.js},
                 assets/{voice_norm.wav,sfx_bus.wav,mix.wav,fonts/,brand/}
  DIR/plan.json  the plan injected into index.html (also printed with --json)
  DIR/sfx_raw.wav the synthesised printer SFX before the bus
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
END_GAP = 0.1       # KAEndCard.DEFAULT_GAP
END_HOLD = 2.0      # KAEndCard.DEFAULT_HOLD
FONTS = ["LilitaOne.woff2", "ComicShanns.woff2"]

# sheet geometry (template): text starts at TX, the right sprocket strip starts at 920
TX = 84
RIGHT = 910
BODY_PX = 8.7       # body dot pitch (glyph cap height 7 * 8.7 = 61 px)
TITLE_PX = 12.4
VERDICT_PX = 8.2    # double height: 14 * 8.2 = 115 px
FINAL_PX = 10.4     # double height: 146 px
HEADER_PX = 7.4     # pre-printed header (period texture): 52 px
MIN_GLYPH_PX = 44.0  # legibility floor; canvas type is invisible to the DOM preflight, so build checks it
BANNER_CW = 15
BANNER_CHARS = 10
FEED = 0.17         # paper feed before a line (template tweens it at t - 0.15 for 0.14 s)
LINE_MIN, LINE_MAX = 0.28, 0.6
PAUSE_BREAK = 0.6
GAG_DUR = 0.6
GAG_MIN = 0.3

GLYPHS = set(" !#'()*+,-./0123456789:;<=>?ABCDEFGHIJKLMNOPQRSTUVWXYZ%")
NEGATIONS = {"not", "no", "never", "don't", "doesn't", "isn't", "aren't", "wasn't", "weren't", "can't", "won't", "nothing"}
STOP = set("""a an the and or but if then so of in on at to for from by with as into onto over under about
this that these those it its it's i i'm i've me my we our you your he she they them their his her
is are was were be been being am do does did done have has had having will would can could should
may might must shall just very really also too than more most much many some any each every
there here what which who whom whose when where why how all both own same such only
s t let's one called based takes take took get gets got make makes made want wants say says said""".split())


def chars_for(px: float) -> int:
    """Characters that fit between TX and RIGHT at dot pitch px (6 columns per cell, last gap dropped)."""
    return int(((RIGHT - TX) / px + 1) // 6)


BODY_CHARS = chars_for(BODY_PX)       # 16
TITLE_CHARS = chars_for(TITLE_PX)     # 11
VERDICT_CHARS = chars_for(VERDICT_PX)  # 16
FINAL_CHARS = chars_for(FINAL_PX)     # 13

DEFAULT_CONTENT = {
    "job": "JOB 049   10/22/86",
    "file": "BRIEF.LST   PAGE 1",
    "gag": "lgtm!!! 100%",
    "gag_sub": "(trust me bro)",
    "printer_label": "KELLER-9 DOT MATRIX",
    "printer_status": "ON LINE",
    "title": "",          # override the derived title words (space separated)
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


# ----------------------------------------------------------------------------- text helpers

def low(tok: str) -> str:
    return re.sub(r"[^\w'\-%]", "", tok.replace("’", "'")).lower().strip("'")


def is_content(tok: str) -> bool:
    c = low(tok)
    return bool(c) and c not in STOP


def ends_clause(tok: str) -> bool:
    return bool(re.search(r"[.,;:?!—–]['\"’”]?$", tok.strip()))


def printable(tok: str) -> str:
    """Upper-case a word for the 5x7 bitmap: split CamelCase (KellerAI -> KELLER AI), map curly
    quotes and dashes, drop every character the glyph table cannot print."""
    s = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", tok.strip())
    s = s.replace("’", "'").replace("‘", "'").replace("—", "-").replace("–", "-").replace("“", "").replace("”", "")
    s = s.upper()
    return "".join(ch for ch in s if ch in GLYPHS).strip()


def line_text(ws: list[dict]) -> str:
    s = " ".join(p for p in (printable(w["text"]) for w in ws) if p)
    s = re.sub(r"\s+", " ", s).strip()
    return re.sub(r"[,;:]+$", "", s)


def fit_px(text: str, px: float) -> float:
    """Largest pitch <= px at which text fits the sheet."""
    n = max(1, len(text))
    return round(min(px, (RIGHT - TX) / (6 * n - 1)), 3)


def endcard_plan(last_word_end: float) -> dict:
    """Mirror of KAEndCard.plan() with default gap/hold (the template re-checks it in JS)."""
    start = round(last_word_end + END_GAP, 6)
    end = math.ceil((max(start, last_word_end) + END_HOLD) * FPS - 1e-6) / FPS
    return {"start": start, "end": round(end, 6), "hold": END_HOLD}


# ----------------------------------------------------------------------------- plan

def assign_roles(words: list[dict], title_override: str) -> tuple[list[str], list[int]]:
    """Role per word: body | title | verdict | final. Returns (roles, title word indices)."""
    n = len(words)
    roles = ["body"] * n
    # final: trailing words that fit FINAL_CHARS, not crossing an earlier clause end
    fi = n - 1
    while fi > 0 and not ends_clause(words[fi - 1]["text"]) and len(line_text(words[fi - 1:])) <= FINAL_CHARS:
        fi -= 1
    if n >= 3:
        for i in range(fi, n):
            roles[i] = "final"
    else:
        fi = n
    # title: last 1-2 adjacent content words of the first clause (or the override)
    first_end = next((i for i, w in enumerate(words) if ends_clause(w["text"])), n - 1)
    title_idx: list[int] = []
    if title_override:
        want = [printable(t) for t in title_override.split() if printable(t)]
        for i in range(0, fi - len(want) + 1):
            if [printable(w["text"]).rstrip(".,;:!?") for w in words[i:i + len(want)]] == want:
                title_idx = list(range(i, i + len(want)))
                break
        if not title_idx:
            raise BuildError(f"--content title {title_override!r} is not in the words")
    else:
        cont = [i for i in range(0, min(first_end + 1, fi)) if is_content(words[i]["text"])]
        if cont:
            title_idx = cont[-2:] if len(cont) >= 2 and cont[-1] - cont[-2] == 1 else cont[-1:]
    title_idx = [i for i in title_idx if len(printable(words[i]["text"]).rstrip(".,;:!?")) <= TITLE_CHARS]
    for i in title_idx:
        roles[i] = "title"
    # verdict: first negation (outside title/final) through its clause end, capped to VERDICT_CHARS
    neg = next((i for i in range(fi) if roles[i] == "body" and low(words[i]["text"]) in NEGATIONS), None)
    if neg is not None:
        j = neg
        roles[neg] = "verdict"
        while (j + 1 < fi and roles[j + 1] == "body" and not ends_clause(words[j]["text"])
               and len(line_text(words[neg:j + 2])) <= VERDICT_CHARS):
            j += 1
            roles[j] = "verdict"
    return roles, title_idx


def group_lines(words: list[dict], roles: list[str]) -> list[dict]:
    caps = {"body": BODY_CHARS, "verdict": VERDICT_CHARS, "final": 10 ** 6, "title": 0}
    lines: list[list[int]] = []
    cur: list[int] = []
    for i, w in enumerate(words):
        r = roles[i]
        if cur:
            prev = cur[-1]
            brk = (roles[prev] != r or r == "title"
                   or (r == "body" and (ends_clause(words[prev]["text"])
                                        or w["start"] - words[prev]["end"] > PAUSE_BREAK))
                   or len(line_text([words[k] for k in cur] + [w])) > caps[r])
            if brk:
                lines.append(cur)
                cur = []
        cur.append(i)
    if cur:
        lines.append(cur)
    out = []
    for idx in lines:
        ws = [words[k] for k in idx]
        role = roles[idx[0]]
        text = line_text(ws)
        if not text:
            continue
        base = {"body": BODY_PX, "title": TITLE_PX, "verdict": VERDICT_PX, "final": FINAL_PX}[role]
        out.append({"kind": "t", "role": role, "text": text, "t": round(ws[0]["start"], 4),
                    "span": ws[-1]["end"] - ws[0]["start"], "px": fit_px(text, base),
                    "sy": 2 if role in ("verdict", "final") else 1})
    return out


def time_lines(lines: list[dict], card_start: float) -> None:
    for k, L in enumerate(lines):
        nxt = lines[k + 1]["t"] if k + 1 < len(lines) else card_start
        room = max(0.1, nxt - FEED - L["t"])
        L["dur"] = round(min(max(LINE_MIN, min(L.pop("span"), LINE_MAX)), room), 4)


def place_gag(lines: list[dict], lwe: float, text: str, sub: str) -> int | None:
    """Insert the gag at the boundary in 40-75 % of the narration with the most free time."""
    best = None
    for k in range(len(lines) - 1):
        a, b = lines[k], lines[k + 1]
        if not (0.4 * lwe <= b["t"] <= 0.75 * lwe) or a["role"] == "title" or b["role"] == "final":
            continue
        a_end = a["t"] + min(a["dur"], 0.3)       # the line before may be trimmed to 0.3 s
        free = (b["t"] - FEED) - (a_end + FEED)
        if best is None or free > best[0]:
            best = (free, k)
    if best is None or best[0] < GAG_MIN:
        return None
    free, k = best
    a, b = lines[k], lines[k + 1]
    dur = min(GAG_DUR, free)
    gt = round(b["t"] - FEED - dur, 4)
    a["dur"] = round(min(a["dur"], gt - FEED - a["t"]), 4)
    lines.insert(k + 1, {"kind": "gag", "role": "gag", "text": text, "sub": sub, "t": gt, "dur": round(dur, 4)})
    return k + 1


def build_plan(words: list[dict], duration: float, brand: dict, content: dict) -> dict:
    lwe = max(w["end"] for w in words)
    if lwe > duration + 1e-3:
        raise BuildError(f"last word ends at {lwe:.3f} s, after the narration ({duration:.3f} s)")
    unknown = set(content) - set(DEFAULT_CONTENT)
    if unknown:
        raise BuildError(f"--content has unknown keys: {sorted(unknown)}")
    co = dict(DEFAULT_CONTENT)
    co.update({k: str(v) for k, v in content.items()})
    br = dict(DEFAULT_BRAND)
    br.update({k: v for k, v in brand.items() if v is not None})
    ec = endcard_plan(lwe)

    roles, title_idx = assign_roles(words, co["title"].strip())
    lines = group_lines(words, roles)
    if len(lines) < 2:
        raise BuildError(f"narration too short for a printout ({len(words)} words, {len(lines)} line)")
    time_lines(lines, ec["start"])
    gag_at = place_gag(lines, lwe, co["gag"][:16], co["gag_sub"][:22])
    if gag_at is not None:
        verdict = next((L for L in lines[gag_at + 1:] if L["role"] == "verdict"), None)
        after = [L for L in lines[gag_at + 1:] if L["kind"] == "t"]
        void_t = verdict["t"] + 0.04 if verdict else (after[min(1, len(after) - 1)]["t"] + 0.04 if after else None)
    else:
        void_t = None

    # outro: blank form feed then tear, inside [last line end, card start]
    last = lines[-1]
    tail0 = last["t"] + last["dur"] + 0.04
    tail = max(0.2, ec["start"] - tail0)
    feed_dur = round(min(0.44, tail * 0.5), 4)
    tear_dur = round(min(0.46, tail - feed_dur), 4)
    feed_t = round(ec["start"] - feed_dur - tear_dur, 4)
    tear_t = round(feed_t + feed_dur, 4)

    title_words = [printable(words[i]["text"]).rstrip(".,;:!?") for i in title_idx]
    # banner width is (6n - 1) * cw; shrink the cell for long words so the banner stays on the sheet
    banners = [{"text": t, "cw": round(min(BANNER_CW, 900 / (6 * len(t) - 1)), 3)} for t in title_words if t]
    name = printable(str(br.get("name") or "KELLER AI"))[:12]
    header = [f"*** {name} ***", printable(co["job"])[:18], printable(co["file"])[:18]]

    glyph = []
    for L in lines:
        if L["kind"] == "t":
            glyph.append(7 * L["px"] * L["sy"])
    if min(glyph) < MIN_GLYPH_PX:
        raise BuildError(f"a printed line would render below {MIN_GLYPH_PX} px ({min(glyph):.1f} px)")

    return {
        "items": lines,
        "header": header,
        "headerPx": HEADER_PX,
        "banners": banners,
        "title": " ".join(title_words),
        "tx": TX,
        "voidT": round(void_t, 4) if void_t is not None else None,
        "feedT": feed_t, "feedDur": feed_dur, "tearT": tear_t, "tearDur": tear_dur,
        "printerLabel": co["printer_label"][:20],
        "printerStatus": co["printer_status"][:8],
        "minGlyphPx": round(min(glyph), 2),
        "firstWordStart": round(words[0]["start"], 4),
        "lastWordEnd": round(lwe, 4),
        "narration": round(duration, 6),
        "endcard": ec,
        "duration": ec["end"],
        "brand": br,
    }


# ----------------------------------------------------------------------------- sfx (port of r7-S49-T sfx_gen.py)

def synth_sfx(plan: dict, out: Path, sr: int = 48000) -> Path:
    """Tractor-feed clicks and gated-noise pin hammering per line, jam clunks on the gag, X-out
    strokes and a VOID thunk, a form-feed ratchet with a paper whoosh, the tear lift and a soft
    clunk on the end-card cut. Seeded numpy, no samples."""
    import numpy as np

    N = int(round(plan["duration"] * sr))
    buf = np.zeros(N)
    rng = np.random.default_rng(7)

    def add(t, s):
        i = max(0, int(t * sr))
        if i >= N:
            return
        s = s[: N - i]
        buf[i:i + len(s)] += s

    def burst(d, v, decay=30):
        n = int(d * sr)
        s = rng.uniform(-1, 1, n)
        s = np.convolve(s, np.ones(4) / 4, "same")
        return v * s * np.exp(-np.arange(n) / sr * decay)

    def line(t, d):
        for k in range(4):
            add(t - .12 + k * .03, burst(.02, .5, 150))
        n = int(d * sr)
        tt = np.arange(n) / sr
        s = rng.uniform(-1, 1, n) * (np.sin(2 * np.pi * 1180 * tt) > 0.2) * (0.6 + 0.4 * np.sign(np.sin(2 * np.pi * 46 * tt)))
        add(t, .35 * s * np.minimum(1, tt / .01) * np.minimum(1, (d - tt) / .02))

    def swell(t, d, v, decay):
        n = int(d * sr)
        add(t, burst(d, v, decay)[:n] * np.sin(np.linspace(0, np.pi, n)))

    for L in plan["items"]:
        line(max(0.0, L["t"]), max(0.05, L["dur"]))
        if L["kind"] == "gag":
            add(L["t"], burst(.1, 1, 25))
            add(L["t"] + .15, burst(.1, .8, 25))
    if plan["voidT"] is not None:
        vt = plan["voidT"]
        add(vt, burst(.06, .7, 60))
        add(vt + .12, burst(.06, .7, 60))
        add(vt + .26, burst(.14, 1.2, 30))
    ft, fd = plan["feedT"], plan["feedDur"]
    for k in range(max(1, int(fd / .04))):
        add(ft + k * .04, burst(.02, .5, 150))
    swell(ft, max(.05, fd), .5, 5)
    swell(plan["tearT"], max(.05, plan["tearDur"]), .5, 6)
    add(plan["endcard"]["start"], burst(.12, .6, 40))

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
    for token in ("__T_DURATION__", "__T_VO_DURATION__", "/*__T_DATA__*/null", "__T_TITLE__"):
        if token not in html:
            raise BuildError(f"template is missing placeholder {token}")
    inject = {k: v for k, v in plan.items() if k not in ("audio", "voice_source")}
    vo_dur = f"{min(probe_duration(Path(bus['voice'])), plan['duration']):.6f}"
    title = re.sub(r"[<>&]", "", slug or wav.stem)
    html = (html.replace("__T_VO_DURATION__", vo_dur)
                .replace("__T_DURATION__", f"{plan['duration']:.6f}")
                .replace("/*__T_DATA__*/null", json.dumps(inject, ensure_ascii=False).replace("</", "<\\/"))
                .replace("__T_TITLE__", title))
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
    ap = argparse.ArgumentParser(description="Instantiate the style T (dot-matrix printout) template.")
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
        print(f"built {plan['project']} duration={plan['duration']:.3f}s lines={len(plan['items'])}")
    return 0 if plan["audio"]["bus_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
