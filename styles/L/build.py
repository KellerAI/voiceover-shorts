#!/usr/bin/env python3
"""Instantiate the style L (thermal receipt) template for any narration.

Canonical look: the reference lookdev (read only). The template keeps its system: one cream
receipt fed from the slot of a graphite printer under a slow camera, a pre-fed header (K tile,
wordmark, "*** BRIEF RECEIPT ***", gold rule), one itemised row per spoken phrase printed with a
stepped feed and a print-head wipe, double-height title rows, a crooked red "vendor claim" gag that
is struck and VOID-stamped, a TEAR HERE rule, an inverse TOTAL bar in brand gold on navy, a
footer, and the tear-off (tug, lift, tilt, zigzag edge, stub left in the slot). The hard-coded S49
times and copy are replaced by a plan derived from the words file.

Usage:
  python3 styles/L/build.py --wav VOICE.wav --words WORDS.json --out DIR
                            [--duration S] [--brand BRAND.json] [--content CONTENT.json]
                            [--slug S49] [--json]

Inputs (script-agnostic):
  --wav       narration WAV (any rate/channels; ffmpeg reads it).
  --words     word timings: a list of {text, start, end} (lookdev format) or {"words": [...]}.
  --duration  narration length in seconds; default: the WAV duration (ffprobe). Every word must
              end inside it. The composition runs past it to hold the end card.
  --brand     optional JSON {name, line, bg, fg, accent} for KAEndCard.mount (kit defaults);
              "name" is also the receipt header wordmark (default "Keller AI").
  --content   optional JSON overriding derived or default copy, keys of DEFAULT_CONTENT.
  --slug      optional short id printed on the printer label (default: the WAV stem).

How the words drive the receipt:
  * Words are chunked into phrases (break on punctuation, a pause >= 0.25 s, the second content
    word, or 1.8 s). Each phrase prints one row "LEFT ..... RIGHT" at its first word (RIGHT is
    its last content word, LEFT the content words before it, or "ITEM nn").
  * The first sentence that ends inside the first 45 % of the narration prints its last one or
    two content words as double-height title rows, each on its own spoken word, then a rule.
  * The gag (big crooked claim in Virgil red) lands on the phrase in the 45-70 % window with the
    longest content word; it is struck, dimmed and stamped at the next phrase (>= 1.2 s later).
  * The final clause (last phrase, merged with the one before when it has a single content word
    and no sentence break) prints as the TOTAL bar on its first word, after a TEAR HERE rule.
  * The receipt tears off 0.8 s before the shared Keller AI end card (kit/endcard.js), which cuts
    in 0.1 s after the last word and holds 2 s with a slow push (no frozen frames).

Output (DIR):
  DIR/project/   a HyperFrames project: index.html, kit/{gsap.min.js,endcard.js},
                 assets/{voice_norm.wav,sfx_bus.wav,mix.wav,fonts/,brand/}
  DIR/plan.json  the receipt plan injected into index.html (also printed with --json)
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
END_GAP = 0.1      # KAEndCard.DEFAULT_GAP
END_HOLD = 2.0     # KAEndCard.DEFAULT_HOLD
FONTS = ["LilitaOne.woff2", "ComicShanns.woff2", "Virgil.woff2"]
# receipt geometry (must match template/index.html): 806 px text column, 56 px Comic Shanns (0.55 em)
TEXT_W = 806
BODY_PX = 56
ADV_EM = 0.55
COLS = int(TEXT_W // (BODY_PX * ADV_EM))   # 26
FTR_COLS = int(TEXT_W // (48 * ADV_EM))   # footer row at 48 px
TOTAL_COLS = 25    # big TOTAL line fits 25 chars at its 56 px floor
MIN_STEP = 0.42    # a feed (0.24 s) plus most of the print-head wipe before the next row
GAG_HOLD = 0.6     # the gag settles before the next row feeds
TEAR_LEAD = 0.8    # tear-off starts this long before the end card

STOP = set("""a an the and or but if then so of in on at to for from by with as into onto over under about
this that these those it its it's i i'm i've me my we our you your he she they them their his her
is are was were be been being am do does did done have has had having will would can could should
may might must shall just very really also too than more most much many some any each every
there here what which who whom whose when where why how all both own same such only
s t don't doesn't isn't aren't wasn't weren't let's one
called based takes take took get gets got make makes made want wants say says said""".split())
STOP -= {"not", "no", "never"}  # negations are content

DEFAULT_CONTENT = {
    "title": "*** BRIEF RECEIPT ***",   # header line under the wordmark
    "gag": [],                           # [big line 1, big line 2, small]; default 100% / HERO!! / trust me bro
    "stamp": "VOID",
    "total_label": "TOTAL",
    "total_due": "DUE",
    "total": "",                         # default: the final clause's content words
    "footer": "THANK YOU * KEEP THIS RECEIPT",
    "rows": [],                          # optional explicit [left, right] per phrase row, in order
}


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


def row(left: str, right: str, n: int = COLS) -> str:
    """'LEFT ..... RIGHT' padded to exactly n columns; the left side is shortened first."""
    right = fit(right, n - 6)
    left = fit(left, max(1, n - len(right) - 4))
    dots = n - len(left) - len(right) - 2
    return f"{left} {'.' * dots} {right}"


def endcard_plan(last_word_end: float) -> dict:
    """Mirror of KAEndCard.plan() with default gap/hold (the template re-checks it in JS)."""
    start = round(last_word_end + END_GAP, 6)
    end = math.ceil((max(start, last_word_end) + END_HOLD) * FPS - 1e-6) / FPS
    return {"start": start, "end": round(end, 6), "hold": END_HOLD}


def phrases_of(words: list[dict]) -> list[list[int]]:
    groups, cur, n_content = [], [], 0
    for i, w in enumerate(words):
        cur.append(i)
        n_content += is_content(w["text"])
        nxt = words[i + 1] if i + 1 < len(words) else None
        brk = (nxt is None or ends_clause(w["text"]) or nxt["start"] - w["end"] >= 0.25
               or n_content >= 2 or w["end"] - words[cur[0]]["start"] >= 1.8)
        if brk:
            # a trailing run of stop words joins the next phrase instead (unless it ends a clause)
            groups.append(cur)
            cur, n_content = [], 0
    return groups


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
    ec = endcard_plan(lwe)
    if ec["start"] < 4.0:
        raise BuildError(f"narration too short for a receipt (last word ends at {lwe:.2f} s)")
    tear = round(ec["start"] - TEAR_LEAD, 4)

    groups = phrases_of(words)
    P = []
    for g in groups:
        ws = [words[i] for i in g]
        P.append({"idx": g, "start": ws[0]["start"], "end": ws[-1]["end"], "words": ws,
                  "content": [w for w in ws if is_content(w["text"])],
                  "sentence_end": ends_sentence(ws[-1]["text"])})

    # total clause: last phrase (+ the one before it when it is thin and in the same sentence)
    tot_first = len(P) - 1
    if tot_first > 0 and len(P[tot_first]["content"]) < 2 and not P[tot_first - 1]["sentence_end"]:
        tot_first -= 1
    tot_words = [w for p in P[tot_first:] for w in p["content"]] or P[-1]["words"]
    total_t = tot_words[0]["start"]

    # title: the last one or two content words of the first sentence that ends inside the first 45 %
    title_ids, rule_t = [], None
    s0 = 0
    for i, w in enumerate(words):
        if not ends_sentence(w["text"]):
            continue
        if w["end"] > 0.45 * lwe or w["start"] >= total_t:
            break
        cids = [j for j in range(s0, i + 1) if is_content(words[j]["text"])]
        if cids:
            title_ids = cids[-2:]
            nxt = [p["start"] for p in P if p["start"] > w["end"] - 1e-6]
            rule_t = nxt[0] if nxt else None
            break
        s0 = i + 1
    title_set = {id(words[j]) for j in title_ids}

    # gag: longest content word among phrases starting in the 45-70 % window
    gag_p, gag_w = None, None
    for k, p in enumerate(P[:tot_first]):
        if k == 0 or not (0.45 * lwe <= p["start"] <= 0.70 * lwe):
            continue
        for w in p["content"]:
            if gag_w is None or len(base(w["text"])) > len(base(gag_w["text"])):
                gag_p, gag_w = k, w

    items = []  # {t, kind, text | l/r, prio}
    rows_override = list(co["rows"])
    n_item = 0
    for j in title_ids:
        items.append({"t": words[j]["start"], "kind": "dbl", "prio": 3, "text": fit(base(words[j]["text"]).upper(), COLS)})
    if rule_t is not None:
        items.append({"t": rule_t, "kind": "ln", "prio": 2, "text": "-" * (COLS - 2)})
    for k, p in enumerate(P[:tot_first]):
        p["content"] = [w for w in p["content"] if id(w) not in title_set]
        if not p["content"] and any(id(w) in title_set for w in p["words"]):
            continue
        if k == gag_p:
            if gag_w is None:
                raise BuildError("internal: gag phrase selected without a gag word")
            hero = base(gag_w["text"]).upper()
            g = list(co["gag"]) or ["100%", fit(hero, 12) + "!!", "trust me bro"]
            if len(g) != 3:
                raise BuildError("--content gag must be [line1, line2, small]")
            items.append({"t": p["start"], "kind": "gag", "prio": 3, "big": [str(g[0]), str(g[1])], "small": str(g[2])})
            continue
        n_item += 1
        if rows_override:
            left_s, right_s = rows_override.pop(0)
            text = row(str(left_s).upper(), str(right_s).upper())
        elif p["content"]:
            right = base(p["content"][-1]["text"]).upper()
            left = " ".join(base(w["text"]).upper() for w in p["content"][:-1]) or f"ITEM {n_item:02d}"
            text = row(left, right)
        else:
            text = row(f"ITEM {n_item:02d}", base(p["words"][-1]["text"]).upper() or "-")
        items.append({"t": p["start"], "kind": "ln", "prio": 1, "text": text})

    items.sort(key=lambda it: it["t"])
    # schedule: rows never overlap a feed; drop low-priority rows that would slip past their phrase
    sched, last_t, last_kind = [], -1.0, None
    tear_here_t = total_t - 0.6
    for it in items:
        gap = GAG_HOLD if last_kind == "gag" else MIN_STEP
        t = max(it["t"], last_t + gap if last_t >= 0 else it["t"])
        if t > tear_here_t - MIN_STEP + 1e-6 or (it["prio"] == 1 and t > it["t"] + 0.8):
            if it["prio"] >= 3 and t <= total_t - 0.3:
                pass  # titles and the gag are kept while they fit before the total
            else:
                continue
        it["t"] = round(t, 4)
        sched.append(it)
        last_t, last_kind = t, it["kind"]
    tear_here_t = max(tear_here_t, last_t + MIN_STEP)
    total_t = max(total_t, tear_here_t + MIN_STEP)
    sched.append({"t": round(tear_here_t, 4), "kind": "dim", "prio": 2, "text": "- - - TEAR HERE - - -"})
    big = co["total"] or " ".join(base(w["text"]).upper() for w in tot_words)
    while len(big) > TOTAL_COLS and " " in big:
        big = big.split(" ", 1)[1]
    sched.append({"t": round(total_t, 4), "kind": "total", "prio": 3,
                  "label": row(co["total_label"], co["total_due"], 24), "big": fit(big, TOTAL_COLS)})
    ftr_t = total_t + 0.55
    if ftr_t <= tear - 0.25:
        sched.append({"t": round(ftr_t, 4), "kind": "ftr", "prio": 1, "text": fit(co["footer"], FTR_COLS)})
    if tear < total_t + 0.7:
        tear = round(total_t + 0.7, 4)  # the tear may run into the end card cut; the card covers it

    stamp = None
    gi = next((i for i, it in enumerate(sched) if it["kind"] == "gag"), None)
    if gi is not None:
        g_t = sched[gi]["t"]
        nxt = [p["start"] for p in P if p["start"] >= g_t + 1.2]
        st = nxt[0] if nxt else g_t + 1.2
        st = min(st, total_t - 0.4)
        if st >= g_t + 0.8:
            stamp = {"t": round(st, 4), "text": str(co["stamp"])[:6]}
    for it in sched:
        it.pop("prio", None)

    return {
        "cols": COLS,
        "items": sched,
        "stamp": stamp,
        "tear": tear,
        "firstWordStart": round(words[0]["start"], 4),
        "lastWordEnd": round(lwe, 4),
        "narration": round(duration, 6),
        "endcard": ec,
        "duration": ec["end"],
        "brand": brand,
        "header": {"name": brand.get("name") or "Keller AI", "title": fit(co["title"], 24),
                   "label": fit(f"KAI-{slug.upper()} - THERMAL", 22)},
    }


# ----------------------------------------------------------------------------- sfx (port of r5L-S49-L sfx_gen.py)

def synth_sfx(plan: dict, out: Path, sr: int = 48000) -> Path:
    """Stepper ticks plus a 190 Hz buzzed print head per row, a clunk-clunk on the gag, a stamp
    thud, a rip on the tear and a soft thud on the end-card cut. Seeded numpy, no samples."""
    import numpy as np

    N = int(round(plan["duration"] * sr))
    buf = np.zeros(N)
    rng = np.random.default_rng(49)

    def add(t, s):
        i = int(t * sr)
        if i < 0 or i >= N:
            return
        s = s[: N - i]
        buf[i:i + len(s)] += s

    def burst(d, v, decay=30):
        n = int(d * sr)
        s = rng.uniform(-1, 1, n)
        s = np.convolve(s, np.ones(6) / 6, "same")
        return v * s * np.exp(-np.arange(n) / sr * decay)

    def printline(t, d=0.30):
        for k in range(5):
            add(t + k * 0.04, burst(0.025, 0.5, 120))
        n = int(d * sr)
        tt = np.arange(n) / sr
        s = rng.uniform(-1, 1, n) * np.sign(np.sin(2 * np.pi * 190 * tt))
        add(t + 0.2, 0.3 * s * np.minimum(1, tt / 0.02) * np.minimum(1, (d - tt) / 0.03))

    def thud(t, f=70, v=0.6):
        n = int(0.18 * sr)
        add(t + 0.01, v * np.sin(2 * np.pi * f * np.arange(n) / sr) * np.exp(-np.arange(n) / sr * 22))

    for it in plan["items"]:
        printline(it["t"])
        if it["kind"] == "gag":
            add(it["t"] + 0.2, burst(0.12, 1, 25))
            add(it["t"] + 0.34, burst(0.12, 0.8, 25))
    if plan["stamp"]:
        add(plan["stamp"]["t"], burst(0.10, 1.1, 40))
        thud(plan["stamp"]["t"])
    add(plan["tear"], burst(0.45, 1.2, 6))
    thud(plan["endcard"]["start"], f=55, v=0.8)

    pk = float(np.max(np.abs(buf))) or 1.0
    buf = buf / pk * 10 ** (-22 / 20)
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
    for token in ("__L_DURATION__", "__L_VO_DURATION__", "/*__L_DATA__*/null", "__L_TITLE__"):
        if token not in html:
            raise BuildError(f"template is missing placeholder {token}")
    inject = {k: v for k, v in plan.items() if k not in ("audio", "voice_source")}
    vo_dur = f"{min(probe_duration(Path(bus['voice'])), plan['duration']):.6f}"
    html = (html.replace("__L_VO_DURATION__", vo_dur)
                .replace("__L_DURATION__", f"{plan['duration']:.6f}")
                .replace("/*__L_DATA__*/null", json.dumps(inject, ensure_ascii=False).replace("</", "<\\/"))
                .replace("__L_TITLE__", (slug or wav.stem).replace("<", "")))
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
    ap = argparse.ArgumentParser(description="Instantiate the style L (thermal receipt) template.")
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
        print(f"built {plan['project']} duration={plan['duration']:.3f}s items={len(plan['items'])}")
    return 0 if plan["audio"]["bus_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
