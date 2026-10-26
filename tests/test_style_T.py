"""Style T template (styles/T, dot-matrix printout): instantiate with the S49 15 s inputs, render for
real, gate the render with every preflight (items 3-6) and the item 7 report.

Real runs only: build.py (ffmpeg + kit/sfx_bus), a sandboxed offline HyperFrames render, and the
preflight CLIs as subprocesses. Renders and scratch go under a pytest tmp dir; the S49 inputs and
the canonical dir come from SHORTS_STUDIO_DIR (tests skip without it). Scratch dirs for negatives
are fresh, uniquely named dirs there, and only the named project files are copied (no tree
copies). The render is reused within one pytest session when index.html, the audio and the kit
end card are unchanged (keyed by a hash); set STYLE_T_FORCE_RENDER=1 to force a fresh one.

Planted negatives: words past the narration, a missing WAV, an unknown --content key, a narration
too short for a printout, a --content title that is not spoken, and an instance copy with the
printer label back at the canonical 30 px, which the legibility preflight must flag. The printed
lines are canvas dots that the DOM legibility preflight cannot see, so the plan's dot pitches are
checked here against the same 44 px floor.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PLUGIN = Path(__file__).resolve().parents[1]
STYLE = PLUGIN / "styles" / "T"
BUILD_PY = STYLE / "build.py"
PRE = PLUGIN / "preflight"
EVIDENCE = PLUGIN / "evidence" / "evidence.py"

_STUDIO_DIR = os.environ.get("SHORTS_STUDIO_DIR")
STUDIO = Path(_STUDIO_DIR) if _STUDIO_DIR else None
if STUDIO is not None:
    INPUTS = STUDIO / "_kit/lookdev/inputs"
    WAV = INPUTS / "r6-S49-cut15.wav"
    WORDS = INPUTS / "r6-S49-cut15.words.json"
    CANON = STUDIO / "_kit/lookdev/r7-S49-T"
else:
    INPUTS = WAV = WORDS = CANON = None
STUDIO_REQUIRED = (WAV, WORDS, CANON)
SANDBOX = PLUGIN / "kit" / "offline.sb"
OUT = INST = VIDEO = RESULTS = None  # scratch, bound per module by _scratch()


@pytest.fixture(scope="module", autouse=True)
def _scratch(tmp_path_factory):
    global OUT, INST, VIDEO, RESULTS
    OUT = tmp_path_factory.mktemp("style-T")
    INST = OUT / "s49-cut15"
    VIDEO = INST / "look.mp4"
    RESULTS = INST / "preflight"


UNLICENSED = ("Comic Sans", "Marker Felt", "Chalkduster", "SF Pro", "SF Mono", "Helvetica", "Arial", "Andale",
              "Avenir", "Futura")
PROJECT_FILES = ["index.html", "kit/gsap.min.js", "kit/endcard.js", "assets/fonts/LilitaOne.woff2",
                 "assets/fonts/ComicShanns.woff2", "assets/brand/keller-ai-logo.svg", "assets/voice_norm.wav",
                 "assets/sfx_bus.wav"]


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def run(cmd, **kw):
    return subprocess.run([str(c) for c in cmd], capture_output=True, text=True, **kw)


def scratch(tag):
    d = OUT / f"{tag}-{uuid.uuid4().hex[:10]}"
    d.mkdir(parents=True)
    return d


def copy_project(dst):
    """Copy only the named instance files (no tree copy, no .hyperframes cache)."""
    for f in PROJECT_FILES:
        (dst / f).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(INST / "project" / f, dst / f)
    return dst


CANON_FILES = [] if CANON is None else [CANON / "project/index.html", CANON / "project/kit/gsap.min.js", CANON / "project/assets/narration.wav",
               CANON / "project/assets/sfx.wav", CANON / "project/assets/ComicShanns.woff2", CANON / "sfx_gen.py",
               CANON / "notes.md", CANON / "look.mp4"]


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
    fresh = os.environ.get("STYLE_T_FORCE_RENDER") == "1"
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
    starts = {round(w["start"], 4) for w in words}
    ec = p["endcard"]
    assert abs(ec["start"] - (lwe + 0.1)) < 1e-6
    assert p["duration"] == ec["end"] and p["duration"] - max(ec["start"], lwe) >= 1.5
    assert [b["text"] for b in p["banners"]] == ["EFFECTIVE", "CHALLENGE"]
    lines = [L for L in p["items"] if L["kind"] == "t"]
    by_role = {r: [L["text"] for L in lines if L["role"] == r] for r in ("title", "verdict", "final")}
    assert by_role == {"title": ["EFFECTIVE", "CHALLENGE."], "verdict": ["NOT INDEPENDENCE"], "final": ["FROM BANKING."]}
    assert lines[0]["text"] == "THIS SHORT IS" and lines[0]["t"] == 0.08
    assert "KELLER AI BRIEF" in [L["text"] for L in lines]          # CamelCase KellerAI split for the bitmap
    for L in lines:
        assert L["t"] in starts                                      # every line prints on a spoken word
        assert TX_FITS(L)
    ts = [L["t"] for L in p["items"]]
    assert ts == sorted(ts)
    for a, b in zip(p["items"], p["items"][1:]):                     # each print finishes before the next feed
        assert a["t"] + a["dur"] <= b["t"] - 0.15 + 1e-6, (a, b)
    gag = [L for L in p["items"] if L["kind"] == "gag"]
    assert len(gag) == 1 and 0.4 * lwe <= gag[0]["t"] <= 0.75 * lwe
    verdict = next(L for L in lines if L["role"] == "verdict")
    assert p["voidT"] == round(verdict["t"] + 0.04, 4) and verdict["t"] == 11.88
    last = p["items"][-1]
    assert last["t"] + last["dur"] <= p["feedT"] < p["tearT"] < ec["start"]
    assert abs(p["tearT"] + p["tearDur"] - ec["start"]) < 1e-3


def TX_FITS(L):
    """The printed line stays left of the right sprocket strip (x = 910) at its pitch."""
    return 84 + (6 * len(L["text"]) - 1) * L["px"] <= 910 + 1e-6


def test_canvas_type_clears_the_size_floor(built):
    """Printed lines are canvas dots (invisible to the DOM legibility preflight): their glyph
    height 7 * pitch * rows must clear the same 44 px floor; the canonical body note was ~40 px."""
    p = built["plan"]
    for L in p["items"]:
        if L["kind"] == "t":
            assert 7 * L["px"] * L["sy"] >= 44.0, L
    assert p["minGlyphPx"] >= 44.0


def test_instance_html_durations_fonts_and_assets(built):
    p = built["plan"]
    proj = INST / "project"
    html = (proj / "index.html").read_text()
    assert f'data-duration="{p["duration"]:.6f}"' in html
    assert "__T_" not in html
    vo = re.search(r'id="vo"[^>]*data-duration="([0-9.]+)"', html)
    assert vo and float(vo.group(1)) <= p["duration"] + 1e-6
    for f in PROJECT_FILES:
        assert (proj / f).is_file(), f
    assert sha(proj / "kit/endcard.js") == sha(PLUGIN / "kit/endcard.js")
    assert sha(proj / "assets/fonts/ComicShanns.woff2") == sha(PLUGIN / "kit/fonts/ComicShanns.woff2")
    # D4: no system/unlicensed faces in the template or the instance; only bundled url() faces
    for doc in (html, (STYLE / "template/index.html").read_text()):
        css = doc.split("<style>", 1)[1].split("</style>", 1)[0]
        assert "local(" not in css
        for name in UNLICENSED:
            assert name not in css, name
        faces = re.findall(r'@font-face\s*\{[^}]*\}', css)
        assert len(faces) == 2 and all('url("assets/fonts/' in f for f in faces)
    assert "✔" not in p["items"][[L["kind"] for L in p["items"]].index("gag")]["text"]  # no glyph outside the font


def test_build_rejects_words_past_narration():
    d = scratch("neg-late")
    words = json.loads(WORDS.read_text())
    words.append({"id": "wX", "text": "late", "start": 15.5, "end": 15.9})
    wj = d / "w.json"
    wj.write_text(json.dumps(words))
    r = run([sys.executable, BUILD_PY, "--wav", WAV, "--words", wj, "--out", d / "o"])
    assert r.returncode == 2
    assert "after the narration" in r.stderr


def test_build_rejects_missing_wav_bad_content_and_unspoken_title():
    d = scratch("neg-args")
    r = run([sys.executable, BUILD_PY, "--wav", d / "nope.wav", "--words", WORDS, "--out", d / "x"])
    assert r.returncode == 2 and "wav not found" in r.stderr
    cj = d / "c.json"
    cj.write_text(json.dumps({"bogus": 1}))
    r = run([sys.executable, BUILD_PY, "--wav", WAV, "--words", WORDS, "--out", d / "y", "--content", cj])
    assert r.returncode == 2 and "unknown keys" in r.stderr
    tj = d / "t.json"
    tj.write_text(json.dumps({"title": "Quantum Pineapple"}))
    r = run([sys.executable, BUILD_PY, "--wav", WAV, "--words", WORDS, "--out", d / "z", "--content", tj])
    assert r.returncode == 2 and "not in the words" in r.stderr


def test_build_rejects_too_short_narration():
    d = scratch("neg-short")
    words = json.loads(WORDS.read_text())[:1]
    wj = d / "w.json"
    wj.write_text(json.dumps(words))
    r = run([sys.executable, BUILD_PY, "--wav", WAV, "--words", wj, "--out", d / "o", "--duration", "1.0"])
    assert r.returncode == 2 and "too short" in r.stderr


@pytest.mark.studio("_kit/lookdev/inputs/r6-S49.wav", "_kit/lookdev/inputs/r6-S49.words.json")
def test_template_is_script_agnostic():
    """A different narration (the full S49 words, first ~20 s) builds a different printout."""
    d = scratch("agnostic")
    words = [w for w in json.loads((INPUTS / "r6-S49.words.json").read_text()) if w["end"] <= 20.0]
    wj = d / "w.json"
    wj.write_text(json.dumps(words))
    r = run([sys.executable, BUILD_PY, "--wav", INPUTS / "r6-S49.wav", "--words", wj, "--out", d / "o",
             "--duration", "20.0", "--json"])
    assert r.returncode == 0, r.stderr
    p = json.loads(r.stdout)
    lwe = max(w["end"] for w in words)
    assert p["lastWordEnd"] == round(lwe, 4)
    finals = [L["text"] for L in p["items"] if L.get("role") == "final"]
    assert finals and finals != ["FROM BANKING."]
    starts = {round(w["start"], 4) for w in words}
    assert all(L["t"] in starts for L in p["items"] if L["kind"] == "t")
    assert all(TX_FITS(L) for L in p["items"] if L["kind"] == "t")
    pr = run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
              d / "o/project/assets/voice_norm.wav"])
    assert abs(float(pr.stdout) - min(20.0, p["endcard"]["start"])) < 0.02
    assert f'data-duration="{p["duration"]:.6f}"' in (d / "o/project/index.html").read_text()


def test_legibility_catches_the_canonical_small_printer_label(built):
    """Planted negative: a copy of the instance with the printer label back at the canonical 30 px
    must be flagged as small_type by the real legibility preflight."""
    proj = copy_project(scratch("neg-label") / "project")
    html = (proj / "index.html").read_text()
    needle = 'font: 400 46px/1 "KS Lilita", sans-serif; letter-spacing: 5px;'
    assert html.count(needle) == 1
    (proj / "index.html").write_text(html.replace(needle, 'font: 400 30px/1 "KS Lilita", sans-serif; letter-spacing: 5px;'))
    r = run([sys.executable, PRE / "legibility.py", proj, "--at", "5.0", "--json"], timeout=600)
    assert r.returncode == 1, r.stderr
    d = json.loads(r.stdout)
    assert any(f["kind"] == "small_type" and "KELLER-9" in f["detail"] for f in d["findings"]), d["findings"]


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
