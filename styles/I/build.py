#!/usr/bin/env python3
"""Instantiate the style I (Swiss modernist motion poster) template for any narration.

Canonical source: the reference lookdev (read only; nothing there is changed).

Usage:
  python3 styles/I/build.py --wav VOICE.wav --words WORDS.json --out DIR
                            [--duration S] [--brand BRAND.json] [--json]

Inputs (script-agnostic):
  --wav       narration WAV (any rate/channels; ffmpeg reads it).
  --words     word timings: a list of {text, start, end} (lookdev format) or {"words": [...]}.
  --duration  narration length in seconds; default: the WAV duration (ffprobe). Every word
              must end inside it. The composition runs past it to hold the end card.
  --brand     optional JSON object. Keys used by the poster:
                folio  top-left running label (default "Keller AI / Brief")
                pair   two diagram labels for scene 3 (default ["Model A", "Model B"])
                gag    {"big", "sub", "tick", "scrawl", "swap"} sticker texts for the scene-3 break
                bars   three bar labels for scene 5 (default ["Line 1", "Line 2", "Line 3"])
              and the KAEndCard keys name, line, bg, fg, accent, logo (kit/endcard.js).

Output (DIR):
  DIR/project/    a HyperFrames project: index.html, kit/{gsap.min.js,endcard.js},
                  assets/{voice_norm.wav,sfx_bus.wav,mix.wav,fonts/*.woff2,brand/keller-ai-logo.svg}
  DIR/plan.json   the poster plan injected into index.html (also printed with --json)
  DIR/sfx_raw.wav the synthesised SFX layer before kit/sfx_bus.py levels it
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
import wave
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN = HERE.parents[1]
sys.path.insert(0, str(PLUGIN))

from kit import sfx_bus  # noqa: E402

TEMPLATE = HERE / "template"
FPS = 30
# r4-S49-I colour-field wipe times (s1..s5) and its end; scene edges are fitted to these proportions.
ORIG = [0.0, 4.36, 7.0, 10.25, 12.92, 15.06]
FIELDS = ["cream", "navy", "cream", "graphite", "cream"]
END_GAP = 0.1      # KAEndCard.DEFAULT_GAP
END_HOLD = 2.0     # KAEndCard.DEFAULT_HOLD
MIN_SCENE = 1.2
WIPE_LEAD = 0.28   # a wipe starts this long before its scene edge (canonical)
LEAD_CHARS = 19    # body lines: about 19 characters fit 936 px at 90 px
DISP_CHARS = 11    # display lines: about 11 characters fit 1000 px at 180 px
# D4: bundled OFL faces (kit/fonts). Comic Sans -> Comic Shanns, Marker Felt -> Virgil,
# Lilita One for the shared end card.
FONTS = ["LilitaOne.woff2", "ComicShanns.woff2", "Virgil.woff2"]
DEFAULT_BRAND = {
    "folio": "Keller AI / Brief",
    "pair": ["Model A", "Model B"],
    "gag": {"big": "lgtm!!!", "sub": "looks fine to me :)", "tick": "✓✓ checked by a model",
            "scrawl": "same vibes?", "swap": "Model A (again)"},
    "bars": ["Line 1", "Line 2", "Line 3"],
}
CARD_KEYS = ("name", "line", "bg", "fg", "accent", "logo")
NEGATIONS = {"not", "no", "never", "nothing", "none", "cannot", "can't", "won't", "isn't", "don't"}
STOP = set("""a an the and or but if then so of in on at to for from by with as into onto over under about
this that these those it its it's i me my we our you your he she they them their his her
is are was were be been being am do does did have has had will would can could should
may might must just very really also too than more most much many some any each every
there here what which who when where why how all both own same such only one""".split())


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
            out.append({"text": str(w["text"]).strip(), "start": float(w["start"]), "end": float(w["end"])})
        except (KeyError, TypeError, ValueError) as e:
            raise BuildError(f"{path}: bad word entry {w!r}") from e
    out = [w for w in out if w["text"]]
    if not out:
        raise BuildError(f"{path}: no words with text")
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


# ----------------------------------------------------------------------------- plan

def endcard_plan(last_word_end: float) -> dict:
    """Mirror of KAEndCard.plan() with the default gap and hold (the template re-checks it in JS)."""
    start = round(last_word_end + END_GAP, 6)
    end = math.ceil((max(start, last_word_end) + END_HOLD) * FPS - 1e-6) / FPS
    return {"start": start, "end": round(end, 6), "hold": END_HOLD}


def bare(tok: str) -> str:
    return re.sub(r"[^\w'’]", "", tok).lower()


def is_content(tok: str) -> bool:
    b = bare(tok)
    return bool(b) and (b in NEGATIONS or b not in STOP)


def phrase_end(words: list[dict], i: int) -> bool:
    punct = bool(re.search(r"[.,;:?!—–]$", words[i]["text"]))
    gap = i + 1 < len(words) and words[i + 1]["start"] - words[i]["end"] >= 0.2
    return punct or gap


def pick_bounds(words: list[dict], cut: float) -> list[float]:
    """Five scenes like r4-S49-I. The four inner edges are word starts chosen by dynamic
    programming: minimise sum((edge - proportional canonical edge)^2) plus a penalty for an
    edge that does not start a phrase, with every scene >= MIN_SCENE s. B[5] = end-card cut."""
    if cut < 5 * MIN_SCENE:
        raise BuildError(f"narration too short for five scenes (cut at {cut:.2f} s)")
    pen = 4.0
    cands = [(words[i]["start"], 0.0 if phrase_end(words, i - 1) else pen) for i in range(1, len(words))]
    cands = [c for c in cands if MIN_SCENE <= c[0] <= cut - MIN_SCENE]
    targets = [cut * ORIG[k] / ORIG[5] for k in range(1, 5)]
    inf = float("inf")
    best = [[(inf, -1)] * len(cands) for _ in range(4)]
    for j, (t, p) in enumerate(cands):
        best[0][j] = ((t - targets[0]) ** 2 + p, -1)
    for k in range(1, 4):
        for j, (t, p) in enumerate(cands):
            c0 = (t - targets[k]) ** 2 + p
            for i in range(j):
                if best[k - 1][i][0] < inf and t - cands[i][0] >= MIN_SCENE:
                    c = best[k - 1][i][0] + c0
                    if c < best[k][j][0]:
                        best[k][j] = (c, i)
    last = [(best[3][j][0], j) for j, (t, _) in enumerate(cands) if cut - t >= MIN_SCENE and best[3][j][0] < inf]
    if last:
        j = min(last)[1]
        inner = []
        for k in range(3, -1, -1):
            inner.insert(0, cands[j][0])
            j = best[k][j][1]
    else:  # too few words for word-aligned edges: use the proportional points
        inner = targets
    return [0.0] + [round(b, 4) for b in inner] + [round(cut, 4)]


def wrap(idx: list[int], words: list[dict], max_chars: int) -> list[list[int]]:
    lines: list[list[int]] = []
    cur: list[int] = []
    n = 0
    for i in idx:
        L = len(words[i]["text"])
        if cur and n + 1 + L > max_chars:
            lines.append(cur)
            cur, n = [], 0
        n = n + (1 if cur else 0) + L
        cur.append(i)
    if cur:
        lines.append(cur)
    return lines


def split_scene(idx: list[int], words: list[dict]) -> tuple[list[int], list[int]]:
    """(lead, disp): disp is the scene's trailing words (<= 3 words, <= 2 display lines), with
    leading stop words pushed back into the lead so the display starts on a content word."""
    disp: list[int] = []
    for i in reversed(idx):
        trial = [i] + disp
        if disp and (len(trial) > 3 or len(wrap(trial, words, DISP_CHARS)) > 2):
            break
        disp = trial
    while len(disp) > 1 and not is_content(words[disp[0]]["text"]):
        disp.pop(0)
    lead = [i for i in idx if i < disp[0]] if disp else list(idx)
    return lead, disp


def line_obj(line: list[int], words: list[dict], floor: float) -> dict:
    return {"text": " ".join(words[i]["text"] for i in line), "idx": line,
            "t": round(max(words[line[0]]["start"], floor), 4)}


def build_plan(words: list[dict], narr: float, brand: dict) -> dict:
    lwe = max(w["end"] for w in words)
    if lwe > narr + 1e-6:
        late = [w["text"] for w in words if w["end"] > narr + 1e-6]
        raise BuildError(f"{len(late)} word(s) end after the narration ({narr:.3f} s): {late[:3]}")
    ec = endcard_plan(lwe)
    B = pick_bounds(words, ec["start"])
    scenes = []
    for k in range(5):
        idx = [i for i, w in enumerate(words) if B[k] - 1e-6 <= w["start"] < B[k + 1] - 1e-6]
        floor = B[k] - WIPE_LEAD + 0.05 if k else 0.05
        lead, disp = split_scene(idx, words) if idx else ([], [])
        width = LEAD_CHARS
        lead_lines = wrap(lead, words, width)
        while len(lead_lines) > 4:  # long scenes: longer, smaller body lines (the template fits them)
            width += 4
            lead_lines = wrap(lead, words, width)
        disp_lines = wrap(disp, words, DISP_CHARS)
        neg = next((n for n, ln in enumerate(disp_lines)
                    if any(bare(words[i]["text"]) in NEGATIONS for i in ln)), None)
        accent = neg if neg is not None else len(disp_lines) - 1
        dl = []
        for n, ln in enumerate(disp_lines):
            o = line_obj(ln, words, floor)
            o["red"] = n == accent
            o["heavy"] = n == neg
            dl.append(o)
        scenes.append({"k": k, "field": FIELDS[k], "t0": B[k], "t1": B[k + 1], "idx": idx,
                       "lead": [line_obj(ln, words, floor) for ln in lead_lines], "disp": dl})

    def onsets(k: int) -> list[float]:
        s = scenes[k]
        ts = [ln["t"] for ln in s["lead"] + s["disp"]]
        return ts or [s["t0"]]

    def clamp(t: float, k: int, pad: float = 0.15) -> float:
        return round(min(max(t, B[k] + 0.05), B[k + 1] - pad), 4)

    dev = {}
    # S1: the red sun grows from frame 0, crops right on the first display line, slides left on the last.
    d1 = [ln["t"] for ln in scenes[0]["disp"]] or [B[1] * 0.5]
    dev["s1"] = {"crop": clamp(min(d1[0], B[1] * 0.45), 0, 0.9), "slide": clamp(d1[-1], 0, 0.6)}
    # S2: the ring is up as the wipe lands; a red dot orbits it to the scene end.
    o2 = onsets(1)
    dev["s2"] = {"ring": round(B[1] - 0.05, 4), "rule": clamp(scenes[1]["disp"][0]["t"] if scenes[1]["disp"] else o2[-1], 1),
                 "orbit": [round(B[1] + 0.1, 4), round(max(B[1] + 0.6, B[2] - 0.2), 4)]}
    # S3: model A, model B, the check bar, then the sloppy break and the snap back at the edge.
    span3 = B[3] - B[2]
    a_t = clamp(B[2] + 0.15, 2)
    b_t = clamp(B[2] + 0.35 * span3, 2)
    gag0 = clamp(B[2] + 0.55 * span3, 2, 0.6)
    dev["s3"] = {"a": a_t, "b": b_t, "chk": clamp(b_t + 0.12, 2), "gag": gag0,
                 "scrawl": round(gag0 + 0.45, 4), "swap": round(gag0 + 0.15, 4), "snap": round(B[3] - 0.15, 4)}
    # S4: two rings, merged into one red ring on the accent (negation) word.
    acc4 = [ln["t"] for ln in scenes[3]["disp"] if ln["red"]] or [B[3] + 0.6 * (B[4] - B[3])]
    dev["s4"] = {"a": round(B[3] + 0.25, 4), "b": round(B[3] + 0.35, 4), "merge": clamp(max(acc4[0], B[3] + 0.7), 3, 0.35)}
    # S5: three bars on the wipe, labels after.
    dev["s5"] = {"bars": [round(B[4] + 0.06 + 0.12 * i, 4) for i in range(3)], "labels": round(B[4] + 0.5, 4)}
    b = {**DEFAULT_BRAND, **{k: v for k, v in brand.items() if k in DEFAULT_BRAND}}
    b["gag"] = {**DEFAULT_BRAND["gag"], **(brand.get("gag") or {})}
    if len(b["pair"]) != 2 or len(b["bars"]) != 3:
        raise BuildError("--brand: pair needs 2 labels and bars needs 3")
    card = {k: brand[k] for k in CARD_KEYS if k in brand}
    return {"style": "I", "fps": FPS, "duration": ec["end"], "narration": round(narr, 6), "lastWordEnd": round(lwe, 4),
            "endcard": ec, "bounds": B, "wipes": [round(t, 4) for t in B[1:5]], "scenes": scenes, "devices": dev,
            "poster": b, "brand": card, "words": [{"text": w["text"], "start": w["start"], "end": w["end"]} for w in words]}


# ----------------------------------------------------------------------------- sfx

def synth_sfx(plan: dict, out: Path, sr: int = 48000) -> Path:
    """Port of r4-S49-I/sfx_gen.py: clicks on the reveals, swish + thud on every field wipe,
    a detuned boing on the break, a snap when the grid locks back, a thud on the end card."""
    import numpy as np

    rng = np.random.default_rng(49)
    buf = np.zeros(int(sr * plan["duration"]) + 1)

    def add(t, sig):
        i = int(max(0.0, t) * sr)
        n = min(len(sig), len(buf) - i)
        if n > 0:
            buf[i:i + n] += sig[:n]

    def env(n, a=0.002, d=0.08):
        tt = np.arange(n) / sr
        return np.minimum(tt / a, 1) * np.exp(-tt / d)

    def click(f=1800, d=0.025, g=1.0):
        n = int(sr * 0.12)
        tt = np.arange(n) / sr
        return g * (np.sin(2 * np.pi * f * tt) * 0.6 + rng.standard_normal(n) * 0.4) * env(n, 0.0005, d)

    def thud(f=70, g=1.0):
        n = int(sr * 0.4)
        tt = np.arange(n) / sr
        return g * np.sin(2 * np.pi * f * tt * (1 + 0.6 * np.exp(-tt / 0.03))) * env(n, 0.002, 0.12)

    def swish(d=0.28, g=0.5):
        n = int(sr * d)
        x = np.convolve(rng.standard_normal(n), np.ones(12) / 12, "same")
        return g * x * np.sin(np.pi * np.arange(n) / n) ** 2

    def boing(g=0.7):
        n = int(sr * 0.6)
        tt = np.arange(n) / sr
        f = 220 + 90 * np.sin(2 * np.pi * 9 * tt)
        return g * np.sign(np.sin(2 * np.pi * np.cumsum(f) / sr)) * 0.5 * env(n, 0.005, 0.2)

    for s in plan["scenes"]:
        for ln in s["disp"]:
            add(ln["t"], click(2200 if ln["red"] else 1800))
    for t in plan["wipes"]:
        add(t - 0.22, swish())
        add(t + 0.06, thud())
    d = plan["devices"]
    add(d["s3"]["gag"], boing())
    add(d["s3"]["snap"], click(2600, 0.04, 1.2))
    add(d["s4"]["merge"], click(1400, 0.05, 1.1))
    for t in d["s5"]["bars"]:
        add(t, click(1600, 0.02, 0.8))
    add(plan["endcard"]["start"], thud(60, 1.1))

    peak = float(np.max(np.abs(buf))) or 1.0
    buf = buf / peak * 10 ** (-19 / 20)
    st = np.stack([buf, buf], 1)
    with wave.open(str(out), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((st * 32767).astype("<i2").tobytes())
    return out


# ----------------------------------------------------------------------------- build

TOKENS = ("__I_DURATION__", "__I_VO_DURATION__", "/*__I_DATA__*/null")


def build(wav: Path, words_path: Path, out: Path, *, duration: float | None = None, brand: dict | None = None) -> dict:
    wav, words_path, out = Path(wav), Path(words_path), Path(out)
    if not wav.is_file():
        raise BuildError(f"wav not found: {wav}")
    words = load_words(words_path)
    narr = float(duration) if duration is not None else probe_duration(wav)
    if narr <= 0:
        raise BuildError("duration must be > 0")
    plan = build_plan(words, narr, dict(brand or {}))

    proj = out / "project"
    for d in ("kit", "assets/fonts", "assets/brand"):
        (proj / d).mkdir(parents=True, exist_ok=True)
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
    for token in TOKENS:
        if token not in html:
            raise BuildError(f"template is missing placeholder {token}")
    inject = {k: v for k, v in plan.items() if k not in ("audio", "voice_source")}
    vo_dur = f"{min(probe_duration(Path(bus['voice'])), plan['duration']):.6f}"
    html = (html.replace("__I_VO_DURATION__", vo_dur)
                .replace("__I_DURATION__", f"{plan['duration']:.6f}")
                .replace("/*__I_DATA__*/null", json.dumps(inject, ensure_ascii=False).replace("</", "<\\/")))
    (proj / "index.html").write_text(html)
    plan["project"] = str(proj)
    (out / "plan.json").write_text(json.dumps(plan, indent=1, ensure_ascii=False))
    return plan


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Instantiate the style I (Swiss poster) template.")
    ap.add_argument("--wav", required=True)
    ap.add_argument("--words", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--duration", type=float)
    ap.add_argument("--brand")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        brand = json.loads(Path(a.brand).read_text()) if a.brand else None
        if brand is not None and not isinstance(brand, dict):
            raise BuildError("--brand must hold a JSON object")
        plan = build(Path(a.wav), Path(a.words), Path(a.out), duration=a.duration, brand=brand)
    except (BuildError, OSError, ValueError) as e:
        print(f"build.py: {e}", file=sys.stderr)
        return 2
    if a.json:
        print(json.dumps(plan, indent=1, ensure_ascii=False))
    else:
        print(f"built {plan['project']} duration={plan['duration']:.3f}s bounds={plan['bounds']}")
    return 0 if plan["audio"]["bus_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
