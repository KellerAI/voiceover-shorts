"""Style A template (styles/A): instantiate with the S49 15 s inputs, render for real, gate on every preflight.

Real runs only: styles/A/build.py builds the project (kit sfx bus, kit end card, bundled OFL fonts),
HyperFrames 0.8.183 renders it offline under the sandbox (-w 2), and each preflight CLI (items 3-6)
plus the item 7 evidence report runs on the output. Build and render scratch live under a pytest
tmp dir. No preview server is used. The S49 inputs and the canonical dir-A come from
SHORTS_STUDIO_DIR; tests that need them skip without it (see conftest.py).
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import importlib.util  # noqa: E402

# load styles/A/build.py under a unique module name (other style tests also have a build.py)
_spec = importlib.util.spec_from_file_location(
    "style_a_build", Path(__file__).resolve().parents[1] / "styles" / "A" / "build.py")
if _spec is None or _spec.loader is None:
    raise ImportError("cannot load styles/A/build.py: no import spec or loader")
style_a = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(style_a)

PLUGIN = Path(__file__).resolve().parents[1]
PRE = PLUGIN / "preflight"
EVIDENCE = PLUGIN / "evidence" / "evidence.py"
_STUDIO_DIR = os.environ.get("SHORTS_STUDIO_DIR")
STUDIO = Path(_STUDIO_DIR) if _STUDIO_DIR else None
if STUDIO is not None:
    INPUTS = STUDIO / "_kit/lookdev/inputs"
    WAV = INPUTS / "r6-S49-cut15.wav"
    WORDS = INPUTS / "r6-S49-cut15.words.json"
    CANON = STUDIO / "_kit/lookdev/dir-A/index.html"
else:
    INPUTS = WAV = WORDS = CANON = None
STUDIO_REQUIRED = (WAV, WORDS, CANON)
BUILD = OUT = None  # scratch, bound per module by _scratch()
UNLICENSED = ["Comic Sans", "Marker Felt", "Chalkduster", "SF Pro", "SF Mono"]
BRAND = {"kicker": "S49 · Effective Challenge", "line": "Effective Challenge"}


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def cli(script, *args):
    r = subprocess.run([sys.executable, str(script), *map(str, args), "--json"],
                       capture_output=True, text=True, timeout=1800)
    (OUT / "preflight").mkdir(parents=True, exist_ok=True)
    (OUT / "preflight" / f"{Path(script).stem}.json").write_text(r.stdout)
    assert r.returncode in (0, 1), f"{script} exit {r.returncode}: {r.stderr[-1500:]}"
    return r.returncode, json.loads(r.stdout)


@pytest.fixture(scope="module", autouse=True)
def _scratch(tmp_path_factory):
    global BUILD, OUT
    BUILD = tmp_path_factory.mktemp("style-A")
    OUT = BUILD / "run"


@pytest.fixture(scope="module")
def built():
    canon_before = sha(CANON)
    OUT.mkdir(parents=True, exist_ok=True)
    plan = style_a.build(WAV, WORDS, OUT, brand=BRAND)
    video = style_a.render(OUT, quality="delivery")
    assert sha(CANON) == canon_before, "canonical dir-A/index.html changed"
    return {"plan": plan, "video": video, "project": OUT / "project", "words": OUT / "words.json"}


# ---------------------------------------------------------------- template contract (no render)

@pytest.mark.studio
def test_phrase_plan_covers_every_word_once():
    words = style_a.load_words(WORDS)
    ph = style_a.plan_phrases(words)
    covered = [i for p in ph for i in range(p["i0"], p["i1"] + 1)]
    assert covered == list(range(len(words)))
    assert ph[0]["kind"] == 0 and all(1 <= p["kind"] <= 5 for p in ph[1:])
    assert all(p["i0"] <= p["hero"] <= p["i1"] for p in ph)
    assert ph[-1]["exit"] is None and all(p["exit"] is not None for p in ph[:-1])
    assert style_a.plan_phrases(words) == ph  # deterministic


@pytest.mark.studio
def test_short_end_card_hold_is_refused(tmp_path):
    """Planted negative: a --duration that leaves < 1.5 s after the last word must not build."""
    last = max(w[2] for w in style_a.load_words(WORDS))
    with pytest.raises(style_a.BuildError):
        style_a.build(WAV, WORDS, tmp_path / "neg-short-hold", duration=last + 0.1 + 0.8)


def test_project_uses_only_bundled_ofl_fonts(built):
    html = (built["project"] / "index.html").read_text()
    faces = re.findall(r"@font-face\s*{[^}]*}", html)
    assert faces and all("url(\"assets/fonts/" in f for f in faces)
    assert "local(" not in html and "googleapis" not in html
    for name in UNLICENSED + ["Avenir", "Didot", "Menlo"]:
        assert name not in html, name
    for f in ("LilitaOne.woff2", "Virgil.woff2", "ComicShanns.woff2"):
        assert sha(built["project"] / "assets/fonts" / f) == sha(PLUGIN / "kit/fonts" / f)


def test_durations_follow_kit_endcard_plan(built):
    plan = built["plan"]
    html = (built["project"] / "index.html").read_text()
    d = re.findall(r'data-duration="([0-9.]+)"', html)
    assert len(d) == 2 and all(abs(float(x) - plan["endcard"]["end"]) < 1e-6 for x in d)
    assert plan["endcard"]["start"] >= plan["last_word_end"]
    assert plan["endcard"]["end"] - plan["endcard"]["start"] >= 1.5
    log = (OUT / "render.log").read_text()
    assert "fonts.googleapis" not in log and "Google Fonts" not in log


# ---------------------------------------------------------------- preflight gates on the real render

def test_dead_frames_pass(built):
    code, res = cli(PRE / "dead_frames.py", built["video"])
    assert code == 0 and res["passed"], res["findings"]


def test_endcard_pass_scene_detected(built):
    code, res = cli(PRE / "endcard.py", built["video"], built["words"])
    assert code == 0 and res["passed"], res["findings"]
    assert res["metrics"]["method"] == "scene"
    assert abs(res["metrics"]["end_card_start"] - built["plan"]["endcard"]["start"]) <= 2 / 30


def test_loudness_pass_on_render_audio(built):
    p = built["plan"]["paths"]
    code, res = cli(PRE / "loudness.py", built["video"], "--voice", p["voice_norm"], "--sfx", p["sfx_bus"])
    assert code == 0 and res["passed"], res["findings"]
    assert res["metrics"]["true_peak_source"] and "sfx_under_voice_db" in res["metrics"]


def test_legibility_pass(built):
    code, res = cli(PRE / "legibility.py", built["project"])
    assert code == 0 and res["passed"], res["findings"]
    assert res["metrics"]["page_errors"] == []


def test_legibility_catches_canonical_hud_size(built):
    """Planted negative: put the canonical 22 px HUD timecode back; legibility must fail small_type."""
    neg = BUILD / "neg-small-hud" / "project"
    if neg.exists():
        shutil.copytree(built["project"], neg, dirs_exist_ok=True)
    else:
        shutil.copytree(built["project"], neg)
    html = (built["project"] / "index.html").read_text()
    old = "#tc { position: absolute; right: 84px; top: 82px; font-size: 44px;"
    assert old in html
    (neg / "index.html").write_text(html.replace(old, old.replace("44px;", "22px;")))
    r = subprocess.run([sys.executable, str(PRE / "legibility.py"), str(neg), "--json"],
                       capture_output=True, text=True, timeout=900)
    res = json.loads(r.stdout)
    assert r.returncode == 1 and not res["passed"]
    assert any(f["kind"] == "small_type" for f in res["findings"])


def test_evidence_report_all_checks_pass(built):
    p = built["plan"]["paths"]
    rep = OUT / "evidence"
    r = subprocess.run([sys.executable, str(EVIDENCE), "report", str(built["video"]), "--out", str(rep),
                        "--words", str(built["words"]),
                        "--run", "dead_frames {video}",
                        "--run", f"endcard {{video}} {built['words']}",
                        "--run", f"loudness {{video}} --voice {p['voice_norm']} --sfx {p['sfx_bus']}",
                        "--run", f"legibility {built['project']}"],
                       capture_output=True, text=True, timeout=3600)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    report = json.loads((rep / "preflight_report.json").read_text())
    assert report["passed"] is True
    assert sorted(c["name"] for c in report["checks"]) == ["dead_frames", "endcard", "legibility", "loudness"]
    assert all(c["passed"] for c in report["checks"])
    assert report["sheets"]
