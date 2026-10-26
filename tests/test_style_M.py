"""Style M template (styles/M, blueprint draft): instantiate with the S49 15 s inputs, render for
real, gate the render with every preflight (items 3-6) and the item 7 report.

Real runs only: build.py (numpy/scipy SFX + kit/sfx_bus), a sandboxed offline HyperFrames render,
and the preflight CLIs as subprocesses. Renders and scratch go under a pytest tmp dir;
the S49 inputs and the canonical dir come from SHORTS_STUDIO_DIR (tests skip without it). The render
is reused within one pytest session when index.html, the audio and the kit
end card are unchanged (keyed by a hash); set STYLE_M_FORCE_RENDER=1 to force a fresh one.

Planted negatives: words past the narration, a missing WAV, an unknown --content key, a glyph no
bundled font covers, an instance copy with the canonical 28 px mono labels (legibility must flag
small_type), and an instance copy without the pre-printed title block (legibility must flag
empty_lower_third at the first sample, the canonical's defect).
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
STYLE = PLUGIN / "styles" / "M"
BUILD_PY = STYLE / "build.py"
S49_CONTENT = STYLE / "examples" / "S49.content.json"
PRE = PLUGIN / "preflight"
EVIDENCE = PLUGIN / "evidence" / "evidence.py"

_STUDIO_DIR = os.environ.get("SHORTS_STUDIO_DIR")
STUDIO = Path(_STUDIO_DIR) if _STUDIO_DIR else None
if STUDIO is not None:
    INPUTS = STUDIO / "_kit/lookdev/inputs"
    WAV = INPUTS / "r6-S49-cut15.wav"
    WORDS = INPUTS / "r6-S49-cut15.words.json"
    CANON = STUDIO / "_kit/lookdev/r5M-S49-M"
else:
    INPUTS = WAV = WORDS = CANON = None
STUDIO_REQUIRED = (WAV, WORDS, CANON)
SANDBOX = PLUGIN / "kit" / "offline.sb"
OUT = INST = VIDEO = RESULTS = None  # scratch, bound per module by _scratch()


@pytest.fixture(scope="module", autouse=True)
def _scratch(tmp_path_factory):
    global OUT, INST, VIDEO, RESULTS
    OUT = tmp_path_factory.mktemp("style-M")
    INST = OUT / "s49-cut15"
    VIDEO = INST / "look.mp4"
    RESULTS = INST / "preflight"


UNLICENSED = ("Comic Sans", "Marker Felt", "Chalkduster", "SF Pro", "SF Mono", "Menlo", "DIN")

CANON_FILES = [] if CANON is None else [CANON / "project/index.html", CANON / "project/kit/gsap.min.js", CANON / "project/assets/narration.wav",
               CANON / "project/assets/sfx.wav", CANON / "project/assets/words.json",
               CANON / "project/assets/fonts/ComicShanns.woff2", CANON / "sfx_gen.py", CANON / "notes.md",
               CANON / "look.mp4", CANON / "look_v2.mp4"]


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def run(cmd, **kw):
    return subprocess.run([str(c) for c in cmd], capture_output=True, text=True, **kw)


@pytest.fixture(scope="module")
def built():
    before = {str(p): sha(p) for p in CANON_FILES}
    r = run([sys.executable, BUILD_PY, "--wav", WAV, "--words", WORDS, "--out", INST, "--slug", "S49",
             "--content", S49_CONTENT, "--json"])
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
    fresh = os.environ.get("STYLE_M_FORCE_RENDER") == "1"
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


def test_build_plan_matches_canonical_rhythm(built):
    """For S49 the anchors land on the canonical ones, so the stroke schedule is the canonical v2's."""
    p = built["plan"]
    words = json.loads(WORDS.read_text())
    lwe = max(w["end"] for w in words)
    ec = p["endcard"]
    assert abs(ec["start"] - (lwe + 0.1)) < 1e-6
    assert p["duration"] == ec["end"] and p["duration"] - max(ec["start"], lwe) >= 1.5
    assert p["anchors"] == {"a": 4.4, "b": 13.2, "k": 1.0}  # "It" (clause after the title) and "borrows"
    st = {s["id"]: s["t"] for s in p["strokes"]}
    assert st == {"border": 0.05, "headrule": 1.0, "uline": 3.3, "boxA": 4.5, "dimA": 5.5, "boxB": 6.1,
                  "arrAB": 7.05, "arrBA": 8.15, "drops": 8.7, "bar": 9.0, "lead": 9.62, "fix": 10.12,
                  "boxK": 10.5, "bank": 11.05}
    assert p["title"]["lines"] == ["EFFECTIVE", "CHALLENGE"] and abs(p["title"]["drop"] - 2.62) < 1e-6
    assert abs(p["stamp"]["t"] - 11.90) < 1e-6        # on "not" (11.88) + 0.02
    assert p["k3"] == 13.2 and abs(p["pulse"] - 14.06) < 1e-6
    assert max(s["t"] + s["dur"] for s in p["strokes"]) < ec["start"]
    assert p["text"]["L_ind2"] == "~ 0"   # canonical "≈ 0": no bundled face has U+2248
    assert p["glyph_check"] == "ok"
    assert p["audio"]["bus_passed"] is True


