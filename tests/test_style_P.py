"""Style P template (styles/P, VHS camcorder tape): instantiate with the S49 15 s inputs, render for
real, gate the render with every preflight (items 3-6) and the item 7 report.

Real runs only: build.py (ffmpeg + kit/sfx_bus), a sandboxed offline HyperFrames render, and the
preflight CLIs as subprocesses. Renders and scratch go under a pytest tmp dir;
the S49 inputs and the canonical dir come from SHORTS_STUDIO_DIR (tests skip without it). The render
is reused within one pytest session when index.html, the audio and the kit
end card are unchanged (keyed by a hash); set STYLE_P_FORCE_RENDER=1 to force a fresh one.

Planted negatives: words past the narration, a missing WAV, an unknown --content key, a narration
too short for five chapters, and an instance copy with the panel headers back at the canonical
32 px, which the legibility preflight must flag. A stricter positive: with the persistent date
stamp excluded, every sample's lower third is still filled by scene content.
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

PLUGIN = Path(__file__).resolve().parents[1]
STYLE = PLUGIN / "styles" / "P"
BUILD_PY = STYLE / "build.py"
PRE = PLUGIN / "preflight"
EVIDENCE = PLUGIN / "evidence" / "evidence.py"

_STUDIO_DIR = os.environ.get("SHORTS_STUDIO_DIR")
STUDIO = Path(_STUDIO_DIR) if _STUDIO_DIR else None
if STUDIO is not None:
    INPUTS = STUDIO / "_kit/lookdev/inputs"
    WAV = INPUTS / "r6-S49-cut15.wav"
    WORDS = INPUTS / "r6-S49-cut15.words.json"
    CANON = STUDIO / "_kit/lookdev/r7-S49-P"
else:
    INPUTS = WAV = WORDS = CANON = None
STUDIO_REQUIRED = (WAV, WORDS, CANON)
SANDBOX = PLUGIN / "kit" / "offline.sb"
OUT = INST = VIDEO = RESULTS = None  # scratch, bound per module by _scratch()


@pytest.fixture(scope="module", autouse=True)
def _scratch(tmp_path_factory):
    global OUT, INST, VIDEO, RESULTS
    OUT = tmp_path_factory.mktemp("style-P")
    INST = OUT / "s49-cut15"
    VIDEO = INST / "look.mp4"
    RESULTS = INST / "preflight"


UNLICENSED = ("Comic Sans", "Marker Felt", "Chalkduster", "SF Pro", "SF Mono", "Andale", "Avenir", "Futura")


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def run(cmd, **kw):
    return subprocess.run([str(c) for c in cmd], capture_output=True, text=True, **kw)


CANON_FILES = [] if CANON is None else [CANON / "project/index.html", CANON / "project/kit/gsap.min.js", CANON / "project/assets/narration.wav",
               CANON / "project/assets/sfx.wav", CANON / "sfx_gen.py", CANON / "notes.md", CANON / "look.mp4"]


@pytest.fixture(scope="module")
def built():
    before = {str(p): sha(p) for p in CANON_FILES}
    r = run([sys.executable, BUILD_PY, "--wav", WAV, "--words", WORDS, "--out", INST, "--slug", "S49", "--json"])
    assert r.returncode == 0, r.stderr
    plan = json.loads(r.stdout)
    after = {str(p): sha(p) for p in CANON_FILES}
    return {"plan": plan, "canon_before": before, "canon_after": after}


def render_key(proj):
    return hashlib.sha256((sha(proj / "index.html") + sha(proj / "assets/voice_norm.wav")
                           + sha(proj / "assets/sfx_bus.wav") + sha(proj / "kit/endcard.js")).encode()).hexdigest()


@pytest.fixture(scope="module")
def rendered(built):
    key = render_key(INST / "project")
    stamp = INST / "render.key"
    fresh = os.environ.get("STYLE_P_FORCE_RENDER") == "1"
    # ubs:ignore — key is a content hash, not a secret
    if fresh or not VIDEO.is_file() or not stamp.is_file() or stamp.read_text() != key:
        r = subprocess.run(
            ["sandbox-exec", "-f", str(SANDBOX), "npx", "hyperframes@0.8.183", "render",
             "project", "-w", "2", "-q", "delivery", "-o", "look.mp4"],
            cwd=INST, env={**os.environ, "npm_config_offline": "true"},
            capture_output=True, text=True, timeout=3600)
        (INST / "render.log").write_text(r.stdout + r.stderr)
        assert r.returncode == 0, (r.stdout + r.stderr)[-3000:]
        stamp.write_text(key)
    assert VIDEO.is_file()
    RESULTS.mkdir(parents=True, exist_ok=True)
    return built["plan"]


def preflight(name, args, out_name=None):
    r = run([sys.executable, PRE / f"{name}.py", *args, "--json"], timeout=1800)
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / f"{out_name or name}.json").write_text(r.stdout)
    assert r.returncode in (0, 1), f"{name} exit {r.returncode}: {r.stderr}"
    d = json.loads(r.stdout)
    assert d["name"] == name
    assert (r.returncode == 0) == d["passed"]
    return d


# ---------------------------------------------------------------- build / instantiation

def test_build_leaves_canonical_dir_untouched(built):
    assert built["canon_before"] == built["canon_after"]


def test_build_plan_shape(built):
    p = built["plan"]
    words = json.loads(WORDS.read_text())
    lwe = max(w["end"] for w in words)
    ec = p["endcard"]
    assert abs(ec["start"] - (lwe + 0.1)) < 1e-6
    assert p["duration"] == ec["end"] and p["duration"] - max(ec["start"], lwe) >= 1.5
    cuts = p["cuts"]
    # chapters at the S49 clause ends: the canonical's hand-set cuts were 0.6/4.4/7.0/9.95/12.95
    assert len(cuts) == 5 and cuts[0] == 0.6
    for got, canon in zip(cuts[1:], [4.4, 7.0, 9.95, 12.95]):
        assert abs(got - canon) <= 0.1, cuts
    spans = list(zip(cuts, cuts[1:] + [ec["start"]]))
    assert all(b - a >= 1.6 for a, b in spans)
    assert [c["first_word"] for c in p["chapters"][1:]] == ["It", "having", "The", "and"]
    assert p["ch1"]["title"]["text"] == "Effective Challenge" and p["ch1"]["title"]["t"] == 2.72
    assert p["ch2"]["head"] == ["A COMMON HABIT"] and p["ch2"]["sub"]["text"] == "IN AI SYSTEMS"
    assert p["ch4"]["slam"] == {"text": "NOT", "t": 11.88}
    assert (p["ch4"]["pre1"]["text"], p["ch4"]["pre2"]["text"], p["ch4"]["post"]["text"]) == ("THAT HABIT", "IS", "INDEPENDENCE")
    assert p["ch5"]["slam"]["text"] == "BANKING" and p["ch5"]["icon"] == "bank"
    # every cue lands inside its own chapter
    assert cuts[2] < p["ch3"]["magT"] < p["ch3"]["stampT"] < cuts[3]
    assert cuts[3] < p["ch4"]["slam"]["t"] < cuts[4] < p["ch5"]["slam"]["t"] < ec["start"]
    assert p["stopT"] < ec["start"]


def test_instance_html_durations_fonts_and_assets(built):
    p = built["plan"]
    proj = INST / "project"
    html = (proj / "index.html").read_text()
    assert f'data-duration="{p["duration"]:.6f}"' in html
    assert "__P_" not in html
    vo = re.search(r'id="vo"[^>]*data-duration="([0-9.]+)"', html)
    assert vo and float(vo.group(1)) <= p["duration"] + 1e-6
    for f in ("kit/gsap.min.js", "kit/endcard.js", "assets/fonts/LilitaOne.woff2", "assets/fonts/ComicShanns.woff2",
              "assets/fonts/Virgil.woff2", "assets/brand/keller-ai-logo.svg", "assets/voice_norm.wav", "assets/sfx_bus.wav"):
        assert (proj / f).is_file(), f
    assert sha(proj / "kit/endcard.js") == sha(PLUGIN / "kit/endcard.js")
    # D4: no system/unlicensed faces anywhere in the template or the instance; only bundled url() faces
    for doc in (html, (STYLE / "template/index.html").read_text()):
        css = doc.split("<style>", 1)[1].split("</style>", 1)[0]
        assert "local(" not in css
        for name in UNLICENSED:
            assert name not in css, name
        faces = re.findall(r'@font-face\s*\{[^}]*\}', css)
        assert len(faces) == 3 and all("url(\"assets/fonts/" in f for f in faces)


def test_build_rejects_words_past_narration(tmp_path):
    words = json.loads(WORDS.read_text())
    words.append({"id": "wX", "text": "late", "start": 15.5, "end": 15.9})
    wj = tmp_path / "w.json"
    wj.write_text(json.dumps(words))
    r = run([sys.executable, BUILD_PY, "--wav", WAV, "--words", wj, "--out", tmp_path / "o"])
    assert r.returncode == 2
    assert "after the narration" in r.stderr


def test_build_rejects_missing_wav_and_bad_content(tmp_path):
    r = run([sys.executable, BUILD_PY, "--wav", tmp_path / "nope.wav", "--words", WORDS, "--out", tmp_path / "x"])
    assert r.returncode == 2 and "wav not found" in r.stderr
    cj = tmp_path / "c.json"
    cj.write_text(json.dumps({"bogus": 1}))
    r = run([sys.executable, BUILD_PY, "--wav", WAV, "--words", WORDS, "--out", tmp_path / "y", "--content", cj])
    assert r.returncode == 2 and "unknown keys" in r.stderr


def test_build_rejects_too_short_narration(tmp_path):
    words = [w for w in json.loads(WORDS.read_text()) if w["end"] <= 4.4]
    wj = tmp_path / "w.json"
    wj.write_text(json.dumps(words))
    r = run([sys.executable, BUILD_PY, "--wav", WAV, "--words", wj, "--out", tmp_path / "o", "--duration", "4.5"])
    assert r.returncode == 2 and "too short" in r.stderr


@pytest.mark.studio("_kit/lookdev/inputs/r6-S49.wav", "_kit/lookdev/inputs/r6-S49.words.json")
def test_template_is_script_agnostic(tmp_path):
    """A different narration (the full S49 words, first ~20 s) builds a different tape."""
    words = [w for w in json.loads((INPUTS / "r6-S49.words.json").read_text()) if w["end"] <= 20.0]
    wj = tmp_path / "w.json"
    wj.write_text(json.dumps(words))
    r = run([sys.executable, BUILD_PY, "--wav", INPUTS / "r6-S49.wav", "--words", wj, "--out", tmp_path / "o",
             "--duration", "20.0", "--json"])
    assert r.returncode == 0, r.stderr
    p = json.loads(r.stdout)
    lwe = max(w["end"] for w in words)
    assert p["lastWordEnd"] == round(lwe, 4)
    assert p["ch5"]["slam"]["text"] != "BANKING"
    starts = {round(w["start"], 4) for w in words}
    for ch in ("ch4", "ch5"):
        assert p[ch]["slam"]["t"] in starts or p[ch]["slam"]["t"] > p["cuts"][-1]
    assert all(b - a >= 1.6 for a, b in zip(p["cuts"], p["cuts"][1:] + [p["endcard"]["start"]]))
    pr = run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
              tmp_path / "o/project/assets/voice_norm.wav"])
    assert abs(float(pr.stdout) - min(20.0, p["endcard"]["start"])) < 0.02
    assert f'data-duration="{p["duration"]:.6f}"' in (tmp_path / "o/project/index.html").read_text()


def test_legibility_catches_the_canonical_small_panel_header(built, tmp_path):
    """Planted negative: a copy of the instance with the panel headers back at the canonical 32 px
    must be flagged as small_type by the real legibility preflight while the counter panel shows."""
    proj = tmp_path / "project"
    shutil.copytree(INST / "project", proj)
    html = (proj / "index.html").read_text()
    needle = 'font-family:"KS Shanns"; font-size:48px; letter-spacing:3px; line-height:72px;'
    assert html.count(needle) == 1
    (proj / "index.html").write_text(html.replace(needle, 'font-family:"KS Shanns"; font-size:32px; letter-spacing:3px; line-height:72px;'))
    t = built["plan"]["cuts"][1] + 1.2
    r = run([sys.executable, PRE / "legibility.py", proj, "--at", f"{t:.2f}", "--json"], timeout=600)
    assert r.returncode == 1, r.stderr
    d = json.loads(r.stdout)
    assert any(f["kind"] == "small_type" and "REPEAT COUNTER" in f["detail"] for f in d["findings"]), d["findings"]


def test_lower_third_filled_without_the_date_stamp(built, tmp_path):
    """Stricter than the gate: the persistent date stamp alone would fill the lower third, so it is
    excluded here and every sample must still be filled by scene content."""
    proj = tmp_path / "project"
    shutil.copytree(INST / "project", proj)
    html = (proj / "index.html").read_text()
    needle = '<div class="osd" id="date" style='
    assert html.count(needle) == 1
    (proj / "index.html").write_text(html.replace(needle, '<div class="osd" id="date" data-legibility="ignore" style='))
    r = run([sys.executable, PRE / "legibility.py", proj, "--samples", "51", "--json"], timeout=900)
    d = json.loads(r.stdout)
    assert r.returncode == 0, d["findings"]
    assert d["metrics"]["empty_lower_third_samples"] == [] and d["metrics"]["indeterminate_lower_third_samples"] == []


# ---------------------------------------------------------------- render + preflight

def test_dead_frames(rendered):
    d = preflight("dead_frames", [VIDEO])
    assert d["passed"], d["findings"]


def test_endcard_scene_method(rendered):
    d = preflight("endcard", [VIDEO, WORDS])
    assert d["passed"], d["findings"]
    assert abs(d["metrics"]["end_card_start"] - rendered["endcard"]["start"]) <= 0.1


def test_endcard_explicit_start(rendered):
    d = preflight("endcard", [VIDEO, WORDS, "--end-card-start", rendered["endcard"]["start"]], "endcard_explicit")
    assert d["passed"], d["findings"]


def test_loudness_delivered_mix(rendered):
    d = preflight("loudness", [VIDEO], "loudness_mix")
    assert d["passed"], d["findings"]


def test_loudness_stems(rendered):
    a = INST / "project/assets"
    d = preflight("loudness", [VIDEO, "--voice", a / "voice_norm.wav", "--sfx", a / "sfx_bus.wav"], "loudness_stems")
    assert d["passed"], d["findings"]
    assert isinstance(d["metrics"].get("sfx_under_voice_db"), float)


def test_legibility(rendered):
    d = preflight("legibility", [INST / "project", "--samples", "51"])
    assert d["passed"], d["findings"]
    assert d["metrics"]["page_errors"] == []
    assert d["metrics"]["empty_lower_third_samples"] == [] and d["metrics"]["indeterminate_lower_third_samples"] == []


def test_evidence_report_all_checks(rendered):
    out = INST / "evidence"
    a = INST / "project/assets"
    r = run([sys.executable, EVIDENCE, "report", VIDEO, "--out", out, "--words", WORDS,
             "--run", "dead_frames {video}",
             "--run", f"endcard {{video}} {WORDS}",
             "--run", f"loudness {{video}} --voice {a / 'voice_norm.wav'} --sfx {a / 'sfx_bus.wav'}",
             "--run", f"legibility {INST / 'project'}"], timeout=3600)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-3000:]
    rep = json.loads((out / "preflight_report.json").read_text())
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "evidence_summary.json").write_text(json.dumps(
        {"passed": rep["passed"], "checks": [{"name": c["name"], "passed": c["passed"]} for c in rep["checks"]]}))
    assert rep["passed"] is True
    assert sorted(c["name"] for c in rep["checks"]) == ["dead_frames", "endcard", "legibility", "loudness"]
    assert all(c["passed"] for c in rep["checks"])
    assert rep["sheets"]
