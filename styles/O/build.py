#!/usr/bin/env python3
"""Instantiate the style O (split-flap departures board) template for any narration.

Canonical look: the reference lookdev (read only). The template keeps its system: a navy
departures board with the brand as the station (K tile + wordmark, DEPARTURES / board id), a
"NOW SHOWING" / "STATUS" label row, a 12 x 6 grid of graphite split-flap cells (80 x 180 px on an
85 / 198 px pitch) with real flap mechanics (top flap folds 0 -> -90 deg, power2.in; new bottom
flap drops 90 -> 0, power2.out, back.out on the final settle; one dim random glyph per changed
cell; 0.02 s column stagger), pages that overwrite in place with stale rows dimmed to 28 %, a
status label (BOARDING / ON TIME mint / DELAYED amber), the jammed-flap gag (yellow flap, hot-pink
tilted Comic Shanns lowercase glyph, 1.75x push-in, board shudder, row shake, flap stutter, snap
back), cream payoff tiles with a warm glow and a stamp pulse, a blue light pool, vignette, a slow
camera push and seeded film grain jittered at 15 fps.

The canonical's hard-coded S49 copy and times are replaced by a plan derived from the words file,
and the canonical scheduler (project/sched.js) is ported here so that ONE op list drives both the
GSAP timeline and the flap SFX.

Usage:
  python3 styles/O/build.py --wav VOICE.wav --words WORDS.json --out DIR
                            [--duration S] [--brand BRAND.json] [--content CONTENT.json]
                            [--slug S49] [--json]

Inputs (script-agnostic):
  --wav       narration WAV (any rate/channels; ffmpeg reads it).
  --words     word timings: a list of {text, start, end} (lookdev format) or {"words": [...]}.
  --duration  narration length in seconds; default: the WAV duration (ffprobe). Every word must
              end inside it. The composition runs past it to hold the end card.
  --brand     optional JSON {name, line, bg, fg, accent} for KAEndCard.mount (kit defaults);
              "name" is also the station wordmark on the board header (default "Keller AI").
  --content   optional JSON overriding copy, keys of DEFAULT_CONTENT.
  --slug      optional short id; its digits become the board id ("S49" -> "BRIEF 049").
              Default: the WAV stem.

How the words drive the board:
  * Words are cleaned and upper-cased, then packed greedily into lines of at most 12 columns.
    A line also ends after clause punctuation or before a pause >= 0.3 s. A word longer than 12
    columns is split over rows with a trailing "-".
  * Each line flips onto the next row of the board at its first word's start (never closer than
    MIN_STEP to the previous line). Lines with a content word are white, all-stop-word lines dim.
  * A page holds 6 rows. A new page starts when the board is full, or at a sentence start when
    the sentence would not fit in the rows left and the page already has >= 3 rows. A page turn
    dims every row to 28 % and the new lines flip over the old ones (the board is never blank).
  * The gag jams the middle letter of the longest content word (>= 5 letters) whose line starts
    in the 45-85 % window of the narration; the status label goes DELAYED (amber) on the jam.
  * The payoff is the final clause (at most 2 lines), on cream tiles with navy ink, the end glow
    and a stamp pulse. It always lands on the bottom rows of the current page, flipping over
    whatever lines are there (the board overwrites in place, as in the canonical).
  * The shared Keller AI end card (kit/endcard.js) cuts in 0.1 s after the last word and holds
    2 s with a slow push (no frozen frames).

Output (DIR):
  DIR/project/   a HyperFrames project: index.html, kit/{gsap.min.js,endcard.js},
                 assets/{voice_norm.wav,sfx_bus.wav,mix.wav,fonts/,brand/}
  DIR/plan.json  the board plan injected into index.html (also printed with --json)
  DIR/sfx_raw.wav the synthesised flap SFX before the bus
Render it with:
  cd DIR && npm_config_offline=true sandbox-exec -f <repo>/kit/offline.sb \\
      npx hyperframes@0.8.183 render project -w 2 -q delivery --no-browser-gpu -o look.mp4
  (--no-browser-gpu: hardware-GPU capture dropped most of the board during the 1.75x jam zoom in
  the canonical, notes.md:70.)

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
FONTS = ["LilitaOne.woff2", "ComicShanns.woff2"]

# board geometry and flap mechanics (canonical sched.js; must match template/index.html)
COLS, ROWS = 12, 6
D = 0.085          # one flap step
STAG = 0.02        # column stagger
SEED = 49
POOL = "ABCDEFGHIJKLMNOPRSTUVWXYZ0123456789"
MIN_STEP = 0.28    # two lines never start closer than this
PAUSE_BREAK = 0.3  # a pause this long ends a line
JAM_HOLD = 0.62    # the jammed flap is held this long before it snaps back
PAGE_DIM = 0.28
PAGE_LEAD = 0.4    # stale rows dim this long before the first line of a new page

DIM, WHITE, CREAM, MINT, AMBER, NAVY, INKA = "#8A97A3", "#FFFFFF", "#FBF7F0", "#8FD9A8", "#F2B33D", "#0F1B2D", "#1d1300"
RAND_INK = "#7B8792"
JAM_INK = "#FF2EA6"
# Tile backgrounds: [top half, bottom half]. Each carries a solid colour after the gradient so the
# tile counts as on-screen fill (the legibility preflight reads background-color, not images).
TILES = {
    "plain": ["linear-gradient(#323232,#262626) #2c2c2c", "linear-gradient(#202020,#171717) #1b1b1b"],
    "cream": ["linear-gradient(#FBF7EC,#F0E9D8) #F5EFE0", "linear-gradient(#E9E1CC,#DDD4BC) #E3DAC4"],
    "amber": ["linear-gradient(#F9C75C,#F2B33D) #F5BD4C", "linear-gradient(#E9A82F,#DC9B22) #E2A128"],
    "jam": ["linear-gradient(#FFE14D,#F7D531) #FBDB3F", "linear-gradient(#F2CF2A,#E6BF1C) #ECC723"],
}

STOP = set("""a an the and or but if then so of in on at to for from by with as into onto over under about
this that these those it its it's i i'm i've me my we our you your he she they them their his her
is are was were be been being am do does did done have has had having will would can could should
may might must shall just very really also too than more most much many some any each every
there here what which who whom whose when where why how all both own same such only
s t don't doesn't isn't aren't wasn't weren't let's one
called based takes take took get gets got make makes made want wants say says said""".split())
STOP -= {"not", "no", "never"}  # negations are content

DEFAULT_CONTENT = {
    "departures": "DEPARTURES",       # header label above the board id
    "board_id": "",                   # default "BRIEF " + the slug's digits (zero-padded to 3)
    "now_showing": "NOW SHOWING",     # label row, left
    "status_prefix": "STATUS",        # label row, right: "<prefix>  <status>"
    "status": ["BOARDING", "ON TIME", "DELAYED", "BOARDING"],  # start, page 2, jam, payoff
    "footer": ["", "PLATFORM 4"],     # left defaults to the brand name upper-cased
    "lines": [],                      # optional explicit board lines (each <= 12 cols), in order;
                                      # replaces the derived copy line for line (times unchanged)
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
    return re.sub(r"[^\w'’\-%&]", "", tok).replace("’", "'").replace("_", "")


def is_content(tok: str) -> bool:
    c = clean(tok).lower().strip("'")
    return bool(c) and c not in STOP


def ends_sentence(tok: str) -> bool:
    return bool(re.search(r"[.?!]['\"’”]?$", tok.strip()))


def ends_clause(tok: str) -> bool:
    return bool(re.search(r"[.,;:?!—–]['\"’”]?$", tok.strip()))


def fit(s: str, n: int) -> str:
    s = s.strip()
    return s if len(s) <= n else s[: max(1, n - 1)].rstrip() + "~"


def endcard_plan(last_word_end: float) -> dict:
    """Mirror of KAEndCard.plan() with default gap/hold (the template re-checks it in JS)."""
    start = round(last_word_end + END_GAP, 6)
    end = math.ceil((max(start, last_word_end) + END_HOLD) * FPS - 1e-6) / FPS
    return {"start": start, "end": round(end, 6), "hold": END_HOLD}


def board_id(slug: str) -> str:
    digits = re.findall(r"\d+", slug)
    return f"BRIEF {int(digits[0]):03d}" if digits else fit(slug.upper(), 10)


def pack_lines(words: list[dict]) -> list[dict]:
    """Greedy pack of cleaned upper-case tokens into board lines of <= COLS columns.
    Each line: {text, t, words:[word idx], sentence_start, clause_end, sentence_end}."""
    lines: list[dict] = []
    cur: list[int] = []
    toks = [clean(w["text"]).upper() for w in words]
    sent_start = True
    cur_sent_start = True

    def flush():
        nonlocal cur
        if cur:
            last = words[cur[-1]]["text"]
            lines.append({"text": " ".join(toks[i] for i in cur if toks[i]), "t": words[cur[0]]["start"],
                          "words": list(cur), "sentence_start": cur_sent_start,
                          "clause_end": ends_clause(last), "sentence_end": ends_sentence(last)})
        cur = []

    for i, w in enumerate(words):
        tok = toks[i]
        if not tok:
            continue
        if len(tok) > COLS:  # split a long word over rows: "ABCDEFGHIJK-" "LMN..."
            flush()
            cur_sent_start = sent_start
            parts = []
            rest = tok
            while len(rest) > COLS:
                parts.append(rest[: COLS - 1] + "-")
                rest = rest[COLS - 1:]
            parts.append(rest)
            for k, p in enumerate(parts):
                lines.append({"text": p, "t": w["start"] + 0.15 * k, "words": [i], "sentence_start": cur_sent_start and k == 0,
                              "clause_end": k == len(parts) - 1 and ends_clause(w["text"]),
                              "sentence_end": k == len(parts) - 1 and ends_sentence(w["text"])})
            sent_start = ends_sentence(w["text"])
            continue
        cand = " ".join([toks[j] for j in cur] + [tok])
        if cur and len(cand) > COLS:
            flush()
        if not cur:
            cur_sent_start = sent_start
        cur.append(i)
        nxt = words[i + 1] if i + 1 < len(words) else None
        sent_start = ends_sentence(w["text"])
        if nxt is None or ends_clause(w["text"]) or nxt["start"] - w["end"] >= PAUSE_BREAK:
            flush()
    flush()
    return lines


# ----------------------------------------------------------------------------- scheduler (port of sched.js)

class Board:
    def __init__(self):
        self.seed = SEED
        self.cur = [[self.S(" ") for _ in range(COLS)] for _ in range(ROWS)]
        self.free = [[0.0] * COLS for _ in range(ROWS)]
        self.ops: list[dict] = []
        self.dims: list[dict] = []

    @staticmethod
    def S(ch, color=WHITE, tile="plain", ff="KFlap", fs: float = 0, rot=0):
        return {"ch": ch, "color": color, "tile": tile, "ff": ff, "fs": fs, "rot": rot}

    def rnd(self) -> float:
        self.seed = (self.seed * 1103515245 + 12345) % 2147483648
        return self.seed / 2147483648

    def rc(self) -> str:
        return POOL[int(self.rnd() * len(POOL))]

    @staticmethod
    def same(a, b):
        return a["ch"] == b["ch"] and a["color"] == b["color"] and a["tile"] == b["tile"]

    def flip(self, r, c, to, t, n) -> float:
        t = max(t, self.free[r][c])
        for _ in range(n):
            rs = self.S(self.rc(), RAND_INK)
            self.ops.append({"r": r, "c": c, "t": round(t, 4), "d": D, "from": self.cur[r][c], "to": rs, "last": False})
            self.cur[r][c] = rs
            t += D
        self.ops.append({"r": r, "c": c, "t": round(t, 4), "d": round(D * 1.3, 4), "from": self.cur[r][c], "to": to, "last": True})
        self.cur[r][c] = to
        t += D * 1.3
        self.free[r][c] = t
        return t

    def dim(self, rows, t, v):
        self.dims.append({"rows": list(rows), "t": round(max(0.0, t), 4), "v": v})

    def row(self, r, t, text, color, tile="plain", jam_col=None, jam_hold=JAM_HOLD):
        """Overwrite row r with text (left aligned); returns jam info when jam_col is set."""
        want = [self.S(" ") for _ in range(COLS)]
        for i, ch in enumerate(text[:COLS]):
            want[i] = self.S(ch, color, tile) if ch != " " else self.S(" ")
        self.dim([r], t - 0.01, 1)
        j, jam = 0, None
        for c in range(COLS):
            if c == jam_col:
                ch = want[c]["ch"]
                land = self.flip(r, c, self.S(ch.lower(), JAM_INK, "jam", "KBad", 1.12, -9), t + j * STAG, 1)
                snap = land + jam_hold
                self.free[r][c] = snap
                self.flip(r, c, want[c], snap, 0)
                jam = {"jamR": r, "jamC": c, "jamLand": round(land, 4), "snap": round(snap, 4)}
                j += 1
                continue
            if self.same(want[c], self.cur[r][c]):
                continue
            self.flip(r, c, want[c], t + j * STAG, 0 if want[c]["ch"] == " " else 1)
            j += 1
        return jam


def build_plan(words: list[dict], duration: float, slug: str, brand: dict, content: dict) -> dict:
    lwe = max(w["end"] for w in words)
    if lwe > duration + 1e-3:
        raise BuildError(f"last word ends at {lwe:.3f} s, after the narration ({duration:.3f} s)")
    unknown = set(content) - set(DEFAULT_CONTENT)
    if unknown:
        raise BuildError(f"--content has unknown keys: {sorted(unknown)}")
    co = dict(DEFAULT_CONTENT)
    co.update(content)
    if not (isinstance(co["status"], list) and len(co["status"]) == 4):
        raise BuildError("--content status must be [start, page2, jam, payoff]")
    if not (isinstance(co["footer"], list) and len(co["footer"]) == 2):
        raise BuildError("--content footer must be [left, right]")
    ec = endcard_plan(lwe)
    if ec["start"] < 3.0:
        raise BuildError(f"narration too short for a departures board (last word ends at {lwe:.2f} s)")

    lines = pack_lines(words)
    if not lines:
        raise BuildError("no printable words")
    over = list(co["lines"])
    for ln in lines:
        if over:
            ln["text"] = fit(str(over.pop(0)).upper(), COLS)
    # colour: lines with a content word are white, stop-word-only lines dim
    for ln in lines:
        ln["color"] = WHITE if any(is_content(words[i]["text"]) for i in ln["words"]) else DIM
        ln["tile"] = "plain"

    # payoff: the final clause (lines after the last clause/sentence break before the end), <= 2 lines
    k = len(lines) - 1
    while k > 0 and not lines[k - 1]["clause_end"] and len(lines) - k < 2:
        k -= 1
    pay_first = k
    for ln in lines[pay_first:]:
        ln["color"], ln["tile"] = NAVY, "cream"

    # gag: longest content word (>= 5 letters) on a line starting in the 45-85 % window
    gag = None
    for li, ln in enumerate(lines[:pay_first]):
        if not (0.45 * lwe <= ln["t"] <= 0.85 * lwe) or li == 0:
            continue
        col = 0
        for tok in ln["text"].split(" "):
            letters = re.sub(r"[^A-Z]", "", tok)
            if len(letters) >= 5 and len(tok) <= COLS and (gag is None or len(tok) > gag[2]):
                gag = (li, col + len(tok) // 2, len(tok))
            col += len(tok) + 1
    # keep the jammed glyph a letter
    if gag and not lines[gag[0]]["text"][gag[1]].isalpha():
        gag = None

    # times: each line at its first word, never closer than MIN_STEP to the previous one
    last = -1.0
    for ln in lines:
        ln["t"] = round(max(ln["t"], last + MIN_STEP) if last >= 0 else ln["t"], 4)
        last = ln["t"]
    if lines[-1]["t"] >= ec["start"] - 0.3:
        raise BuildError("narration too dense for the board: the last line would flip under the end card")

    # rows and pages
    b = Board()
    r, page, page_starts = 0, 0, [0.0]
    jam = None
    lines_left_in_sentence = []
    for li in range(len(lines)):
        n = 0
        for lj in range(li, len(lines)):
            if lj > li and lines[lj]["sentence_start"]:
                break
            n += 1
        lines_left_in_sentence.append(n)
    for li, ln in enumerate(lines):
        if li >= pay_first:
            # the payoff always lands on the bottom rows, flipping over whatever is there
            r = ROWS - (len(lines) - pay_first) + (li - pay_first)
        turn = r >= ROWS
        if not turn and li < pay_first and ln["sentence_start"] and r >= 3 and lines_left_in_sentence[li] > ROWS - r:
            turn = True
        if turn:
            t_dim = max(ln["t"] - PAGE_LEAD, (b.ops[-1]["t"] if b.ops else 0.0))
            b.dim(range(ROWS), min(t_dim, ln["t"] - 0.02), PAGE_DIM)
            r, page = 0, page + 1
            page_starts.append(ln["t"])
        ln["row"], ln["page"] = r, page
        jc = gag[1] if gag and gag[0] == li else None
        got = b.row(r, ln["t"], ln["text"], ln["color"], ln["tile"], jam_col=jc)
        if got:
            jam = got
        r += 1

    settle = max(o["t"] + o["d"] for o in b.ops)
    if settle > ec["start"] - 0.05:
        raise BuildError(f"flips run to {settle:.2f} s, under the end card at {ec['start']:.2f} s")
    pay = lines[pay_first]
    status = [{"t": 0.0, "text": co["status"][0], "color": CREAM}]
    if len(page_starts) > 1:
        status.append({"t": round(page_starts[1] + 0.3, 4), "text": co["status"][1], "color": MINT})
    elif not jam:
        status.append({"t": round(0.4 * lwe, 4), "text": co["status"][1], "color": MINT})
    if jam:
        status.append({"t": round(jam["jamLand"], 4), "text": co["status"][2], "color": AMBER})
    status.append({"t": round(max(pay["t"], (jam["snap"] + 0.1) if jam else 0), 4), "text": co["status"][3], "color": CREAM})
    status.sort(key=lambda s: s["t"])

    name = brand.get("name") or "Keller AI"
    foot_l = co["footer"][0] or name.upper()
    return {
        "cols": COLS, "rows": ROWS,
        "lines": [{k2: ln[k2] for k2 in ("text", "t", "row", "page", "color", "tile", "sentence_start")} for ln in lines],
        "ops": b.ops,
        "dims": b.dims,
        "tiles": TILES,
        "jam": jam,
        "payoff": {"t": pay["t"], "rows": [ln["row"] for ln in lines[pay_first:]], "settle": round(settle, 4)},
        "status": status,
        "pages": len(page_starts),
        "firstWordStart": round(words[0]["start"], 4),
        "lastWordEnd": round(lwe, 4),
        "narration": round(duration, 6),
        "endcard": ec,
        "duration": ec["end"],
        "brand": brand,
        "header": {"name": name, "departures": fit(co["departures"], 12), "board_id": fit(co["board_id"] or board_id(slug), 10),
                   "now_showing": fit(co["now_showing"], 14), "status_prefix": fit(co["status_prefix"], 8),
                   "footer": [fit(foot_l, 18), fit(co["footer"][1], 12)]},
    }


# ----------------------------------------------------------------------------- sfx (port of r5O-S49-O sfx_gen.py)

def synth_sfx(plan: dict, out: Path, sr: int = 48000) -> Path:
    """A clack on every flap landing from the plan's op list (louder on the final settle), thunks
    on the jam landing and the shudder, a clack on the snap, a stamp thunk on the payoff settle and
    a soft thunk on the end-card cut. Seeded numpy, no samples."""
    import numpy as np

    N = int(round(plan["duration"] * sr))
    buf = np.zeros(N)
    rng = np.random.default_rng(49)

    def add(sig, t):
        i = int(t * sr)
        n = min(len(sig), N - i)
        if i >= 0 and n > 0:
            buf[i:i + n] += sig[:n]

    def clack(g=1.0):
        n = int(sr * 0.03)
        t = np.arange(n) / sr
        x = rng.standard_normal(n) * 0.6 + np.sin(2 * np.pi * rng.uniform(1800, 2600) * t) * 0.4
        return g * x * np.exp(-t / 0.004)

    def thunk(g=1.0, f=140):
        n = int(sr * 0.22)
        t = np.arange(n) / sr
        return g * np.sin(2 * np.pi * f * t * (1 + np.exp(-t / 0.02))) * np.exp(-t / 0.05)

    for o in plan["ops"]:
        add(clack(0.7 if o["last"] else 0.3), o["t"] + o["d"])
    j = plan["jam"]
    if j:
        add(thunk(1.4), j["jamLand"])
        add(thunk(1.0, 110), j["jamLand"] + 0.34)
        add(clack(1.6), j["snap"] + 0.11)
    add(thunk(1.2, 90), plan["payoff"]["settle"])
    add(thunk(0.9, 70), plan["endcard"]["start"])

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
    for token in ("__O_DURATION__", "__O_VO_DURATION__", "/*__O_DATA__*/null", "__O_TITLE__"):
        if token not in html:
            raise BuildError(f"template is missing placeholder {token}")
    inject = {k: v for k, v in plan.items() if k not in ("audio", "voice_source")}
    vo_dur = f"{min(probe_duration(Path(bus['voice'])), plan['duration']):.6f}"
    html = (html.replace("__O_VO_DURATION__", vo_dur)
                .replace("__O_DURATION__", f"{plan['duration']:.6f}")
                .replace("/*__O_DATA__*/null", json.dumps(inject, ensure_ascii=False).replace("</", "<\\/"))
                .replace("__O_TITLE__", (slug or wav.stem).replace("<", "")))
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
    ap = argparse.ArgumentParser(description="Instantiate the style O (split-flap departures board) template.")
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
        print(f"built {plan['project']} duration={plan['duration']:.3f}s lines={len(plan['lines'])} ops={len(plan['ops'])}")
    return 0 if plan["audio"]["bus_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