def test_instantiated_html_has_no_placeholders_and_uses_kit(built):
    html = (INST / "project/index.html").read_text()
    for tok in ("__M_DURATION__", "__M_VO_DURATION__", "__M_DATA__", "__M_TITLE__"):
        assert tok not in html
    dur = f'data-duration="{built["plan"]["duration"]:.6f}"'
    assert html.count(dur) == 2  # root + sfx <audio>; the voice slot is the narration's length
    css = html.split("<style>", 1)[1].split("</style>", 1)[0]
    for bad in UNLICENSED:  # D4 (plus the canonical's DIN and Menlo system faces): bundled OFL only
        assert bad not in css, bad
    assert "local(" not in html
    assert sha(INST / "project/kit/endcard.js") == sha(PLUGIN / "kit/endcard.js")
    for f in ("LilitaOne.woff2", "ComicShanns.woff2", "Virgil.woff2"):
        assert sha(INST / "project/assets/fonts" / f) == sha(PLUGIN / "kit/fonts" / f)
    assert sha(INST / "project/assets/brand/keller-ai-logo.svg") == sha(PLUGIN / "kit/brand/keller-ai-logo.svg")
    assert sha(INST / "project/kit/gsap.min.js") == sha(CANON / "project/kit/gsap.min.js")


def test_build_rejects_words_past_the_narration(tmp_path):
    """Planted negative: a word that ends after --duration must fail with exit 2."""
    words = json.loads(WORDS.read_text())
    r = run([sys.executable, BUILD_PY, "--wav", WAV, "--words", WORDS, "--out", tmp_path / "x",
             "--duration", str(max(w["end"] for w in words) - 1.0)])
    assert r.returncode == 2
    assert "after the narration" in r.stderr


def test_build_rejects_missing_wav_bad_content_and_uncovered_glyph(tmp_path):
    r = run([sys.executable, BUILD_PY, "--wav", tmp_path / "nope.wav", "--words", WORDS, "--out", tmp_path / "x"])
    assert r.returncode == 2 and "wav not found" in r.stderr
    cj = tmp_path / "c.json"
    cj.write_text(json.dumps({"bogus": 1}))
    r = run([sys.executable, BUILD_PY, "--wav", WAV, "--words", WORDS, "--out", tmp_path / "y", "--content", cj])
    assert r.returncode == 2 and "unknown keys" in r.stderr
    cj.write_text(json.dumps({"stamp": "★ VOID"}))  # BLACK STAR: in neither Lilita One nor Comic Shanns
    r = run([sys.executable, BUILD_PY, "--wav", WAV, "--words", WORDS, "--out", tmp_path / "z", "--content", cj])
    assert r.returncode == 2 and "glyphs the bundled fonts lack" in r.stderr


