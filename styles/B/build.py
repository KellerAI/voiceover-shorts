#!/usr/bin/env python3
"""Instantiate (and optionally render) the style B "Cinematic depth" template for any script.

Usage:
  build.py --wav VOICE.wav --words WORDS.json --out BUILD_DIR
           [--duration S] [--brand BRAND.json] [--emphasis w1,w2] [--eyebrow TEXT]
           [--hud-run TEXT] [--steps S1,...,S8]
           [--render OUT.mp4 [--quality delivery] [--workers 2]] [--json]

Inputs (script-agnostic):
  --wav       narration (any rate/channels; resampled to 48 kHz stereo by the sfx bus).
  --words     word timings: a list of {text, start, end} (lookdev format, extra keys ignored)
              or {"words": [...]}.
  --duration  seconds of the voice to use (the voice is cut to this length). Default: the whole
              wav. Every word must end at or before it. It is NOT the composition length: the
              composition always ends at KAEndCard.plan({lastWordEnd}).end (kit/endcard.js),
              i.e. last word end + 0.1 s gap + 2.0 s brand hold, rounded up to a frame.
  --brand     JSON {name, line, logo, bg, fg, accent}; every key optional. `name` is also the HUD
              brand label (<= 12 chars), `logo` a path to an svg/png that is copied into the project.
              Defaults: kit/endcard.js DEFAULT_BRAND and kit/brand/keller-ai-logo.svg.

Outputs under BUILD_DIR:
  project/            the HyperFrames project (index.html, kit/gsap.min.js, kit/endcard.js,
                      assets/mix.wav, assets/fonts/LilitaOne.woff2, assets/brand/<logo>)
  audio/voice.wav     the cut voice; audio/sfx.wav the raw style B bed;
  audio/bus/          kit/sfx_bus.py outputs: voice_norm.wav, sfx_bus.wav, mix.wav
  config.json         the exact config injected into index.html (cues, plan, brand, hud, steps)
  words.json          the words used (for preflight/endcard.py)
  build.json          manifest (paths, plan, sfx-bus result, render command/output)

Timing: the canonical dir-B world choreography (camera flight, node lighting, check cascade,
unease) is authored on a 10.16 s clock and time-warped linearly so it completes at the end-card
cut. Captions follow the real word times. For scripts much longer than 15 s the warp slows the
camera proportionally (60 s is about 6x slower than the original); grain and dust keep every
frame moving, but the flight reads as a slow drift. Long-form use is not verified.

Exit: 0 built (and rendered if asked); 1 the sfx-bus loudness gate or the render failed;
2 usage or IO error (missing file, bad words, word past --duration, HUD string too long).
"""
from __future__ import annotations

import argparse
import importlib.util
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
from kit import sfx_bus  # noqa: E402

KIT = PLUGIN / "kit"
TEMPLATE = HERE / "template.html"
GSAP = PLUGIN / "kit" / "vendor" / "gsap.min.js"
ENDCARD_JS = KIT / "endcard.js"
FONT = KIT / "fonts" / "LilitaOne.woff2"
DEFAULT_LOGO = KIT / "brand" / "keller-ai-logo.svg"
SANDBOX = KIT / "offline.sb"
HYPERFRAMES = "hyperframes@0.8.183"
FPS = 30
DEFAULT_STEPS = ["READ PLAN", "SCAFFOLD", "IMPLEMENT", "UNIT TESTS", "LINT", "TYPECHECK",
                 "INTEGRATION", "REVIEW"]
HUD_MAX_CHARS = 12          # 44 px mono budget per corner (see template.html HUD comment)
CUE_MAX_WORDS = 4
CUE_MAX_CHARS = 28          # two lines at most in the 888 px caption box
CARRY_WORDS = {"a", "an", "the", "on", "of", "in", "to", "from", "and", "or", "but", "with",
               "for", "at", "by", "is", "it", "its", "that", "this"}
CUE_GAP_BREAK = 0.7         # a pause this long starts a new cue
EMPH_MIN_LEN = 7


