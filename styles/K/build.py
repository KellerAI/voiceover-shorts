#!/usr/bin/env python3
"""Style K (16-bit pixel arcade, variant K16) template: instantiate a HyperFrames project from words.

Canonical look: the K16 reference look and its 15 s cut (read only). The template
keeps the K16 system: one paused GSAP timeline redrawing a 270x480 canvas scaled 4x crisp, the hand
defined 5x7 bitmap font with 16-bit outline/shadow/two-tone fill, the Mode-7 floor, parallax stars,
mountains, skyline and clouds, the shaded MODEL bots, the K coin rasterised from the official tile,
the HUD, the sloppy "boil" gag font, mosaic transitions and whole-art-pixel screen shake. Every beat
now comes from the words file instead of hard-coded S49 times; captions stay off (Shorts-Feedback.md:326).

Inputs (script-agnostic):
  wav       narration (any rate/channels; normalised by kit/sfx_bus.py)
  words     list of {text, start, end} (or {"words": [...]}), seconds
  duration  optional minimum composition length in seconds (default: the plan end)
  brand     optional dict/JSON for the end card: {logo, name, line, bg, fg, accent}
            (logo may be a local file path; it is copied into the project)
  content   optional dict/JSON overriding derived copy:
            {"stage": "WORLD 1-1", "headline": [str, str], "meter_label": "POWER",
             "labels": ["A", "A"], "power": "POWER UP!", "start": "PRESS START"}

How the words drive the picture:
  * Sentence 1 is the title/stage act: wordmark + coin + bot, then a stage card whose headline is
    the last one or two words of sentence 1, each slamming in on its own start time.
  * The rest is the level: phrases (punctuation or width breaks) become hero lines in the sky band,
    each word typed in as it is spoken, the phrase key word (longest) in gold with a hit.
  * Sentence 2 (or the first half) brings the twin checker bot; its last phrase is the gag, drawn
    in a speech bubble that turns into the sloppy boil font. A negation word (not/never/no/...)
    after that slams in red with a red sky, X eyes and a drained meter. The last phrase is the
    power-up: the bot bumps a ? block, a K coin rises, the bot turns gold on the last word.
  * The shared Keller AI end card (kit/endcard.js) cuts in 0.1 s after the last word, holds 2 s.

Fixes against the preflight defects (relative to r6-S49-K16-cut15): end card after the last word
with a >= 1.5 s hold (the cut15 card covered "banking."), no iris close to near-black, the Mode-7
floor runs to the bottom edge (the no-subs cut left a 208 px black band), every bitmap glyph is
drawn at scale >= 2 (56 px cap height; the 1x HUD labels were 28 px), the end card has a slow logo
push so its hold never freezes, and the audio is one mix from kit/sfx_bus.py (voice -14 LUFS,
SFX 14 dB under the voice peak). Fonts (D4): the canvas uses no font at all; the end card uses the
bundled OFL Lilita One.

CLI:
  python3 build.py --wav W.wav --words W.json --out PROJECT_DIR [--duration S] [--brand B.json]
                   [--content C.json] [--render OUT.mp4 [--quality draft|looks|delivery] [--workers 2]] [--json]
  Exit 0 on success, 2 on a usage or IO error, 1 if a requested render fails.

Python API:
  build(wav, words, out_dir, *, duration=None, brand=None, content=None) -> manifest dict
  render(project_dir, out_mp4, *, quality="looks", workers=2, timeout=1800) -> Path
  load_words(src) -> [word];  plan_timeline(words, duration=None, content=None) -> dict
  fit_lines(words, max_lines=3) -> {sc, sy, lines};  sanitize(text) -> str
"""
from __future__ import annotations

