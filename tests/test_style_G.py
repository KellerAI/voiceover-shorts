"""Style G template (styles/G): instantiate with the S49 15 s inputs, render for real, gate
the render with every preflight (items 3-6) and the item 7 report.

Real runs only: build.py (ffmpeg + kit/sfx_bus), a sandboxed offline HyperFrames render,
and the preflight CLIs as subprocesses. Renders and scratch go under a pytest tmp dir; the
S49 inputs and the canonical dir come from SHORTS_STUDIO_DIR (tests skip without it). The render
is reused within one pytest session when index.html and the audio are unchanged (keyed by a hash), so set
STYLE_G_FORCE_RENDER=1 to force a fresh one.
"""
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PLUGIN = Path(__file__).resolve().parents[1]
STYLE = PLUGIN / "styles" / "G"
BUILD_PY = STYLE / "build.py"
PRE = PLUGIN / "preflight"
EVIDENCE = PLUGIN / "evidence" / "evidence.py"

_STUDIO_DIR = os.environ.get("SHORTS_STUDIO_DIR")
STUDIO = Path(_STUDIO_DIR) if _STUDIO_DIR else None
if STUDIO is not None:
    INPUTS = STUDIO / "_kit/lookdev/inputs"
    WAV = INPUTS / "r6-S49-cut15.wav"
    WORDS = INPUTS / "r6-S49-cut15.words.json"
    CANON = STUDIO / "_kit/lookdev/r2-15s-G"
else:
    INPUTS = WAV = WORDS = CANON = None
STUDIO_REQUIRED = (WAV, WORDS, CANON)
SANDBOX = PLUGIN / "kit" / "offline.sb"
OUT = INST = VIDEO = RESULTS = None  # scratch, bound per module by _scratch()


@pytest.fixture(scope="module", autouse=True)
def _scratch(tmp_path_factory):
    global OUT, INST, VIDEO, RESULTS
    OUT = tmp_path_factory.mktemp("style-G")
    INST = OUT / "s49-cut15"
    VIDEO = INST / "look.mp4"
    RESULTS = INST / "preflight"

D4_UNLICENSED = ("Comic Sans", "ComicSans", "Marker Felt", "MarkerFelt", "Chalkduster", "SF Pro", "SF Mono")


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def run(cmd, **kw):
    return subprocess.run([str(c) for c in cmd], capture_output=True, text=True, **kw)


CANON_FILES = [] if CANON is None else [CANON / "project/index.html", CANON / "project/kit/gsap.min.js", CANON / "project/assets/sfx.wav",
               CANON / "project/assets/narration.wav", CANON / "project/assets/logo.svg", CANON / "sfx_gen.py",
               CANON / "notes.md", CANON / "look.mp4"]


@pytest.fixture(scope="module")
def built():
    before = {str(p): sha(p) for p in CANON_FILES}
    r = run([sys.executable, BUILD_PY, "--wav", WAV, "--words", WORDS, "--out", INST, "--slug", "S49", "--json"])
    assert r.returncode == 0, r.stderr
    plan = json.loads(r.stdout)
    after = {str(p): sha(p) for p in CANON_FILES}
    return {"plan": plan, "canon_before": before, "canon_after": after}


