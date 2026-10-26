"""Tests for the shared kit: kit/sfx_bus.py, kit/endcard.js, kit/fonts, kit/brand.

All checks are real runs: ffmpeg for audio, node for endcard.plan(), and real
HyperFrames renders (sandboxed offline) gated by the item 4 and item 5 CLIs.
Renders and scratch go under pytest's tmp_path. Tests that read the canonical look E project
or the official brand logo need SHORTS_STUDIO_DIR and skip without it.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PLUGIN = Path(__file__).resolve().parents[1]
KIT = PLUGIN / "kit"
LOUDNESS_CLI = PLUGIN / "preflight" / "loudness.py"
ENDCARD_CLI = PLUGIN / "preflight" / "endcard.py"
SFX_BUS_CLI = KIT / "sfx_bus.py"
ENDCARD_JS = KIT / "endcard.js"

SANDBOX = KIT / "offline.sb"

# External studio data, from SHORTS_STUDIO_DIR. Tests that read it are marked studio (or are real
# renders) and skip when it is unset or a path below is missing (see conftest.py).
_STUDIO_DIR = os.environ.get("SHORTS_STUDIO_DIR")
STUDIO = Path(_STUDIO_DIR) if _STUDIO_DIR else None
if STUDIO is not None:
    LOOK_E = STUDIO / "_kit/lookdev/r3-S43-E/project"
    FONT_SRC = LOOK_E / "assets/fonts"
    BRAND_SRC = STUDIO / "_kit/lookdev/inputs/brand/official/keller-ai-logo.svg"
    GSAP_SRC = LOOK_E / "kit/gsap.min.js"
else:
    LOOK_E = FONT_SRC = BRAND_SRC = GSAP_SRC = None
STUDIO_REQUIRED = (LOOK_E,)

FPS = 30
LAST_WORD_END = 1.9
WORDS = [{"id": 0, "text": "hello", "start": 0.2, "end": 1.0},
         {"id": 1, "text": "world", "start": 1.1, "end": LAST_WORD_END}]


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def run(cmd, **kw):
    return subprocess.run([str(c) for c in cmd], capture_output=True, text=True, **kw)


def ff(*args):
    subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-y", "-loglevel", "error", *map(str, args)],
                   check=True)


# ---------------------------------------------------------------- fonts / brand

@pytest.mark.studio
@pytest.mark.parametrize("name", ["Virgil.woff2", "LilitaOne.woff2", "ComicShanns.woff2"])
def test_ofl_font_copied_byte_identical(name):
    dst = KIT / "fonts" / name
    assert dst.is_file()
    assert sha(dst) == sha(FONT_SRC / name)
    assert (FONT_SRC / name).is_file()  # copied, not moved


@pytest.mark.studio("_kit/lookdev/inputs/brand/official/keller-ai-logo.svg")
def test_brand_logo_copied_from_official():
    dst = KIT / "brand" / "keller-ai-logo.svg"
    assert sha(dst) == sha(BRAND_SRC)


# ---------------------------------------------------------------- endcard.plan (node)

def node_eval(expr):
    js = f"const K = require({json.dumps(str(ENDCARD_JS))}); " \
         f"try {{ console.log(JSON.stringify({{ok: true, v: ({expr})}})); }} " \
         f"catch (e) {{ console.log(JSON.stringify({{ok: false, err: String(e.message)}})); }}"
    r = run(["node", "-e", js], check=True)
    return json.loads(r.stdout)


def test_plan_default_hold_passes_item4_rule():
    r = node_eval(f"K.plan({{lastWordEnd: {LAST_WORD_END}}})")
    assert r["ok"], r
    v = r["v"]
    assert v["hold"] == 2.0 and v["hold"] >= 1.5
    assert v["start"] == pytest.approx(2.0)
    assert v["end"] == pytest.approx(4.0)
    assert node_eval("K.DEFAULT_HOLD")["v"] == 2.0


def test_plan_rounds_end_up_to_frame():
    v = node_eval("K.plan({lastWordEnd: 1.234, hold: 1.6})")["v"]
    assert v["end"] * FPS == pytest.approx(round(v["end"] * FPS))
    assert v["end"] - max(v["start"], 1.234) >= 1.6


def test_plan_rejects_short_hold_and_covering_start():  # planted negatives
    short = node_eval(f"K.plan({{lastWordEnd: {LAST_WORD_END}, hold: 1.0}})")
    assert not short["ok"] and "minimum" in short["err"]
    cover = node_eval(f"K.plan({{lastWordEnd: {LAST_WORD_END}, start: 1.5}})")
    assert not cover["ok"] and "covers the last word" in cover["err"]


def test_fontface_css_matches_contract():
    css = node_eval("K.fontFaceCSS()")["v"]
    assert '"KS Lilita"' in css and 'url("assets/fonts/LilitaOne.woff2")' in css


# ---------------------------------------------------------------- sfx bus

@pytest.fixture(scope="module")
def stems(tmp_path_factory):
    """Real stems from the canonical look E project (copied, read-only source)."""
    d = tmp_path_factory.mktemp("stems")
    shutil.copy(LOOK_E / "assets/narration.wav", d / "voice.wav")
    shutil.copy(LOOK_E / "assets/ui_sfx.wav", d / "sfx.wav")
    return d / "voice.wav", d / "sfx.wav"


def item5(out_dir):
    r = run([sys.executable, LOUDNESS_CLI, out_dir / "mix.wav", "--voice", out_dir / "voice_norm.wav",
             "--sfx", out_dir / "sfx_bus.wav", "--json"])
    return r.returncode, json.loads(r.stdout), r


@pytest.mark.studio
def test_sfx_bus_output_passes_item5_cli(stems, tmp_path):
    out = tmp_path / "bus"
    r = run([sys.executable, SFX_BUS_CLI, "--voice", stems[0], "--sfx", stems[1],
             "--out-dir", out, "--json"])
    assert r.returncode == 0, r.stderr
    built = json.loads(r.stdout)
    assert built["passed"] and built["voice_limiter"] is None
    code, res, raw = item5(out)
    assert code == 0, raw.stdout
    m = res["metrics"]
    assert abs(m["lufs"] + 14.0) <= 0.3
    assert m["true_peak_dbtp"] <= -1.5
    assert m["sfx_under_voice_db"] >= 12.0
    for name in ("voice_norm.wav", "sfx_bus.wav", "mix.wav"):
        p = run(["ffprobe", "-v", "error", "-show_entries", "stream=channels,sample_rate",
                 "-of", "json", out / name], check=True)
        s = json.loads(p.stdout)["streams"][0]
        assert (s["channels"], s["sample_rate"]) == (2, "48000"), name


def test_sfx_bus_limiter_path_passes_item5(tmp_path):
    """Voice whose peaks sit ~33 dB over its body: linear gain alone would clip."""
    voice, sfx = tmp_path / "spiky.wav", tmp_path / "beep.wav"
    ff("-f", "lavfi", "-i",
       "aevalsrc=0.02*sin(2*PI*220*t)+0.9*sin(2*PI*1000*t)*lt(mod(t\\,1)\\,0.01):s=48000:d=6",
       "-ac", "2", voice)
    ff("-f", "lavfi", "-i", "sine=frequency=2000:duration=6:sample_rate=48000", "-ac", "2", sfx)
    from kit import sfx_bus
    res = sfx_bus.build(str(voice), [str(sfx)], str(tmp_path / "bus"))
    assert res["voice_limiter"] is not None
    assert res["passed"], res["loudness"]
    code, _, raw = item5(tmp_path / "bus")
    assert code == 0, raw.stdout


@pytest.mark.studio
def test_planted_too_loud_sfx_fails_item5_cli(stems, tmp_path):  # planted negative
    out = tmp_path / "loud"
    r = run([sys.executable, SFX_BUS_CLI, "--voice", stems[0], "--sfx", stems[1],
             "--out-dir", out, "--sfx-under-db", "6", "--json"])
    assert r.returncode == 1, (r.stdout, r.stderr)
    code, res, raw = item5(out)
    assert code == 1, raw.stdout
    assert [f["kind"] for f in res["findings"]] == ["sfx_too_loud"]
    assert res["metrics"]["sfx_under_voice_db"] == pytest.approx(6.0, abs=0.1)


def test_sfx_bus_usage_and_io_errors(tmp_path):
    assert run([sys.executable, SFX_BUS_CLI, "--voice", "x.wav"]).returncode == 2
    r = run([sys.executable, SFX_BUS_CLI, "--voice", tmp_path / "missing.wav",
             "--sfx", tmp_path / "missing2.wav", "--out-dir", tmp_path / "o"])
    assert r.returncode == 2 and "not a file" in r.stderr


# ---------------------------------------------------------------- endcard render

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=1080, height=1920"/>
<title>item30 endcard {tag}</title>
<script src="kit/gsap.min.js"></script>
<script src="kit/endcard.js"></script>
<style>
{fontface}
html, body {{ margin: 0; background: #ffcc33; }}
#root {{ width: 1080px; height: 1920px; overflow: hidden; background: #ffcc33; }}
#word {{ position: absolute; top: 820px; width: 100%; text-align: center; font-family: "KS Lilita";
        font-size: 220px; color: #1b1b1b; }}
</style>
</head>
<body>
<div id="root" data-composition-id="main" data-start="0" data-width="1080" data-height="1920" data-fps="30" data-duration="{end}">
  <div id="word"><span id="w0">hello</span><span id="w1" style="visibility:hidden;opacity:0;position:absolute;left:0;width:100%">world</span></div>
  <audio id="mix" src="assets/mix.wav" data-start="0" data-duration="{end}" data-volume="1"></audio>
</div>
<script>
(function () {{
  function build() {{
    window.__timelines = window.__timelines || {{}};
    var tl = gsap.timeline({{ paused: true }});
    tl.set("#w0", {{ autoAlpha: 0 }}, 1.1);
    tl.set("#w1", {{ autoAlpha: 1 }}, 1.1);
    tl.fromTo("#word", {{ y: 0 }}, {{ y: -40, duration: 1.8, ease: "none" }}, 0);
    KAEndCard.mount({{ tl: tl, root: document.getElementById("root"), start: {start},
                       brand: {{ line: "governed AI, shipped" }} }});
    tl.set({{}}, {{}}, {end});
    window.__timelines["main"] = tl;
  }}
  if (document.fonts && document.fonts.ready) {{ document.fonts.ready.then(build); }} else {{ build(); }}
}})();
</script>
</body>
</html>
"""