import argparse
import importlib.util
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
ART_W = 270       # canvas art width (scaled 4x to 1080)
TEXT_W = 258      # usable hero line width in art px
MIN_SC = 2        # smallest glyph scale: 7 art rows * 2 * 4 = 56 px cap height
HERO_SCALES = (5, 4, 3, 2)
HERO_TOP = 50     # first hero line y (art px), under the HUD
HERO_BOTTOM = 214  # hero lines end above the ? block and the bots' heads
KEY_HOLD = 0.55   # seconds a key word stays up after it is spoken
KEY_HOLD_NOT = 0.9
PHRASE_CHARS = 18  # punchy hero phrases (K16 used 1-3 word slams)
NEGATIONS = {"not", "never", "no", "nothing", "none", "isn't", "isnt", "don't", "dont", "can't", "cant",
             "won't", "wont", "doesn't", "doesnt", "aren't", "arent", "wasn't", "wasnt"}
# The template's 5x7 bitmap glyph set (template/index.html, G table). Anything else is dropped.
GLYPHS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.:!?-+%'/>,=)# ")
DEFAULT_TEXT = {"stage": "WORLD 1-1", "meter_label": "POWER", "power": "POWER UP!", "start": "PRESS START"}

sys.path.insert(0, str(PLUGIN))


class BuildError(Exception):
    """Usage or IO problem (CLI exit 2)."""


def _load_local(name: str):
    """Import a sibling module by path under a unique name (no clash with other styles' build/sfx)."""
    spec = importlib.util.spec_from_file_location(f"shorts_style_K_{name}", HERE / f"{name}.py")
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load sibling module {name!r} from {HERE / f'{name}.py'}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------- words and text


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
    if len(out) < 4:
        raise BuildError("need at least 4 words (a title act and a level act)")
    return out


def sanitize(text: str) -> str:
    """Uppercase and keep only glyphs the bitmap font defines (curly quotes -> straight)."""
    t = str(text).upper().replace("’", "'").replace("‘", "'").replace("&", "+")
    t = t.replace("—", "-").replace("–", "-")
    return re.sub(r"\s+", " ", "".join(ch for ch in t if ch in GLYPHS)).strip()


def _bare(t: str) -> str:
    return re.sub(r"[^A-Za-z0-9']", "", t).lower()


def _ends_sentence(t: str) -> bool:
    return bool(re.search(r"[.!?][\"')\]]*$", t))


def _ends_clause(t: str) -> bool:
    return bool(re.search(r"[,;:—][\"')\]]*$", t))


def tw(n_chars: int, sc: int) -> int:
    """Width in art px of n glyphs at scale sc (template tw())."""
    return n_chars * 6 * sc - sc if n_chars else 0


def max_chars(sc: int) -> int:
    return (TEXT_W + sc) // (6 * sc)


def _sy(sc: int) -> int:
    return round(sc * 1.6) if sc >= 4 else sc + 1


def _pitch(sc: int) -> int:
    return 7 * _sy(sc) + 8


def _wrap(tokens: list[str], sc: int) -> list[list[int]] | None:
    mc = max_chars(sc)
    lines, cur, n = [], [], 0
    for i, tok in enumerate(tokens):
        if len(tok) > mc:
            return None
        add = len(tok) if not cur else n + 1 + len(tok)
        if cur and add > mc:
            lines.append(cur)
            cur, n = [i], len(tok)
        else:
            cur.append(i)
            n = add
    if cur:
        lines.append(cur)
    return lines


def fit_lines(tokens: list[str], max_lines: int = 3, scales=HERO_SCALES) -> dict | None:
    """Largest scale at which the tokens wrap into <= max_lines inside the hero band."""
    for sc in scales:
        lines = _wrap(tokens, sc)
        if lines is not None and len(lines) <= max_lines and len(lines) * _pitch(sc) <= HERO_BOTTOM - HERO_TOP:
            return {"sc": sc, "sy": _sy(sc), "pitch": _pitch(sc), "lines": lines}
    return None


