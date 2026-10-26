"""Style E template (styles/E): instantiate with the S49 15 s inputs, render for real, gate
the render with every preflight (items 3-6) and the item 7 report.

Real runs only: build.py (ffmpeg + kit/sfx_bus), a sandboxed offline HyperFrames render,
and the preflight CLIs as subprocesses. Renders and scratch go under a pytest tmp dir; the
S49 inputs and the canonical dir come from SHORTS_STUDIO_DIR (tests skip without it). The render
is reused within one pytest session when index.html, the audio and the kit are unchanged (keyed by a hash); set
STYLE_E_FORCE_RENDER=1 to force a fresh one.
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
STYLE = PLUGIN / "styles" / "E"
BUILD_PY = STYLE / "build.py"
PRE = PLUGIN / "preflight"
EVIDENCE = PLUGIN / "evidence" / "evidence.py"

_STUDIO_DIR = os.environ.get("SHORTS_STUDIO_DIR")
STUDIO = Path(_STUDIO_DIR) if _STUDIO_DIR else None
if STUDIO is not None:
    INPUTS = STUDIO / "_kit/lookdev/inputs"
    WAV = INPUTS / "r6-S49-cut15.wav"
    WORDS = INPUTS / "r6-S49-cut15.words.json"
    CANON = STUDIO / "_kit/lookdev/r2-15s-E"
else:
    INPUTS = WAV = WORDS = CANON = None
STUDIO_REQUIRED = (WAV, WORDS, CANON)
SANDBOX = PLUGIN / "kit" / "offline.sb"
OUT = INST = VIDEO = RESULTS = None  # scratch, bound per module by _scratch()


@pytest.fixture(scope="module", autouse=True)
def _scratch(tmp_path_factory):
    global OUT, INST, VIDEO, RESULTS
    OUT = tmp_path_factory.mktemp("style-E")
    INST = OUT / "s49-cut15"
    VIDEO = INST / "look.mp4"
    RESULTS = INST / "preflight"


CANON_FILES = [] if CANON is None else [CANON / "project/index.html", CANON / "project/kit/gsap.min.js", CANON / "project/assets/narration.wav",
               CANON / "project/assets/ui_sfx.wav", CANON / "tools/timeline.js", CANON / "tools/cues.py",
               CANON / "sfx_gen.py", CANON / "notes.md", CANON / "look.mp4"]
UNLICENSED = ("Comic Sans", "Marker Felt", "Chalkduster", "SF Pro", "SF Mono")
SYSTEM_FACES = ("Avenir", "Didot", "Menlo", "local(")


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def run(cmd, **kw):
    return subprocess.run([str(c) for c in cmd], capture_output=True, text=True, **kw)


@pytest.fixture(scope="module")
def built():
    before = {str(p): sha(p) for p in CANON_FILES}
    r = run([sys.executable, BUILD_PY, "--wav", WAV, "--words", WORDS, "--out", INST, "--slug", "S49", "--json"],
            timeout=900)
    assert r.returncode == 0, r.stderr
    plan = json.loads(r.stdout)
    after = {str(p): sha(p) for p in CANON_FILES}
    return {"plan": plan, "canon_before": before, "canon_after": after}


@pytest.fixture(scope="module")
def rendered(built):
    proj = INST / "project"
    key = hashlib.sha256("".join(sha(proj / f) for f in (
        "index.html", "assets/voice_norm.wav", "assets/sfx_bus.wav", "kit/endcard.js", "kit/gsap.min.js",
        "assets/fonts/LilitaOne.woff2", "assets/fonts/ComicShanns.woff2", "assets/fonts/Virgil.woff2")).encode()).hexdigest()
    stamp = INST / "render.key"
    fresh = os.environ.get("STYLE_E_FORCE_RENDER") == "1"
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
    starts = {round(w["start"], 4) for w in words}
    names = ["fail", "slop", "leg1", "rig", "exactly", "press", "tension", "worse", "end"]
    a = [p["anchors"][n] for n in names]
    # anchors are word-aligned, strictly increasing and all before the end-card cut
    assert p["snapped"] is True
    assert all(t in starts for t in a), a
    assert all(y - x >= 0.6 for x, y in zip(a, a[1:])), a
    assert p["endcard"]["start"] - a[-1] >= 1.3
    # item 4 constraints, from the plan
    assert p["endcard"]["start"] >= lwe - 0.05
    assert p["duration"] - max(p["endcard"]["start"], lwe) >= 1.5
    T = p["T"]
    assert T["leg1"][0] < T["leg1"][1] <= T["ticks"][0] and T["ticks"] == sorted(T["ticks"])
    assert T["ticks"][-1] < T["press"] < T["leg2"][0] < T["checks"][0] and T["allPass"] < T["tension"]
    # every kinetic slot has text drawn from the narration
    spoken = " ".join(w["text"] for w in words).lower()
    for slot in ("fail", "slop", "rig", "worse", "green"):
        assert p["kinetic"][slot], slot
        for line in p["kinetic"][slot]:
            for tok in line.replace("!!", "").replace(".", "").split():
                assert tok.lower() in spoken, (slot, tok)
    assert len(p["acts"]) == 3 and all(act["rows"] for act in p["acts"])
    assert p["audio"]["bus_passed"] is True


def test_instantiated_html_has_no_placeholders_and_one_duration(built):
    html = (INST / "project/index.html").read_text()
    for tok in ("__E_DURATION__", "__E_VO_DURATION__", "__E_DATA__", "__E_TITLE__"):
        assert tok not in html
    dur = f'data-duration="{built["plan"]["duration"]:.6f}"'
    assert html.count(dur) == 2  # root + sfx <audio>; the voice slot is the narration's own length
    assert 'id="vo" src="assets/voice_norm.wav" data-start="0" data-duration="15.0' in html
    # D4: no unlicensed family anywhere, and no uncleared system face either (all bundled OFL)
    for bad in UNLICENSED + SYSTEM_FACES:
        assert bad not in html, bad
    for f in ("LilitaOne.woff2", "ComicShanns.woff2", "Virgil.woff2"):
        assert f'url("assets/fonts/{f}")' in html
        assert sha(INST / "project/assets/fonts" / f) == sha(PLUGIN / "kit/fonts" / f)
    # the shared kit is used, not a private copy
    assert sha(INST / "project/kit/endcard.js") == sha(PLUGIN / "kit/endcard.js")
    assert "KAEndCard.mount" in html


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


def test_build_rejects_too_short_narration(tmp_path):
    """Planted negative: 5 s of speech cannot carry E's nine beats."""
    words = [w for w in json.loads(WORDS.read_text()) if w["end"] <= 5.0]
    wj = tmp_path / "w.json"
    wj.write_text(json.dumps(words))
    r = run([sys.executable, BUILD_PY, "--wav", WAV, "--words", wj, "--out", tmp_path / "x"])
    assert r.returncode == 2
    assert "too short" in r.stderr