@pytest.mark.studio("_kit/lookdev/inputs/r6-S49.wav", "_kit/lookdev/inputs/r6-S49.words.json")
def test_template_is_script_agnostic(tmp_path):
    """A different narration (the full S49 words, first ~20 s, no --content) builds a different
    sheet: other anchors, mapped stroke times and copy derived from the words."""
    words = [w for w in json.loads((INPUTS / "r6-S49.words.json").read_text()) if w["end"] <= 20.0]
    wj = tmp_path / "w.json"
    wj.write_text(json.dumps(words))
    r = run([sys.executable, BUILD_PY, "--wav", INPUTS / "r6-S49.wav", "--words", wj, "--out", tmp_path / "o",
             "--duration", "20.0", "--json"])
    assert r.returncode == 0, r.stderr
    p = json.loads(r.stdout)
    assert p["lastWordEnd"] == round(max(w["end"] for w in words), 4)
    assert p["anchors"]["b"] != 13.2 and p["anchors"]["k"] != 1.0
    st = {s["id"]: s["t"] for s in p["strokes"]}
    assert st["boxA"] < st["boxB"] < st["bar"] < st["fix"] < st["boxK"] < st["seal"] < p["k3"] < p["endcard"]["start"]
    assert "bank" not in st  # default icon
    assert p["text"]["L_A1"] != "MODEL A" and p["text"]["L_K3"] != "BORROWED FROM BANKING"
    pr = run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
              tmp_path / "o/project/assets/voice_norm.wav"])
    assert abs(float(pr.stdout) - min(20.0, p["endcard"]["start"])) < 0.02
    assert f'data-duration="{p["duration"]:.6f}"' in (tmp_path / "o/project/index.html").read_text()


def test_legibility_catches_the_canonical_small_mono_labels(built, tmp_path):
    """Planted negative: a copy with the mono labels back at the canonical 28 px must be flagged."""
    proj = tmp_path / "project"
    shutil.copytree(INST / "project", proj)
    html = (proj / "index.html").read_text()
    needle = 'font-weight: 700; font-size: 46px; letter-spacing: 0; }'
    assert html.count(needle) == 1
    (proj / "index.html").write_text(html.replace(needle, 'font-weight: 700; font-size: 28px; letter-spacing: 0; }'))
    r = run([sys.executable, PRE / "legibility.py", proj, "--at", "8.6", "--json"], timeout=600)
    assert r.returncode == 1, r.stderr
    d = json.loads(r.stdout)
    small = [f for f in d["findings"] if f["kind"] == "small_type"]
    assert small, d["findings"]
    # at 8.6 s eight mono runs are up (header x2, brief, dim, GENERATOR, CHECKER, CHECKS, LGTM);
    # the detail lists the five smallest by name
    assert "8 visible text run(s) below 44.0px" in small[0]["detail"], small[0]["detail"]
    assert "'GENERATOR' 28.5px" in small[0]["detail"], small[0]["detail"]


def test_legibility_catches_empty_lower_third_without_title_block(built, tmp_path):
    """Planted negative: without the pre-printed title block (the canonical drew it at 1.25 s),
    the lower third is empty at the opening."""
    proj = tmp_path / "project"
    shutil.copytree(INST / "project", proj)
    html = (proj / "index.html").read_text()
    needle = '<path id="tblock" class="ln"'
    assert html.count(needle) == 1
    html = html.replace(needle, '<path id="tblock" class="ln" style="display:none"')
    html = html.replace('<g id="tile" clip-path', '<g id="tile" style="display:none" clip-path')
    for tid in ("L_word", "L_tb2a", "L_tb2b", "L_rev"):
        html = html.replace(f'<text id="{tid}" class="tb', f'<text id="{tid}" style="display:none" class="tb')
    (proj / "index.html").write_text(html)
    r = run([sys.executable, PRE / "legibility.py", proj, "--at", "0.17", "--json"], timeout=600)
    assert r.returncode == 1, r.stderr
    d = json.loads(r.stdout)
    assert [f["kind"] for f in d["findings"]] == ["empty_lower_third"], d["findings"]


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