class BuildError(RuntimeError):
    """Usage or IO problem (exit 2)."""


def _sfx_module():
    spec = importlib.util.spec_from_file_location("shorts_style_b_sfx", HERE / "sfx.py")
    if spec is None or spec.loader is None:
        raise BuildError(f"cannot load sfx module from {HERE / 'sfx.py'}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_words(path: str | Path) -> list[dict]:
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError) as e:
        raise BuildError(f"cannot read words {path}: {e}") from e
    if isinstance(data, dict):
        data = data.get("words")
    if not isinstance(data, list) or not data:
        raise BuildError(f"{path}: expected a non-empty list of words")
    out = []
    for i, w in enumerate(data):
        try:
            text, s, e = str(w["text"]).strip(), float(w["start"]), float(w["end"])
        except (KeyError, TypeError, ValueError) as ex:
            raise BuildError(f"{path}: word {i} needs text/start/end ({ex})") from ex
        if e < s:
            raise BuildError(f"{path}: word {i} ends before it starts")
        if text:
            out.append({"text": text, "start": round(s, 4), "end": round(e, 4)})
    if not out:
        raise BuildError(f"{path}: no non-empty words")
    return sorted(out, key=lambda w: w["start"])


def _norm(word: str) -> str:
    return re.sub(r"[^a-z0-9']", "", word.lower())


def make_cues(words: list[dict], card_start: float, emphasis: list[str] | None = None,
              eyebrow: str | None = None) -> list[dict]:
    """Group words into caption cues: break after . ? ! , ; :,
    on a pause >= CUE_GAP_BREAK, or at CUE_MAX_WORDS / CUE_MAX_CHARS. Each cue holds until the
    next cue starts (the last one until the end-card cut)."""
    groups: list[list[dict]] = []
    cur: list[dict] = []
    for w in words:
        if cur:
            chars = len(" ".join(x["text"] for x in cur)) + 1 + len(w["text"])
            prev = cur[-1]
            if (w["start"] - prev["end"] >= CUE_GAP_BREAK or prev["text"][-1] in ".?!"
                    or prev["text"][-1] in ",;:"):
                groups.append(cur)
                cur = []
            elif len(cur) >= CUE_MAX_WORDS or chars > CUE_MAX_CHARS:
                # a size break never strands a function word at the end of a cue
                carry = [cur.pop()] if len(cur) > 1 and _norm(prev["text"]) in CARRY_WORDS else []
                groups.append(cur)
                cur = carry
        cur.append(w)
    groups.append(cur)

    emph = {_norm(x) for x in (emphasis or []) if _norm(x)}
    cues = []
    for gi, g in enumerate(groups):
        if emph:
            em_idx = {i for i, w in enumerate(g) if _norm(w["text"]) in emph}
        else:  # default: the longest word of 7+ letters in the cue
            lens = [len(_norm(w["text"])) for w in g]
            best = max(range(len(g)), key=lambda i: lens[i])
            em_idx = {best} if lens[best] >= EMPH_MIN_LEN else set()
        t0 = 0.0 if gi == 0 else round(max(0.0, g[0]["start"] - 0.12), 3)
        cues.append({
            "t0": t0,
            "words": [{"text": w["text"], "t": w["start"], "em": "blue" if i in em_idx else ""}
                      for i, w in enumerate(g)],
        })
    for i, c in enumerate(cues):
        c["t1"] = cues[i + 1]["t0"] if i + 1 < len(cues) else round(card_start, 4)
    if eyebrow:
        cues[0]["eyebrow"] = eyebrow
    return cues


def endcard_plan(last_word_end: float) -> dict:
    """KAEndCard.plan() from kit/endcard.js, evaluated in node (single source of timing)."""
    js = (f"const K = require({json.dumps(str(ENDCARD_JS))}); "
          f"process.stdout.write(JSON.stringify(K.plan({{lastWordEnd: {last_word_end!r}, fps: {FPS}}})));")
    r = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise BuildError(f"KAEndCard.plan failed: {r.stderr.strip()}")
    return json.loads(r.stdout)