def _phrases(words: list[dict]) -> list[list[dict]]:
    """Greedy phrases: break after punctuation, past PHRASE_CHARS, or when it would not fit at scale >= 3."""
    out, cur = [], []
    for w in words:
        cand = cur + [w]
        toks = [sanitize(x["text"]) for x in cand]
        if cur and (len(" ".join(toks)) > PHRASE_CHARS or fit_lines(toks, scales=(5, 4, 3)) is None):
            out.append(cur)
            cur = [w]
        else:
            cur = cand
        if (_ends_sentence(w["text"]) or _ends_clause(w["text"])) and cur:
            out.append(cur)
            cur = []
    if cur:
        if out and len(cur) == 1 and fit_lines([sanitize(x["text"]) for x in out[-1] + cur]) is not None:
            out[-1] = out[-1] + cur
        else:
            out.append(cur)
    return out


def _merge_negation(phrases: list[list[dict]]) -> list[list[dict]]:
    """A phrase ending on its negation ("... IS NOT") takes the next phrase with it when it fits,
    so the negated word lands on screen with the negation (THAT HABIT IS / NOT / INDEPENDENCE)."""
    out = list(phrases)
    for i, p in enumerate(out[:-1]):
        tail = [_bare(w["text"]) for w in p[-2:]]
        if any(t in NEGATIONS for t in tail) and not _ends_sentence(p[-1]["text"]):
            merged = p + out[i + 1]
            if fit_lines([sanitize(w["text"]) for w in merged]) is not None:
                out[i:i + 2] = [merged]
            break
    return out


def _sentences(words: list[dict]) -> list[list[dict]]:
    out, cur = [], []
    for w in words:
        cur.append(w)
        if _ends_sentence(w["text"]):
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return out