@pytest.mark.studio("_kit/lookdev/inputs/r6-S49.wav", "_kit/lookdev/inputs/r6-S49.words.json")
def test_template_is_script_agnostic(tmp_path):
    """A different narration (the full 27.87 s S49 voice) builds a different, valid plan."""
    full_words = INPUTS / "r6-S49.words.json"
    r = run([sys.executable, BUILD_PY, "--wav", INPUTS / "r6-S49.wav", "--words", full_words,
             "--out", tmp_path / "o", "--json"], timeout=900)
    assert r.returncode == 0, r.stderr
    p = json.loads(r.stdout)
    cut = json.loads((INST / "plan.json").read_text()) if (INST / "plan.json").is_file() else None
    words = json.loads(full_words.read_text())
    lwe = max(w["end"] for w in words)
    assert p["lastWordEnd"] == round(lwe, 4)
    assert p["endcard"]["start"] >= lwe - 0.05 and p["duration"] - max(p["endcard"]["start"], lwe) >= 1.5
    assert p["anchors"]["end"] > 20.0  # the beats stretch with the longer narration
    if cut is not None:
        assert p["kinetic"] != cut["kinetic"]
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


def test_legibility_at_kinetic_beats(rendered):
    """The slams are short: sample each beat (mid-slam and settled) and the end-card hold."""
    T = rendered["T"]
    at = []
    for k in ("fail", "worry", "slop", "rig", "exactly", "still", "worse", "end"):
        at += [T[k] + 0.05, T[k] + 0.35]
    at += [T["slopOut"] + 0.2, T["ecStart"] + 0.1, T["END"] - 0.1]
    d = preflight("legibility", [INST / "project", "--at", ",".join(f"{t:.3f}" for t in at)], "legibility_beats")
    assert d["passed"], d["findings"]


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
