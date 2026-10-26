#!/usr/bin/env python3
"""Instantiate the style N (gallery placard) template for any narration.

Canonical look: the reference lookdev (read only). The template keeps its system: one long
cream gallery wall of 1080 px bays under a spotlight pool, vignette and film grain; each bay hangs
one framed piece on a locked eye line (frame 820x980 at left 130 / top 470) with a headline above
it (Baskerville, word-by-word pops on the voice, the key word on a navy block) and a placard below
it (rule at top 1500). The camera holds on a bay with a +-14 px creep, then whips to the next one
(0.6 s power4.inOut with a horizontal motion blur). Pieces rotate through the canonical's motifs:
two circles that slide to overlap (first bay), the hockey-stick chart with the yellow "Untitled
(Marketing Claim)" sticky (the one loud gag), a terminal where one model checks another, two
circles that collapse into "A = B", and a final room with the K tile and the wordmark. The
hard-coded S49 times and copy are replaced by a plan derived from the words file.

Usage:
  python3 styles/N/build.py --wav VOICE.wav --words WORDS.json --out DIR
                            [--duration S] [--brand BRAND.json] [--content CONTENT.json]
                            [--slug S49] [--json]

Inputs (script-agnostic):
  --wav       narration WAV (any rate/channels; ffmpeg reads it).
  --words     word timings: a list of {text, start, end} (lookdev format) or {"words": [...]}.
  --duration  narration length in seconds; default: the WAV duration (ffprobe). Every word must
              end inside it. The composition runs past it to hold the end card.
  --brand     optional JSON {name, line, bg, fg, accent} for KAEndCard.mount (kit defaults);
              "name" is also the HUD label, the final-room wordmark and the intro headline.
  --content   optional JSON overriding default copy, keys of DEFAULT_CONTENT.
  --slug      optional short id for the page title (default: the WAV stem).

How the words drive the gallery:
  * Words split into clauses on , ; : . ? ! (or a pause >= 0.6 s). A clause shorter than 1.6 s
    joins its shorter neighbour; more than 7 clauses merge the shortest neighbouring pair. Each
    clause is one bay ("room"). The camera lands on room k (k >= 1) 0.05 s after its first word,
    the whip starting 0.6 s earlier.
  * Headline: the contiguous run of <= 5 words (<= 26 characters) that starts and ends on a
    content word and holds the most content words (latest on a tie); each word pops on its own
    start time, the last one on the navy block. Room 1 opens on a finished intro headline
    ("A <brand> brief") that swaps out just before its own headline is spoken.
  * Placard: title = the headline in title case; caption = the spoken clause (<= 64 characters).
  * Motifs: room 1 circles, the last room the brand piece, rooms between cycle chart (the gag),
    terminal, merge.
  * The shared Keller AI end card (kit/endcard.js) cuts in 0.1 s after the last word and holds
    2 s with a slow push (no frozen frames).

Output (DIR):
  DIR/project/   a HyperFrames project: index.html, kit/{gsap.min.js,endcard.js},
                 assets/{voice_norm.wav,sfx_bus.wav,mix.wav,fonts/,brand/}
  DIR/plan.json  the gallery plan injected into index.html (also printed with --json)
  DIR/sfx_raw.wav the synthesised air-swell SFX before the bus
Render it with:
  cd DIR && npm_config_offline=true sandbox-exec -f <repo>/kit/offline.sb \\
      npx hyperframes@0.8.183 render project -w 2 -q delivery -o look.mp4

Exit: 0 built, 1 the SFX bus loudness gate failed, 2 usage or IO error (message on stderr).
"""
from __future__ import annotations

import argparse
import datetime
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
FONTS = ["LilitaOne.woff2", "ComicShanns.woff2"]
PAN = 0.6          # whip length (canonical)
LAND_LAG = 0.05    # the camera lands just after the room's first word (canonical 4.40 -> 4.45)
MIN_ROOM = 1.6     # a room shorter than this joins a neighbour
MAX_ROOMS = 7
HEAD_CHARS = 26
HEAD_WORDS = 5
CAP_CHARS = 64
GAG_CAP_CHARS = 52
MIDDLE_MOTIFS = ["chart", "terminal", "merge"]