def build_and_render(base, tag, hold):
    hold_arg = "" if hold is None else f", hold: {hold}, allowShortHold: true"
    plan = node_eval(f"K.plan({{lastWordEnd: {LAST_WORD_END}{hold_arg}}})")
    assert plan["ok"], plan
    plan = plan["v"]
    BUILD = Path(base)
    proj = BUILD / f"render-{tag}" / "project"
    for sub in ("kit", "assets/fonts", "assets/brand"):
        (proj / sub).mkdir(parents=True, exist_ok=True)
    shutil.copy(GSAP_SRC, proj / "kit/gsap.min.js")
    shutil.copy(ENDCARD_JS, proj / "kit/endcard.js")
    shutil.copy(KIT / "fonts/LilitaOne.woff2", proj / "assets/fonts/LilitaOne.woff2")
    shutil.copy(KIT / "brand/keller-ai-logo.svg", proj / "assets/brand/keller-ai-logo.svg")
    # audio: a slice of the real look E stems run through the sfx bus
    scratch = BUILD / f"render-{tag}" / "audio"
    scratch.mkdir(parents=True, exist_ok=True)
    ff("-i", LOOK_E / "assets/narration.wav", "-t", plan["end"], scratch / "voice.wav")
    ff("-ss", "1.4", "-i", LOOK_E / "assets/ui_sfx.wav", "-t", plan["end"], scratch / "sfx.wav")
    from kit import sfx_bus
    bus = sfx_bus.build(str(scratch / "voice.wav"), [str(scratch / "sfx.wav")], str(scratch / "bus"))
    shutil.copy(bus["mix"], proj / "assets/mix.wav")
    fontface = node_eval("K.fontFaceCSS()")["v"]
    (proj / "index.html").write_text(PAGE.format(tag=tag, fontface=fontface,
                                                 start=plan["start"], end=plan["end"]))
    words = BUILD / f"render-{tag}" / "words.json"
    words.write_text(json.dumps(WORDS))
    out = BUILD / f"render-{tag}" / f"endcard-{tag}.mp4"
    # kit render form: cd <project> && npm_config_offline=true sandbox-exec -f offline.sb npx ...
    cmd = ["sandbox-exec", "-f", str(SANDBOX), "npx", "hyperframes@0.8.183", "render", ".",
           "-q", "draft", "-w", "2", "-o", str(out)]
    env = {**os.environ, "npm_config_offline": "true"}
    r = subprocess.run(cmd, cwd=proj, env=env, capture_output=True, text=True, timeout=900)
    (BUILD / f"render-{tag}" / "render.log").write_text(r.stdout + "\n--- stderr ---\n" + r.stderr)
    assert r.returncode == 0, r.stderr[-2000:]
    assert out.is_file()
    return {"plan": plan, "video": out, "words": words, "log": r.stdout + r.stderr, "bus": bus}