def _split_two(words: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split the level into a checker act and a payoff act: sentence end nearest the middle."""
    sents = _sentences(words)
    if len(sents) >= 2:
        t0, t1 = words[0]["start"], words[-1]["end"]
        best, acc, idx = None, 0, 0
        for k, s in enumerate(sents[:-1]):
            acc += len(s)
            cost = abs((s[-1]["end"] - t0) / max(1e-6, t1 - t0) - 0.5)
            if best is None or cost < best:
                best, idx = cost, acc
        return words[:idx], words[idx:]
    mid = max(1, len(words) // 2)
    return words[:mid], words[mid:]


def _frame_ceil(t: float) -> float:
    return math.ceil(t * FPS - 1e-6) / FPS


def plan_timeline(words: list[dict], duration: float | None = None, content: dict | None = None) -> dict:
    """Every beat time and layout the template draws, from the words alone (plus optional copy)."""
    content = content or {}
    sents = _sentences(words)
    if len(sents) >= 2 and len(words) - len(sents[0]) >= 2:
        s1, rest = sents[0], words[len(sents[0]):]
    else:  # one long sentence: title act = first clause or first quarter
        cut = next((i + 1 for i, w in enumerate(words[:-2]) if _ends_clause(w["text"])), max(2, len(words) // 4))
        s1, rest = words[:cut], words[cut:]

    # stage headline: last one or two words of the title act (or content override), each slams in
    head_words = s1[-2:] if len(s1) >= 2 else s1[-1:]
    head_tokens = content.get("headline") or [sanitize(w["text"]).rstrip(".,!?:") for w in head_words]
    head_tokens = [sanitize(h).rstrip(".,!?:") for h in head_tokens][:2]
    head_times = [w["start"] for w in head_words][-len(head_tokens):]
    while len(head_times) < len(head_tokens):
        head_times.insert(0, head_times[0] if head_times else s1[-1]["start"])
    head_sc = []
    for h in head_tokens:
        sc = next((s for s in (5, 4, 3, 2) if tw(len(h), s) <= TEXT_W), MIN_SC)
        head_sc.append(sc)
    stage_start = round(min(max(1.2, head_times[0] - 0.7), s1[-1]["end"]), 3)
    level_start = round((s1[-1]["end"] + rest[0]["start"]) / 2, 3)
    level_start = max(level_start, stage_start + 0.5)
    coin_settle = round(min(1.36, stage_start * 0.68), 3)

    act2, act3 = _split_two(rest)
    if not act3:
        act2, act3 = rest[:-1], rest[-1:]
    phrases2, phrases3 = _phrases(act2), _merge_negation(_phrases(act3))
    all_phrases = [(p, 2) for p in phrases2] + [(p, 3) for p in phrases3]

    neg_idx = None
    for k, p in enumerate(phrases3):
        if any(_bare(w["text"]) in NEGATIONS for w in p):
            neg_idx = k
            break
    gag_i = len(phrases2) - 1 if len(phrases2) >= 2 else None
    power_i = len(all_phrases) - 1

    hero = []
    last_word_end = words[-1]["end"]
    ec_start = round(last_word_end + EC_GAP, 3)
    for i, (p, act) in enumerate(all_phrases):
        toks = [sanitize(w["text"]) for w in p]
        keep = [j for j, t in enumerate(toks) if t]
        toks = [toks[j] for j in keep]
        pw = [p[j] for j in keep]
        if not toks:
            continue
        kind = "line"
        if act == 2 and i == gag_i:
            kind = "gag"
        if act == 3 and neg_idx is not None and i == len(phrases2) + neg_idx:
            kind = "not"
        if i == power_i:
            kind = "power" if kind == "line" else kind
        lay = fit_lines(toks) or fit_lines(toks, max_lines=4, scales=(3, 2))
        if lay is None:
            raise BuildError(f"phrase does not fit the hero band: {' '.join(toks)}")
        if kind == "not":
            key = next(j for j, w in enumerate(pw) if _bare(w["text"]) in NEGATIONS)
        else:
            key = max(range(len(toks)), key=lambda j: (len(toks[j].rstrip(".,!?:")), -j))
        nxt = all_phrases[i + 1][0][0]["start"] if i + 1 < len(all_phrases) else ec_start + 10
        hero.append({
            "kind": kind, "act": act, "t0": round(max(level_start, pw[0]["start"] - 0.12), 3),
            "t1": round(nxt - 0.12, 3), "end": pw[-1]["end"], "key": key,
            "words": [{"t": toks[j], "s": pw[j]["start"], "e": pw[j]["end"]} for j in range(len(toks))],
            **lay,
        })
    # no gap before the first phrase: it opens with the level
    if hero:
        hero[0]["t0"] = level_start
    # the key word stays up long enough to read (a late "NOT" was on screen for 0.08 s); the next
    # phrase starts where this one ends, so the sky band is never empty between phrases
    for i, h in enumerate(hero[:-1]):
        hold = h["words"][h["key"]]["s"] + (KEY_HOLD_NOT if h["kind"] == "not" else KEY_HOLD)
        nxt = hero[i + 1]
        h["t1"] = round(min(max(h["t1"], hold), nxt["t1"] - 0.4), 3)
        nxt["t0"] = h["t1"]

    twin_drop = round(phrases2[1][0]["start"] if len(phrases2) >= 2 else (act2[0]["start"] + act2[-1]["end"]) / 2, 3)
    gag = next((h for h in hero if h["kind"] == "gag"), None)
    neg = next((h for h in hero if h["kind"] == "not"), None)
    power = hero[-1]
    last_w = power["words"][-1]
    power_on = round(last_w["s"], 3)
    bump = round(max(power["t0"] + 0.2, power_on - 0.9), 3)
    end = _frame_ceil(max(ec_start, last_word_end) + EC_HOLD)
    if duration is not None:
        end = max(end, _frame_ceil(float(duration)))

    score_t = [round(w["start"], 3) for w in rest]
    T = {
        "stageStart": stage_start, "levelStart": round(level_start, 3), "coinSettle": coin_settle,
        "headline": [{"t": h, "s": round(s, 3), "sc": sc} for h, s, sc in zip(head_tokens, head_times, head_sc)],
        "walk": [round(level_start + 0.05, 3), round(level_start + 1.7, 3)],
        "twinDrop": twin_drop, "twinLand": round(twin_drop + 0.36, 3),
        "gag": [gag["t0"], gag["t1"]] if gag else None,
        "gagSloppy": round(gag["t0"] + 0.27, 3) if gag else None,
        "neg": {"t": neg["words"][neg["key"]]["s"], "t0": neg["t0"], "t1": neg["t1"]} if neg else None,
        "bump": bump, "powerOn": power_on,
        "scoreTimes": score_t,
        "hero": hero,
        "ecStart": ec_start, "END": end, "lastWordEnd": last_word_end,
    }
    return {"T": T, "acts": {"title": s1, "checker": act2, "payoff": act3}}


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
    plan = plan_timeline(ws, duration, content)
    T = plan["T"]

    out = Path(out_dir)
    for sub in ("kit", "assets/fonts", "assets/brand", "assets/audio"):
        (out / sub).mkdir(parents=True, exist_ok=True)
    shutil.copy(PLUGIN / "kit" / "vendor" / "gsap.min.js", out / "kit/gsap.min.js")
    shutil.copy(KIT / "endcard.js", out / "kit/endcard.js")
    shutil.copy(KIT / "fonts/LilitaOne.woff2", out / "assets/fonts/LilitaOne.woff2")
    logo = brand.get("logo")
    if logo and Path(logo).is_file():
        dst = out / "assets/brand" / Path(logo).name
        shutil.copy(logo, dst)
        brand["logo"] = f"assets/brand/{dst.name}"
    else:
        shutil.copy(KIT / "brand/keller-ai-logo.svg", out / "assets/brand/keller-ai-logo.svg")
        brand["logo"] = "assets/brand/keller-ai-logo.svg"
    brand.setdefault("name", "KELLER AI")
    # Light brand card (official #FBF7F0 ground, #10243E ink): after the navy arcade it is an
    # unmistakable hard cut for the scene detector.
    brand.setdefault("bg", "#FBF7F0")
    brand.setdefault("fg", "#10243E")

    text = {**DEFAULT_TEXT, **{k: v for k, v in content.items() if k in DEFAULT_TEXT}}
    text = {k: sanitize(v) for k, v in text.items()}
    labels = [sanitize(x)[:1] or "A" for x in (content.get("labels") or ["A", "A"])][:2]
    wordmark = sanitize(brand["name"]) or "KELLER AI"
    wm_sc = next((s for s in (4, 3, 2) if tw(len(wordmark), s) <= TEXT_W), MIN_SC)
    data = {"T": T, "text": text, "labels": labels, "wordmark": wordmark, "wmSc": wm_sc, "brand": brand,
            "minSc": MIN_SC}

    # audio: synthesised 16-bit cue layer on the plan's beats, then the shared SFX bus (loudness owner)
    sfx_mod = _load_local("sfx")
    raw = sfx_mod.render(T, out / "assets/audio/sfx_raw.wav")
    from kit import sfx_bus
    bus = sfx_bus.build(str(wav), [str(raw)], str(out / "assets/audio"))
    if not bus["passed"]:
        raise BuildError(f"sfx bus failed its loudness gate: {json.dumps(bus['loudness'])}")

    end = f"{T['END']:.6f}".rstrip("0").rstrip(".")
    html = (TEMPLATE / "index.html").read_text()
    html = (html.replace("{{FONTFACE}}", _fontface_css())
                .replace("{{DURATION}}", end)
                .replace("{{DATA}}", json.dumps(data, ensure_ascii=False).replace("</", "<\\/")))
    if "{{" in html:
        raise BuildError("template placeholder left unfilled")
    (out / "index.html").write_text(html)
    (out / "words.json").write_text(json.dumps(ws, indent=1))
    manifest = {
        "style": "K", "variant": "K16", "project": str(out), "index": str(out / "index.html"),
        "duration": T["END"], "wav_duration": wav_dur, "last_word_end": T["lastWordEnd"],
        "end_card_start": T["ecStart"], "words": str(out / "words.json"),
        "voice": bus["voice"], "sfx": bus["sfx"], "mix": bus["mix"],
        "voice_gain_db": bus["voice_gain_db"], "sfx_gain_db": bus["sfx_gain_db"],
        "hero": [{"kind": h["kind"], "sc": h["sc"], "text": " ".join(w["t"] for w in h["words"])} for h in T["hero"]],
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