STOP = set("""a an the and or but if then so of in on at to for from by with as into onto over under about
this that these those it its it's i i'm i've me my we our you your he she they them their his her
is are was were be been being am do does did done have has had having will would can could should
may might must shall just very really also too than more most much many some any each every
there here what which who whom whose when where why how all both own same such only
s t don't doesn't isn't aren't wasn't weren't let's one
called based takes take took get gets got make makes made want wants say says said""".split())
STOP -= {"not", "no", "never"}  # negations are content

DEFAULT_CONTENT = {
    "intro": "",                        # room 1 opening headline; default "A <brand name> brief"
    "gag_title": "Untitled (Marketing Claim), {year}",
    "gag_medium": "Medium: vibes",
    "gag_chart_label": "",              # default: "<room title> (all of them)" when it fits
    "gag_number": "10x!!",
    "term_prior": ["$ draft --run brief.md", "✓ done · 1 file"],
    "term_cmd": "review draft.out",
    "term_out": ["reviewing draft…", "✓ looks good to me", "(same blind spots)"],
    "merge_eq": "A = B",
    "titles": [],                       # optional placard title per room, in order
    "captions": [],                     # optional placard caption per room, in order
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


def ends_clause(tok: str) -> bool:
    return bool(re.search(r"[.,;:?!—–]['\"’”]?$", tok.strip()))


def head_word(tok: str, last: bool) -> str:
    """Headline form of a spoken token: inner punctuation kept, a final clause mark becomes '.'."""
    t = re.sub(r"[\"“”]", "", tok.strip())
    if last:
        t = re.sub(r"[,;:—–]+$", "", t)
        if not re.search(r"[.?!]$", t):
            t += "."
    else:
        t = re.sub(r"[.,;:?!—–]+$", "", t)
    return t


def title_case(words: list[str]) -> str:
    out = []
    for i, w in enumerate(words):
        w = re.sub(r"[.,;:?!—–\"“”]+$", "", w)
        if not w:
            continue
        low = w.lower()
        if i > 0 and low in STOP:
            out.append(low)
        elif w[:1].islower():
            out.append(w[:1].upper() + w[1:])
        else:
            out.append(w)
    return " ".join(out)


def fit_text(s: str, n: int) -> str:
    s = " ".join(s.split())
    if len(s) <= n:
        return s
    cut = s[: n - 1]
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip(" ,;:") + "…"


def endcard_plan(last_word_end: float) -> dict:
    """Mirror of KAEndCard.plan() with default gap/hold (the template re-checks it in JS)."""
    start = round(last_word_end + END_GAP, 6)
    end = math.ceil((max(start, last_word_end) + END_HOLD) * FPS - 1e-6) / FPS
    return {"start": start, "end": round(end, 6), "hold": END_HOLD}


# ----------------------------------------------------------------------------- rooms

def clauses_of(words: list[dict]) -> list[list[int]]:
    groups, cur = [], []
    for i, w in enumerate(words):
        cur.append(i)
        nxt = words[i + 1] if i + 1 < len(words) else None
        if nxt is None or ends_clause(w["text"]) or nxt["start"] - w["end"] >= 0.6:
            groups.append(cur)
            cur = []
    return groups


def room_spans(words: list[dict], lwe: float) -> list[list[int]]:
    g = clauses_of(words)

    def span(k: int) -> float:
        s = words[g[k][0]]["start"]
        e = words[g[k + 1][0]]["start"] if k + 1 < len(g) else lwe
        return e - s

    while len(g) > 1:
        short = [k for k in range(len(g)) if span(k) < MIN_ROOM]
        if not short:
            break
        k = min(short, key=span)
        if k == 0:
            j = 1
        elif k == len(g) - 1:
            j = k - 1
        else:
            j = k - 1 if span(k - 1) <= span(k + 1) else k + 1
        a, b = min(j, k), max(j, k)
        g[a:b + 1] = [g[a] + g[b]]
    while len(g) > MAX_ROOMS:
        k = min(range(len(g) - 1), key=lambda k: span(k) + span(k + 1))
        g[k:k + 2] = [g[k] + g[k + 1]]
    if len(g) == 1 and len(g[0]) > 1:
        # one long clause: split at the widest pause that leaves >= 1.0 s on each side
        idx = g[0]
        best = None
        for p in range(1, len(idx)):
            left = words[idx[p]]["start"] - words[idx[0]]["start"]
            right = lwe - words[idx[p]]["start"]
            gap = words[idx[p]]["start"] - words[idx[p - 1]]["end"]
            if left >= 1.0 and right >= 1.0 and (best is None or gap > best[0]):
                best = (gap, p)
        if best:
            g = [idx[:best[1]], idx[best[1]:]]
    if len(g) < 2:
        raise BuildError("narration too short for a gallery (needs at least two rooms)")
    return g


def pick_headline(ws: list[dict]) -> list[int]:
    """Indices (into ws) of the headline run; see the module docstring."""
    cont = [is_content(w["text"]) for w in ws]
    best, best_key = None, None
    for i in range(len(ws)):
        for j in range(i, min(len(ws), i + HEAD_WORDS)):
            if not (cont[i] and cont[j]):
                continue
            txt = " ".join(head_word(ws[k]["text"], k == j) for k in range(i, j + 1))
            if len(txt) > HEAD_CHARS:
                break
            key = (sum(cont[i:j + 1]), j, -i)
            if best_key is None or key > best_key:
                best, best_key = list(range(i, j + 1)), key
    if best is None:
        best = [len(ws) - 1]
    return best


def split_lines(n_words: int, texts: list[str]) -> list[int]:
    """Line index per word: one line up to 13 characters, else two lines balanced by width."""
    full = " ".join(texts)
    if len(full) <= 13 or n_words == 1:
        return [0] * n_words
    best, best_w = 1, None
    for b in range(1, n_words):
        w = max(len(" ".join(texts[:b])), len(" ".join(texts[b:])))
        if best_w is None or w < best_w or (w == best_w and b > best):
            best, best_w = b, w
    return [0 if k < best else 1 for k in range(n_words)]


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
    name = str(brand.get("name") or "Keller AI")

    groups = room_spans(words, lwe)
    n = len(groups)
    motifs = ["circles"] + [MIDDLE_MOTIFS[k % len(MIDDLE_MOTIFS)] for k in range(n - 2)] + ["brand"]
    rooms = []
    for k, g in enumerate(groups):
        ws = [words[i] for i in g]
        land = 0.0 if k == 0 else round(ws[0]["start"] + LAND_LAG, 4)
        hi = pick_headline(ws)
        htexts = [head_word(ws[i]["text"], i == hi[-1]) for i in hi]
        lines = split_lines(len(hi), htexts)
        head = []
        for m, i in enumerate(hi):
            t = ws[i]["start"] if k == 0 else max(ws[i]["start"], land + 0.08)
            head.append({"text": htexts[m], "t": round(t, 4), "line": lines[m], "hl": m == len(hi) - 1})
        titles, caps = co["titles"], co["captions"]
        title = str(titles[k]) if k < len(titles) else title_case(htexts)
        spoken = " ".join(w["text"] for w in ws)
        spoken = re.sub(r"[,;:—–]+$", ".", spoken[:1].upper() + spoken[1:])
        cap = str(caps[k]) if k < len(caps) else spoken
        rooms.append({"motif": motifs[k], "land": land, "pan": None if k == 0 else round(land - PAN, 4),
                      "first": round(ws[0]["start"], 4), "head": head, "title": fit_text(title, 34),
                      "caption": fit_text(cap, GAG_CAP_CHARS if motifs[k] == "chart" else CAP_CHARS)})
    for k in range(1, n):
        # the whip waits until the previous room's last headline word has popped
        prev_t = rooms[k - 1]["head"][-1]["t"]
        r = rooms[k]
        if r["pan"] < prev_t + 0.12:
            r["pan"] = round(prev_t + 0.12, 4)
            r["land"] = round(r["pan"] + PAN, 4)
            for h in r["head"]:
                h["t"] = round(max(h["t"], r["land"] + 0.08), 4)
    for k, r in enumerate(rooms):
        r["end"] = rooms[k + 1]["pan"] if k + 1 < n else ec["end"]
        if r["pan"] is not None and r["pan"] < rooms[k - 1]["land"] + 0.8:
            raise BuildError(f"room {k + 1} pans {r['pan']:.2f} s, too soon after room {k} lands")

    # room 1 opens on a finished intro headline when its own headline starts late enough
    first_head_t = rooms[0]["head"][0]["t"]
    intro = co["intro"] or f"A {name} brief"
    swap = round(first_head_t - 0.22, 4) if first_head_t >= 1.0 else None
    if swap is None:
        intro = ""

    chart_label = co["gag_chart_label"]
    gag = None
    for r in rooms:
        if r["motif"] == "chart":
            lab = chart_label or f"{r['title']} (all of them)"
            if len(lab) > 24:
                lab = fit_text(r["title"], 24)
            gag = {"title": co["gag_title"].replace("{year}", str(datetime.date.today().year)),
                   "medium": co["gag_medium"], "label": lab, "number": co["gag_number"]}
            break

    return {
        "rooms": rooms,
        "intro": {"text": intro, "swap": swap},
        "gag": gag,
        "terminal": {"prior": list(co["term_prior"])[:2], "cmd": str(co["term_cmd"])[:22],
                     "out": list(co["term_out"])[:3]},
        "merge_eq": co["merge_eq"],
        "firstWordStart": round(words[0]["start"], 4),
        "lastWordEnd": round(lwe, 4),
        "narration": round(duration, 6),
        "endcard": ec,
        "duration": ec["end"],
        "brand": brand,
        "name": name,
        "slug": slug,
    }


# ----------------------------------------------------------------------------- sfx (port of r5N-S49-N sfx_gen.py)

def synth_sfx(plan: dict, out: Path, sr: int = 48000) -> Path:
    """Soft filtered-noise air swells on each whip (canonical) plus a low thud on the end-card cut.
    Seeded numpy, no samples."""
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

    def swell(d=0.8):
        n = int(d * sr)
        x = rng.uniform(-1, 1, n)
        lp = np.zeros(n)
        acc = 0.0
        for i in range(n):  # one-pole low-pass, as in the canonical sfx_gen.py
            acc += 0.04 * (x[i] - acc)
            lp[i] = acc
        env = np.sin(np.pi * np.arange(n) / n) ** 2
        return lp * env

    for r in plan["rooms"]:
        if r["pan"] is not None:
            add(r["pan"] + 0.05, swell())
    m = int(0.18 * sr)
    add(plan["endcard"]["start"] + 0.01, 0.15 * np.sin(2 * np.pi * 60 * np.arange(m) / sr) * np.exp(-np.arange(m) / sr * 22))

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
    for token in ("__N_DURATION__", "__N_VO_DURATION__", "/*__N_DATA__*/null", "__N_TITLE__"):
        if token not in html:
            raise BuildError(f"template is missing placeholder {token}")
    inject = {k: v for k, v in plan.items() if k not in ("audio", "voice_source")}
    vo_dur = f"{min(probe_duration(Path(bus['voice'])), plan['duration']):.6f}"
    html = (html.replace("__N_VO_DURATION__", vo_dur)
                .replace("__N_DURATION__", f"{plan['duration']:.6f}")
                .replace("/*__N_DATA__*/null", json.dumps(inject, ensure_ascii=False).replace("</", "<\\/"))
                .replace("__N_TITLE__", (slug or wav.stem).replace("<", "")))
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
    ap = argparse.ArgumentParser(description="Instantiate the style N (gallery placard) template.")
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
        print(f"built {plan['project']} duration={plan['duration']:.3f}s rooms={len(plan['rooms'])}")
    return 0 if plan["audio"]["bus_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
