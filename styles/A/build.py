#!/usr/bin/env python3
"""Style A (kinetic typography) template: instantiate a HyperFrames project from script-agnostic inputs.

Canonical look: the reference lookdev (S61 open). This template keeps that look (two type registers,
six kinetic treatments, glows, ghost type, dust, grain, HUD, motion-blurred snaps) but drives every
time from the words file instead of hand-timed S61 numbers.

Inputs (all script-agnostic):
  --wav WAV          narration (any rate/channels; normalised by kit/sfx_bus.py)
  --words JSON       list of {text, start, end} (lookdev format; {"words": [...]} also accepted)
  --duration S       optional total length; must leave >= 1.5 s of end card after the last word.
                     Default: kit/endcard.js plan() default (last word end + 0.1 s gap + 2.0 s hold)
  --brand JSON       optional {name, kicker, line, bg, fg, accent}; name/line/bg/fg/accent go to the
                     kit end card, name + kicker to the HUD
  --out DIR          output dir (created). Writes:
                       DIR/project/index.html (+ kit/, assets/)   the HyperFrames project
                       DIR/audio/{sfx_raw,voice_norm,sfx_bus,mix}.wav
                       DIR/plan.json   phrases, end-card plan, duration
                       DIR/words.json  normalised words (for preflight/endcard.py and evidence.py)
  --render           also render DIR/styleA.mp4 (sandboxed, offline, -w 2)
  --quality Q        render quality (default delivery)

Python API: build(wav, words, out, duration=None, brand=None, seed=61) -> dict (plan.json content plus
paths); render(out, quality="delivery") -> Path to the mp4. CLI exit: 0 ok, 2 usage/IO/render error.

Fonts (D4): only the bundled fonts in kit/fonts are used: Lilita One (shout + end card),
Virgil (aside) (both OFL), Comic Shanns (HUD) (MIT). No system or Google fonts.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN = HERE.parents[1]
sys.path.insert(0, str(PLUGIN))

import importlib.util  # noqa: E402

from kit import sfx_bus  # noqa: E402

# styles/A/sfx.py under a unique module name (each style dir may have its own sfx.py)
_spec = importlib.util.spec_from_file_location("style_a_sfx", HERE / "sfx.py")
if _spec is None or _spec.loader is None:
    raise ImportError(f"cannot load style sfx module from {HERE / 'sfx.py'}")
style_sfx = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(style_sfx)

KIT = PLUGIN / "kit"
ENDCARD_JS = KIT / "endcard.js"
SANDBOX = KIT / "offline.sb"
HYPERFRAMES = "hyperframes@0.8.183"
FPS = 30

# phrase chunking
MAX_WORDS = 6
MAX_PHRASE_S = 2.8
GAP_BREAK_S = 0.30
MIN_PHRASE_S = 0.9
MERGE_MAX_WORDS = 8
N_KINDS = (
    6  # 0 roll (opening only), 1 flip, 2 drop, 3 track-lock, 4 zoom, 5 check-field
)

DEFAULT_BRAND = {"name": "KELLER AI", "kicker": "", "line": ""}


class BuildError(Exception):
    pass


def load_words(path) -> list[list]:
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise BuildError(f"cannot read words {path}: {exc}") from exc
    if isinstance(data, dict):
        data = data.get("words")
    if not isinstance(data, list) or not data:
        raise BuildError(f"{path}: expected a non-empty list of {{text,start,end}}")
    out = []
    for w in data:
        try:
            t, s, e = str(w["text"]).strip(), float(w["start"]), float(w["end"])
        except (KeyError, TypeError, ValueError) as exc:
            raise BuildError(f"{path}: bad word entry {w!r}") from exc
        if t:
            out.append([t, round(s, 3), round(max(e, s), 3)])
    out.sort(key=lambda w: w[1])
    return out


def _core(t: str) -> str:
    return re.sub(r"[^\w']", "", t)


def plan_phrases(words: list[list]) -> list[dict]:
    """Chunk words into phrases and pick one hero word per phrase. Deterministic."""
    groups, cur = [], []
    for i, w in enumerate(words):
        cur.append(i)
        nxt = words[i + 1] if i + 1 < len(words) else None
        dur = w[2] - words[cur[0]][1]
        brk = (
            nxt is None
            or re.search(r"[.,;:!?]$", w[0]) is not None
            or nxt[1] - w[2] >= GAP_BREAK_S
            or len(cur) >= MAX_WORDS
            or dur >= MAX_PHRASE_S
        )
        if brk:
            groups.append(cur)
            cur = []
    # merge too-short phrases into the previous one (the canonical look read as too fast)
    merged = []
    for g in groups:
        span = words[g[-1]][2] - words[g[0]][1]
        if (
            merged
            and span < MIN_PHRASE_S
            and len(merged[-1]) + len(g) <= MERGE_MAX_WORDS
        ):
            merged[-1] = merged[-1] + g
        else:
            merged.append(g)
    phrases = []
    for pi, g in enumerate(merged):
        hero = max(g, key=lambda i: (len(_core(words[i][0])), i))
        phrases.append(
            {
                "i0": g[0],
                "i1": g[-1],
                "hero": hero,
                "hero_text": words[hero][0],
                "start": words[g[0]][1],
                "end": words[g[-1]][2],
                "kind": 0 if pi == 0 else 1 + (pi - 1) % (N_KINDS - 1),
            }
        )
    for pi, ph in enumerate(phrases):
        if pi + 1 < len(phrases):
            nxt = phrases[pi + 1]["start"]
            ph["exit"] = round(max(ph["end"] - 0.04, nxt - 0.26), 3)
        else:
            ph["exit"] = None  # last phrase holds until the end card cuts in
    return phrases


def endcard_plan(last_word_end: float, hold: float | None = None) -> dict:
    """kit/endcard.js plan(), run in node so the frame rounding is the kit's own."""
    arg = {"lastWordEnd": last_word_end, "fps": FPS}
    if hold is not None:
        arg["hold"] = hold
    js = (
        f"const K=require({json.dumps(str(ENDCARD_JS))});"
        f"process.stdout.write(JSON.stringify(K.plan({json.dumps(arg)})))"
    )
    r = subprocess.run(["node", "-e", js], capture_output=True, text=True)
    if r.returncode != 0:
        raise BuildError(f"endcard plan failed: {r.stderr.strip()[-400:]}")
    return json.loads(r.stdout)