@pytest.fixture(scope="module")
def rendered(built):
    proj = INST / "project"
    key = hashlib.sha256((sha(proj / "index.html") + sha(proj / "assets/voice_norm.wav")
                          + sha(proj / "assets/sfx_bus.wav") + sha(proj / "kit/endcard.js")).encode()).hexdigest()
    stamp = INST / "render.key"
    fresh = os.environ.get("STYLE_G_FORCE_RENDER") == "1"
    # ubs:ignore — key is a content hash, not a secret
    if fresh or not VIDEO.is_file() or not stamp.is_file() or stamp.read_text() != key:
        r = subprocess.run(["sandbox-exec", "-f", str(SANDBOX), "npx", "hyperframes@0.8.183", "render",
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
    B = p["bounds"]
    assert len(B) == 7 and B[0] == 0.0
    assert all(b - a >= 0.9 for a, b in zip(B, B[1:]))
    assert p["endcard"]["start"] >= lwe - 0.05
    assert p["duration"] - max(p["endcard"]["start"], lwe) >= 1.5
    assert abs(B[6] - p["endcard"]["start"]) < 1e-6
    assert len(p["pages"]) == 6
    # every spoken word lands on exactly one page, and every page has a headline
    assert sorted(i for pg in p["pages"] for i in pg["idx"]) == list(range(len(words)))
    assert all(pg["hl"] for pg in p["pages"])
    # page devices sit inside their own pages
    assert B[1] <= p["ransom2"]["t0"] < B[2]
    assert all(B[2] < t < B[3] for t in p["ticks"]) and len(p["ticks"]) == 6
    assert all(B[3] < t < B[4] for t in p["stamps"]) and p["stamps"] == sorted(p["stamps"])
    assert B[4] <= p["ransom5"]["t0"] < B[5]
    assert 1 <= len(p["cells"]["text"]) <= 9
    assert p["audio"]["bus_passed"] is True


def test_instantiated_html_has_no_placeholders_and_one_duration(built):
    html = (INST / "project/index.html").read_text()
    for tok in ("__G_DURATION__", "__G_VO_DURATION__", "__G_DATA__", "__G_TITLE__"):
        assert tok not in html
    dur = f'data-duration="{built["plan"]["duration"]:.6f}"'
    assert html.count(dur) == 2  # root + sfx <audio>; the voice slot is the narration length
    assert 'id="vo" src="assets/voice_norm.wav" data-start="0" data-duration="15.0' in html
    # D4: no unlicensed font family is named anywhere in the composition; OFL faces are bundled
    for bad in D4_UNLICENSED:
        assert bad not in html
    for f in ("LilitaOne.woff2", "ComicShanns.woff2", "Virgil.woff2"):
        assert f'url("assets/fonts/{f}")' in html
        assert sha(INST / "project/assets/fonts" / f) == sha(PLUGIN / "kit/fonts" / f)
    # the shared kit is used, not a private copy
    assert sha(INST / "project/kit/endcard.js") == sha(PLUGIN / "kit/endcard.js")
    assert sha(INST / "project/assets/brand/keller-ai-logo.svg") == sha(PLUGIN / "kit/brand/keller-ai-logo.svg")
    assert "KAEndCard.mount" in html


def test_template_source_has_no_unlicensed_fonts():
    src = (STYLE / "template/index.html").read_text()
    for bad in D4_UNLICENSED:
        assert bad not in src


def test_build_rejects_words_past_the_narration(tmp_path):
    """Planted negative: a word that ends after --duration must fail with exit 2."""
    words = json.loads(WORDS.read_text())
    r = run([sys.executable, BUILD_PY, "--wav", WAV, "--words", WORDS, "--out", tmp_path / "x",
             "--duration", str(max(w["end"] for w in words) - 1.0)])
    assert r.returncode == 2
    assert "after the narration" in r.stderr


def test_build_rejects_missing_wav(tmp_path):
    r = run([sys.executable, BUILD_PY, "--wav", tmp_path / "nope.wav", "--words", WORDS, "--out", tmp_path / "x"])
    assert r.returncode == 2
    assert "wav not found" in r.stderr


@pytest.mark.studio("_kit/lookdev/inputs/r6-S49.wav", "_kit/lookdev/inputs/r6-S49.words.json")
def test_template_is_script_agnostic(tmp_path):
    """A different narration (the full S49 words, first ~20 s) builds a different plan."""
    words = [w for w in json.loads((INPUTS / "r6-S49.words.json").read_text()) if w["end"] <= 20.0]
    wj = tmp_path / "w.json"
    wj.write_text(json.dumps(words))
    brand = tmp_path / "brand.json"
    brand.write_text(json.dumps({"masthead": "The Brief", "line": "Field notes"}))
    r = run([sys.executable, BUILD_PY, "--wav", INPUTS / "r6-S49.wav", "--words", wj, "--out", tmp_path / "o",
             "--duration", "20.0", "--brand", brand, "--json"])
    assert r.returncode == 0, r.stderr
    p = json.loads(r.stdout)
    assert p["lastWordEnd"] == round(max(w["end"] for w in words), 4)
    assert p["bounds"][6] == p["endcard"]["start"]
    assert p["masthead"] == "The Brief" and p["brand"] == {"line": "Field notes"}
    assert len(p["words"]) == len(words)
    assert p["pages"][5]["text"] != "and it borrows a fix from banking."  # not the S49 cut's plan
    pr = run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
              tmp_path / "o/project/assets/voice_norm.wav"])
    assert abs(float(pr.stdout) - min(20.0, p["endcard"]["start"])) < 0.02
    html = (tmp_path / "o/project/index.html").read_text()
    assert f'data-duration="{p["duration"]:.6f}"' in html


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
    (RESULTS / "evidence_summary.json").write_text(json.dumps(
        {"passed": rep["passed"], "checks": [{"name": c["name"], "passed": c["passed"]} for c in rep["checks"]]}))
    assert rep["passed"] is True
    assert sorted(c["name"] for c in rep["checks"]) == ["dead_frames", "endcard", "legibility", "loudness"]
    assert all(c["passed"] for c in rep["checks"])
    assert rep["sheets"]
