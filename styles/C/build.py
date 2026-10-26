#!/usr/bin/env python3
"""Style C (Product-UI motion) template: instantiate a HyperFrames project from script-agnostic inputs.

Canonical look: the reference lookdev (read only). This template keeps its system (one perspective
camera flying between three glass UI cards, travel blur, depth of field, specular sheen, two-tone
headlines with per-word rise, reel HUD, grain, the orange tension cue) and drives every beat from
the words file instead of hard-coded S61 timings.

Inputs (all script-agnostic):
  wav       narration (any rate/channels; normalised by kit/sfx_bus.py)
  words     list of {text, start, end} (or {"words": [...]}), seconds
  duration  optional minimum composition length in seconds (default: the wav duration)
  brand     optional dict/JSON for the end card: {logo, name, line, bg, fg, accent}
            (logo may be a local file path; it is copied into the project)
  content   optional dict/JSON overriding derived copy:
            {"heads": [str x3], "kickers": [str x3], "window_title", "ctx_dir", "ctx_branch",
             "tasks_title", "checks_title", "button", "merge"}

How the words drive the picture:
  * The narration is split into three acts at sentence ends nearest 1/3 and 2/3 of the speech
    (clause ends, then any word boundary, as fallbacks).
  * Act 1 types into the terminal word by word as it is spoken; act 2 is a task list whose rows
    (phrases) tick when each phrase ends; act 3 is a checks panel whose rows go green when each
    phrase ends. The camera flies between cards on the act boundaries.
  * Headlines default to the last one or two words of each act (last word in the accent colour).
  * The shared Keller AI end card (kit/endcard.js) cuts in 0.1 s after the last word and holds 2 s.

Fixes against the preflight defects (relative to dir-C): every text run is >= 44 px (legibility),
cards are opaque enough and tall enough to fill the lower third, no ghost chips, the end card holds
>= 1.5 s after the last word, grain plus a slow logo push keep the hold from freezing, and the audio
is one mix built by kit/sfx_bus.py (voice -14 LUFS, SFX 14 dB under the voice peak).
Fonts (D4): SF Pro -> Lilita One, SF Mono -> Comic Shanns, both bundled OFL from kit/fonts.

CLI:
  python3 build.py --wav W.wav --words W.json --out PROJECT_DIR [--duration S] [--brand B.json]
                   [--content C.json] [--render OUT.mp4 [--quality draft|looks|delivery] [--workers 2]] [--json]
  Exit 0 on success, 2 on a usage or IO error, 1 if a requested render fails.

Python API:
  build(wav, words, out_dir, *, duration=None, brand=None, content=None) -> manifest dict
  render(project_dir, out_mp4, *, quality="looks", workers=2, timeout=1800) -> Path
  segment_acts(words) -> [[word, ...] x3];  plan_timeline(words, duration) -> dict
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN = HERE.parents[1]
KIT = PLUGIN / "kit"
TEMPLATE = HERE / "template"
SANDBOX = KIT / "offline.sb"
HYPERFRAMES = "hyperframes@0.8.183"
FPS = 30
EC_GAP = 0.1      # kit/endcard.js DEFAULT_GAP
EC_HOLD = 2.0     # kit/endcard.js DEFAULT_HOLD (the scene detector needs >= 1.5)
ROW_CHARS = 24    # phrase width for list rows (Lilita One 50 px in a 940 px card)
LINE_CHARS = 26   # terminal line width (Comic Shanns 44 px)
MAX_ROWS = 6

DEFAULT_TEXT = {
    "window_title": "agent — zsh",
    "ctx_dir": "~/keller/short",
    "ctx_branch": "main",
    "tasks_title": "Run",
    "checks_title": "Checks",
    "button": "Run checks →",
    "merge": "Merge",
}
DEFAULT_KICKERS = ["THE SETUP", "THE RUN", "THE CHECK"]

sys.path.insert(0, str(PLUGIN))
sys.path.insert(0, str(HERE))


class BuildError(Exception):
    """Usage or IO problem (CLI exit 2)."""


# ---------------------------------------------------------------- words


def load_words(src) -> list[dict]:
    if isinstance(src, (str, Path)):
        try:
            data = json.loads(Path(src).read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise BuildError(f"cannot read words {src}: {exc}") from exc
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
        except (KeyError, TypeError, ValueError) as exc:
            raise BuildError(f"bad word entry {w!r}") from exc
    out = [w for w in out if w["text"]]
    out.sort(key=lambda w: w["start"])
    if len(out) < 3:
        raise BuildError("need at least 3 words to make three acts")
    return out


def _ends_sentence(t: str) -> bool:
    return bool(re.search(r"[.!?][\"')\]]*$", t))


def _ends_clause(t: str) -> bool:
    return bool(re.search(r"[,;:—][\"')\]]*$", t))


def segment_acts(words: list[dict]) -> list[list[dict]]:
    """Split into three non-empty acts, preferring sentence ends near 1/3 and 2/3 of the speech."""
    n = len(words)
    t0, t1 = words[0]["start"], words[-1]["end"]
    span = max(1e-6, t1 - t0)

    def pen(i):
        t = words[i]["text"]
        return 0.0 if _ends_sentence(t) else (0.08 if _ends_clause(t) else 0.25)

    best = None
    for i in range(n - 2):
        ti = (words[i]["end"] - t0) / span
        for j in range(i + 1, n - 1):
            tj = (words[j]["end"] - t0) / span
            cost = abs(ti - 1 / 3) + abs(tj - 2 / 3) + pen(i) + pen(j)
            if best is None or cost < best[0]:
                best = (cost, i, j)
    if best is None:
        raise BuildError(f"cannot split {n} words into three acts (need at least 3)")
    _, i, j = best
    return [words[: i + 1], words[i + 1 : j + 1], words[j + 1 :]]


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
            if (_ends_sentence(w["text"]) or _ends_clause(w["text"])) and len(" ".join(x["text"] for x in cur)) >= 8:
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


def default_head(act: list[dict]) -> list[str]:
    toks = [re.sub(r"[,;:—]+$", "", w["text"]) for w in act[-2:]]
    toks = [t for t in toks if t]
    if len(toks) == 2 and len(" ".join(toks)) > 22:
        toks = toks[-1:]
    if toks and not _ends_sentence(toks[-1]):
        toks[-1] += "."
    return toks or ["…"]


def _frame_ceil(t: float) -> float:
    return math.ceil(t * FPS - 1e-6) / FPS


def plan_timeline(words: list[dict], duration: float | None = None) -> dict:
    """Beat times (seconds) for the template, from the words alone."""
    acts = segment_acts(words)
    rows1 = phrase_rows(acts[0], LINE_CHARS, max_rows=8)
    rows2 = phrase_rows(acts[1], ROW_CHARS)
    rows3 = phrase_rows(acts[2], ROW_CHARS)
    a1_end, a1_last_start = acts[0][-1]["end"], acts[0][-1]["start"]

    leg1_0 = max(a1_end - 0.1, a1_last_start + 0.1)
    leg1 = [round(leg1_0, 3), round(leg1_0 + 0.96, 3)]

    def spaced(times, lo, gap=0.12):
        out = []
        for t in times:
            t = max(t, lo, (out[-1] + gap) if out else lo)
            out.append(round(t, 3))
        return out

    ticks = spaced([r["end"] for r in rows2], leg1[1] + 0.2)
    press = round(max(acts[1][-1]["end"] + 0.05, ticks[-1] + 0.3), 3)
    release = round(press + 0.12, 3)
    leg2 = [round(release + 0.02, 3), round(release + 0.46, 3)]
    checks = spaced([r["end"] for r in rows3], leg2[1] + 0.06)
    tension = round(min(max(rows3[-1]["start"], leg2[1] + 0.1), checks[-1]), 3)
    last_word_end = words[-1]["end"]
    ec_start = round(max(last_word_end + EC_GAP, checks[-1] + 0.3), 3)
    end = _frame_ceil(max(ec_start, last_word_end) + EC_HOLD)
    if duration is not None:
        end = max(end, _frame_ceil(float(duration)))

    keys = []
    for r in rows1:
        for w in r["words"]:
            n = len(w["text"]) + 1
            keys += [w["start"] + (w["end"] - w["start"]) * c / n for c in range(n)]
    if len(keys) > 90:  # cap the click count; keep them evenly sampled
        keys = [keys[int(k * len(keys) / 90)] for k in range(90)]

    return {
        "acts": [acts, rows1, rows2, rows3],
        "T": {
            "leg1": leg1, "leg2": leg2, "ticks": ticks, "match": round(ticks[-1] + 0.1, 3),
            "cursorIn": round(max(leg1[1] + 0.2, press - 0.85), 3), "press": press, "release": release,
            "checks": checks, "tension": tension, "allPass": round(checks[-1] + 0.06, 3),
            "ecStart": ec_start, "END": end, "lastWordEnd": last_word_end,
            "keys": [round(k, 4) for k in keys],
            "lineTicks": [r["start"] for r in rows1[1:]],
        },
    }


# ---------------------------------------------------------------- project


def _probe_duration(path: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise BuildError(f"ffprobe failed on {path}: {r.stderr.strip()}")
    return float(r.stdout.strip())


def _load_obj(v, what):
    if v is None:
        return {}
    if isinstance(v, dict):
        return dict(v)
    try:
        return json.loads(Path(v).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise BuildError(f"cannot read {what} {v}: {exc}") from exc


def _fontface_css() -> str:
    # Same text as KAEndCard.fontFaceCSS() (kit/endcard.js); inline so HyperFrames skips Google Fonts.
    return '@font-face { font-family: "KS Lilita"; font-weight: 400 900; src: url("assets/fonts/LilitaOne.woff2") format("woff2"); }'


def build(wav, words, out_dir, *, duration=None, brand=None, content=None) -> dict:
    """Instantiate the template into out_dir (created; existing files are overwritten)."""
    wav = Path(wav)
    if not wav.is_file():
        raise BuildError(f"no such wav: {wav}")
    ws = load_words(words)
    brand = _load_obj(brand, "brand")
    content = _load_obj(content, "content")
    wav_dur = _probe_duration(wav)
    dur = float(duration) if duration is not None else wav_dur
    plan = plan_timeline(ws, dur)
    acts, rows1, rows2, rows3 = plan["acts"]
    T = plan["T"]

    out = Path(out_dir)
    for sub in ("kit", "assets/fonts", "assets/brand", "assets/audio"):
        (out / sub).mkdir(parents=True, exist_ok=True)
    shutil.copy(PLUGIN / "kit" / "vendor" / "gsap.min.js", out / "kit/gsap.min.js")
    shutil.copy(KIT / "endcard.js", out / "kit/endcard.js")
    for f in ("LilitaOne.woff2", "ComicShanns.woff2"):
        shutil.copy(KIT / "fonts" / f, out / "assets/fonts" / f)
    logo = brand.get("logo")
    if logo and Path(logo).is_file():
        dst = out / "assets/brand" / Path(logo).name
        shutil.copy(logo, dst)
        brand["logo"] = f"assets/brand/{dst.name}"
    else:
        shutil.copy(KIT / "brand/keller-ai-logo.svg", out / "assets/brand/keller-ai-logo.svg")
        brand["logo"] = "assets/brand/keller-ai-logo.svg"
    brand.setdefault("name", "KELLER AI")
    # Light brand card (official #FBF7F0 ground, #10243E ink): after the dark UI it is an unmistakable
    # hard cut, which the scene detector needs (a navy card after near-black UI is too subtle).
    brand.setdefault("bg", "#FBF7F0")
    brand.setdefault("fg", "#10243E")

    heads: list[str | None] = content.get("heads") or [None, None, None]
    kickers = content.get("kickers") or DEFAULT_KICKERS
    act_data = []
    for k, (act, rows) in enumerate(zip(acts, (rows1, rows2, rows3))):
        head_txt = heads[k]
        head = head_txt.split() if head_txt else default_head(act)
        act_data.append({"start": act[0]["start"], "end": act[-1]["end"], "head": head,
                         "kicker": kickers[k], "rows": rows})
    text = {**DEFAULT_TEXT, **{k: v for k, v in content.items() if k in DEFAULT_TEXT}}
    data = {"T": T, "acts": act_data, "text": text, "brand": brand}

    # audio: synthesised UI layer on the plan's cues, then the shared SFX bus (loudness owner)
    import sfx as sfx_mod  # styles/C/sfx.py
    from kit import sfx_bus
    raw = sfx_mod.render(T, out / "assets/audio/sfx_raw.wav")
    bus = sfx_bus.build(str(wav), [str(raw)], str(out / "assets/audio"))
    if not bus["passed"]:
        raise BuildError(f"sfx bus failed its loudness gate: {json.dumps(bus['loudness'])}")

    end = f"{T['END']:.6f}".rstrip("0").rstrip(".")
    html = (TEMPLATE / "index.html").read_text()
    html = (html.replace("{{FONTFACE}}", _fontface_css())
                .replace("{{DURATION}}", end)
                .replace("{{DATA}}", json.dumps(data, ensure_ascii=False).replace("</", "<\\/")))
    (out / "index.html").write_text(html)
    (out / "words.json").write_text(json.dumps(ws, indent=1))
    manifest = {
        "style": "C", "project": str(out), "index": str(out / "index.html"),
        "duration": T["END"], "wav_duration": wav_dur, "last_word_end": T["lastWordEnd"],
        "end_card_start": T["ecStart"], "words": str(out / "words.json"),
        "voice": bus["voice"], "sfx": bus["sfx"], "mix": bus["mix"],
        "voice_gain_db": bus["voice_gain_db"], "sfx_gain_db": bus["sfx_gain_db"],
        "acts": [{"start": a["start"], "end": a["end"], "head": a["head"], "rows": [r["text"] for r in a["rows"]]}
                 for a in act_data],
        "timeline": T,
    }
    (out / "build.json").write_text(json.dumps(manifest, indent=1))
    return manifest


def render(project_dir, out_mp4, *, quality="looks", workers=2, timeout=1800) -> Path:
    """Render offline under the studio sandbox: npx hyperframes@0.8.183 render . -q Q -w N -o OUT."""
    project_dir, out_mp4 = Path(project_dir).resolve(), Path(out_mp4).resolve()
    out_mp4.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["sandbox-exec", "-f", str(SANDBOX), "npx", HYPERFRAMES, "render", ".",
           "-q", quality, "-w", str(workers), "-o", str(out_mp4)]
    env = {**os.environ, "npm_config_offline": "true"}
    # ubs:ignore - false positive: os.environ is passed only as the child env= dict, never as the command; argv is a list, shell=False
    r = subprocess.run(cmd, cwd=project_dir, env=env, capture_output=True, text=True, timeout=timeout)
    out_mp4.with_suffix(".render.log").write_text(" ".join(cmd) + "\n" + r.stdout + "\n--- stderr ---\n" + r.stderr)
    if r.returncode != 0 or not out_mp4.is_file():
        raise RuntimeError(f"render failed ({r.returncode}): {r.stderr[-1500:]}")
    return out_mp4


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    ap.add_argument("--wav", required=True)
    ap.add_argument("--words", required=True)
    ap.add_argument("--out", required=True, help="project dir to create")
    ap.add_argument("--duration", type=float)
    ap.add_argument("--brand")
    ap.add_argument("--content")
    ap.add_argument("--render", help="also render to this mp4")
    ap.add_argument("--quality", default="looks")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        m = build(a.wav, a.words, a.out, duration=a.duration, brand=a.brand, content=a.content)
    except BuildError as exc:
        print(f"build.py: {exc}", file=sys.stderr)
        return 2
    if a.render:
        try:
            m["video"] = str(render(a.out, a.render, quality=a.quality, workers=a.workers))
        except (RuntimeError, subprocess.TimeoutExpired) as exc:
            print(f"build.py: {exc}", file=sys.stderr)
            return 1
    print(json.dumps(m, indent=1) if a.json else f"built {m['project']} ({m['duration']} s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