def build(wav, words, out, duration=None, brand=None, seed=61) -> dict:
    wav, out = Path(wav), Path(out)
    if not wav.is_file():
        raise BuildError(f"wav not found: {wav}")
    wl = load_words(words)
    last_end = max(w[2] for w in wl)
    hold = None
    if duration is not None:
        hold = round(float(duration) - (last_end + 0.1), 4)
    ec = endcard_plan(last_end, hold)
    total = ec["end"]
    phrases = plan_phrases(wl)
    b = dict(DEFAULT_BRAND)
    b.update({k: v for k, v in (brand or {}).items() if v is not None})
    plan = {
        "words": wl,
        "phrases": phrases,
        "endcard": ec,
        "duration": total,
        "last_word_end": last_end,
        "seed": int(seed),
        "brand": b,
        "fps": FPS,
    }

    proj = out / "project"
    for sub in ("kit", "assets/fonts", "assets/brand"):
        (proj / sub).mkdir(parents=True, exist_ok=True)
    shutil.copy(PLUGIN / "kit" / "vendor" / "gsap.min.js", proj / "kit/gsap.min.js")
    shutil.copy(ENDCARD_JS, proj / "kit/endcard.js")
    shutil.copy(HERE / "assets/grain.png", proj / "assets/grain.png")
    for f in ("LilitaOne.woff2", "Virgil.woff2", "ComicShanns.woff2"):
        shutil.copy(KIT / "fonts" / f, proj / "assets/fonts" / f)
    shutil.copy(
        KIT / "brand/keller-ai-logo.svg", proj / "assets/brand/keller-ai-logo.svg"
    )

    audio = out / "audio"
    audio.mkdir(parents=True, exist_ok=True)
    style_sfx.render(plan, str(audio / "sfx_raw.wav"), seed=seed)
    try:
        bus = sfx_bus.build(str(wav), [str(audio / "sfx_raw.wav")], str(audio))
    except sfx_bus.SfxBusError as exc:
        raise BuildError(f"sfx bus failed: {exc}") from exc
    shutil.copy(bus["mix"], proj / "assets/mix.wav")

    html = (HERE / "template.html").read_text()
    dur_s = f"{total:.6f}".rstrip("0").rstrip(".")
    html = (
        html.replace("__DURATION__", dur_s)
        .replace(
            "__PLAN_JSON__",
            json.dumps(plan, separators=(",", ":")).replace("</", "<\\/"),
        )
        .replace("__TITLE__", "Style A kinetic type")
    )
    (proj / "index.html").write_text(html)
    (out / "words.json").write_text(
        json.dumps(
            [
                {"id": f"w{i}", "text": w[0], "start": w[1], "end": w[2]}
                for i, w in enumerate(wl)
            ],
            indent=1,
        )
    )
    plan["paths"] = {
        "project": str(proj),
        "words": str(out / "words.json"),
        "voice_norm": bus["voice"],
        "sfx_bus": bus["sfx"],
        "mix": bus["mix"],
    }
    plan["bus"] = {
        "passed": bus["passed"],
        "voice_gain_db": bus["voice_gain_db"],
        "sfx_gain_db": bus["sfx_gain_db"],
    }
    (out / "plan.json").write_text(json.dumps(plan, indent=1))
    return plan


def render(out, quality="delivery", timeout=1800) -> Path:
    out = Path(out)
    proj = out / "project"
    mp4 = out / "styleA.mp4"
    cmd = [
        "sandbox-exec",
        "-f",
        str(SANDBOX),
        "npx",
        HYPERFRAMES,
        "render",
        ".",
        "-q",
        quality,
        "-w",
        "2",
        "-o",
        str(mp4),
    ]
    env = {**os.environ, "npm_config_offline": "true"}
    r = subprocess.run(
        cmd, cwd=proj, env=env, capture_output=True, text=True, timeout=timeout
    )
    (out / "render.log").write_text(r.stdout + "\n--- stderr ---\n" + r.stderr)
    if r.returncode != 0 or not mp4.is_file():
        raise BuildError(
            f"render failed (exit {r.returncode}); see {out / 'render.log'}"
        )
    return mp4


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Instantiate (and optionally render) the style A template."
    )
    ap.add_argument("--wav", required=True)
    ap.add_argument("--words", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--duration", type=float)
    ap.add_argument("--brand")
    ap.add_argument("--seed", type=int, default=61)
    ap.add_argument("--render", action="store_true")
    ap.add_argument("--quality", default="delivery")
    a = ap.parse_args(argv)
    try:
        brand = json.loads(Path(a.brand).read_text()) if a.brand else None
        plan = build(a.wav, a.words, a.out, a.duration, brand, a.seed)
        res = {
            "project": plan["paths"]["project"],
            "duration": plan["duration"],
            "endcard": plan["endcard"],
            "phrases": len(plan["phrases"]),
        }
        if a.render:
            res["video"] = str(render(a.out, a.quality))
    except (
        BuildError,
        OSError,
        json.JSONDecodeError,
        subprocess.TimeoutExpired,
    ) as exc:
        print(f"build.py: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(res, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