def probe_duration(path: str | Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=nw=1:nk=1", str(path)], capture_output=True, text=True,
                       timeout=60)
    try:
        return float(r.stdout.strip())
    except ValueError as e:
        raise BuildError(f"ffprobe could not read {path}: {r.stderr.strip()}") from e


def _ff(*args: str) -> None:
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-y", "-loglevel", "error", *args],
                       capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        raise BuildError(f"ffmpeg failed: {r.stderr.strip()}")


def render_cmd(project: Path, out_mp4: Path, quality: str = "delivery", workers: int = 2) -> list[str]:
    return ["sandbox-exec", "-f", str(SANDBOX), "npx", HYPERFRAMES, "render", ".",
            "-q", quality, "-w", str(workers), "-o", str(out_mp4)]


def render(project: Path, out_mp4: Path, quality: str = "delivery", workers: int = 2,
           log: Path | None = None) -> subprocess.CompletedProcess:
    """cd project && npm_config_offline=true sandbox-exec -f offline.sb npx hyperframes render ."""
    out_mp4.parent.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "npm_config_offline": "true"}
    r = subprocess.run(render_cmd(project, out_mp4, quality, workers), cwd=project, env=env,
                       capture_output=True, text=True, timeout=1800)
    if log:
        log.write_text(r.stdout + "\n--- stderr ---\n" + r.stderr)
    return r