@pytest.fixture(scope="module")
def render_hold2(tmp_path_factory):
    return build_and_render(tmp_path_factory.mktemp("kit-render"), "hold2", None)  # DEFAULT_HOLD


@pytest.fixture(scope="module")
def render_hold08(tmp_path_factory):
    return build_and_render(tmp_path_factory.mktemp("kit-render"), "hold08", 0.8)


def endcard_cli(rend, *extra):
    r = run([sys.executable, ENDCARD_CLI, rend["video"], rend["words"], "--json", *extra])
    return r.returncode, json.loads(r.stdout), r


def test_render_offline_no_google_fonts(render_hold2):
    assert "fonts.googleapis" not in render_hold2["log"]
    assert "Google Fonts" not in render_hold2["log"]


def test_endcard_render_passes_item4_scene(render_hold2):
    code, res, raw = endcard_cli(render_hold2)
    assert code == 0, raw.stdout
    m = res["metrics"]
    assert m["method"] == "scene"
    assert m["end_card_start"] == pytest.approx(render_hold2["plan"]["start"], abs=1.5 / FPS)
    assert m["hold_after_last_word"] == pytest.approx(2.0, abs=1.5 / FPS)
    assert m["duration"] == pytest.approx(render_hold2["plan"]["end"], abs=1.5 / FPS)


def test_endcard_render_passes_item4_explicit(render_hold2):
    code, res, raw = endcard_cli(render_hold2, "--end-card-start", str(render_hold2["plan"]["start"]))
    assert code == 0, raw.stdout
    assert res["metrics"]["method"] == "explicit"


def test_endcard_render_audio_passes_item5(render_hold2):
    r = run([sys.executable, LOUDNESS_CLI, render_hold2["video"], "--json"])
    res = json.loads(r.stdout)
    assert r.returncode == 0, r.stdout
    assert res["metrics"]["true_peak_dbtp"] <= -1.5


def test_planted_short_hold_fails_item4(render_hold08):  # planted negative
    code, res, raw = endcard_cli(render_hold08)
    assert code == 1, raw.stdout
    assert "too_short" in [f["kind"] for f in res["findings"]]
    assert res["metrics"]["hold_after_last_word"] == pytest.approx(0.8, abs=1.5 / FPS)