def build(wav: str, words: str, out: str, *, duration: float | None = None,
          brand: dict | None = None, emphasis: list[str] | None = None,
          eyebrow: str | None = None, hud_run: str = "PLAN 8 STEPS",
          steps: list[str] | None = None) -> dict:
    wav_p, out_p = Path(wav), Path(out)
    if not wav_p.is_file():
        raise BuildError(f"no such wav: {wav}")
    ws = load_words(words)
    wav_dur = probe_duration(wav_p)
    use_dur = wav_dur if duration is None else float(duration)
    if use_dur <= 0 or use_dur > wav_dur + 1e-3:
        raise BuildError(f"--duration {use_dur} must be in (0, {wav_dur:.3f}] (the wav length)")
    last_end = max(w["end"] for w in ws)
    if last_end > use_dur + 1e-3:
        raise BuildError(f"last word ends at {last_end:.3f} s, after the voice duration {use_dur:.3f} s")
    steps = list(steps or DEFAULT_STEPS)
    if len(steps) != 8:
        raise BuildError(f"--steps needs exactly 8 labels (the world has 8 nodes), got {len(steps)}")
    brand = dict(brand or {})
    name = str(brand.get("name", "KELLER AI"))
    for label, s in (("brand name", name), ("--hud-run", hud_run)):
        if len(s) > HUD_MAX_CHARS:
            raise BuildError(f"{label} {s!r} is longer than {HUD_MAX_CHARS} chars (HUD budget at 44 px)")
    logo_src = Path(brand.get("logo") or DEFAULT_LOGO)
    if not logo_src.is_file():
        raise BuildError(f"no such logo: {logo_src}")

    plan = endcard_plan(last_end)
    proj = out_p / "project"
    audio = out_p / "audio"
    for d in (proj / "kit", proj / "assets" / "fonts", proj / "assets" / "brand", audio):
        d.mkdir(parents=True, exist_ok=True)
    shutil.copy(GSAP, proj / "kit" / "gsap.min.js")
    shutil.copy(ENDCARD_JS, proj / "kit" / "endcard.js")
    shutil.copy(FONT, proj / "assets" / "fonts" / "LilitaOne.woff2")
    logo_rel = f"assets/brand/{logo_src.name}"
    shutil.copy(logo_src, proj / logo_rel)

    # audio: cut voice -> style B bed -> kit sfx bus (normalise, level, mix, loudness gate)
    voice = audio / "voice.wav"
    _ff("-i", str(wav_p), "-t", f"{use_dur:.6f}", "-ar", "48000", "-ac", "2", "-c:a", "pcm_s24le",
        str(voice))
    sfx = audio / "sfx.wav"
    mod = _sfx_module()
    mod.write_wav(str(sfx), mod.synth(plan["start"], plan["end"]))
    bus = sfx_bus.build(str(voice), [str(sfx)], str(audio / "bus"))
    shutil.copy(bus["mix"], proj / "assets" / "mix.wav")

    card_brand = {k: brand[k] for k in ("name", "line", "bg", "fg", "accent") if k in brand}
    card_brand["logo"] = logo_rel
    config = {
        "fps": FPS,
        "lastWordEnd": last_end,
        "start": plan["start"],
        "end": plan["end"],
        "hold": plan["hold"],
        "cues": make_cues(ws, plan["start"], emphasis, eyebrow),
        "brand": card_brand,
        "hud": {"name": name, "run": hud_run},
        "steps": steps,
    }
    html = TEMPLATE.read_text()
    for token in ("__END__", "__CONFIG_JSON__", "__LOGO__"):
        if token not in html:
            raise BuildError(f"template.html lost its {token} placeholder")
    html = (html.replace("__END__", f"{plan['end']:.6f}")
                .replace("__LOGO__", logo_rel)
                .replace("__CONFIG_JSON__", json.dumps(config).replace("</", "<\\/")))
    (proj / "index.html").write_text(html)
    (out_p / "config.json").write_text(json.dumps(config, indent=2))
    (out_p / "words.json").write_text(json.dumps(ws))
    manifest = {
        "style": "B",
        "project": str(proj),
        "config": str(out_p / "config.json"),
        "words": str(out_p / "words.json"),
        "plan": plan,
        "voice_duration": use_dur,
        "voice_norm": bus["voice"],
        "sfx_bus": bus["sfx"],
        "mix": bus["mix"],
        "sfx_bus_passed": bus["passed"],
        "sfx_bus_loudness": bus["loudness"],
    }
    (out_p / "build.json").write_text(json.dumps(manifest, indent=2))
    return manifest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build the style B template for a script.")
    ap.add_argument("--wav", required=True)
    ap.add_argument("--words", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--duration", type=float)
    ap.add_argument("--brand")
    ap.add_argument("--emphasis", help="comma-separated words to set in the Didot accent")
    ap.add_argument("--eyebrow", help="mono eyebrow over the first cue")
    ap.add_argument("--hud-run", default="PLAN 8 STEPS")
    ap.add_argument("--steps", help="8 comma-separated node labels")
    ap.add_argument("--render", metavar="OUT_MP4")
    ap.add_argument("--quality", default="delivery")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        brand = json.loads(Path(a.brand).read_text()) if a.brand else None
        m = build(a.wav, a.words, a.out, duration=a.duration, brand=brand,
                  emphasis=a.emphasis.split(",") if a.emphasis else None, eyebrow=a.eyebrow,
                  hud_run=a.hud_run, steps=a.steps.split(",") if a.steps else None)
    except (BuildError, OSError, ValueError, sfx_bus.SfxBusError) as e:
        print(f"build.py: {e}", file=sys.stderr)
        return 2
    code = 0 if m["sfx_bus_passed"] else 1
    if a.render and code == 0:
        out_mp4 = Path(a.render).resolve()
        r = render(Path(m["project"]), out_mp4, a.quality, a.workers,
                   log=Path(a.out) / "render.log")
        m["render"] = {"cmd": render_cmd(Path(m["project"]), out_mp4, a.quality, a.workers),
                       "output": str(out_mp4), "returncode": r.returncode}
        Path(a.out, "build.json").write_text(json.dumps(m, indent=2))
        if r.returncode != 0 or not out_mp4.is_file():
            print(f"build.py: render failed (see {Path(a.out) / 'render.log'})", file=sys.stderr)
            code = 1
    if a.json:
        print(json.dumps(m, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())
