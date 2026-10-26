"""Style J template (styles/J): instantiate with the S49 15 s inputs, render for real, gate the
render with every preflight (items 3-6) and the item 7 report.

Real runs only: build.py (ffmpeg + kit/sfx_bus), a sandboxed offline HyperFrames render, and the
preflight CLIs as subprocesses. Renders and scratch go under a pytest tmp dir; the S49 inputs and
the canonical dir come from SHORTS_STUDIO_DIR (tests skip without it). The render is reused within
one pytest session when index.html, the audio and the kit end card are unchanged (keyed by a hash); set STYLE_J_FORCE_RENDER=1 to force a fresh one.

The 46 px footer is legible text in the lower third, so the DOM legibility check would count the
band as filled even on an otherwise empty page. A pixel check closes that gap: the band between
y 1280 and the footer (y 1790) must carry ink on every sampled frame before the end card. It has
a planted negative, and so does the footer size fix (the canonical 34 px footer must fail).
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
STYLE = PLUGIN / "styles" / "J"
BUILD_PY = STYLE / "build.py"
PRE = PLUGIN / "preflight"
EVIDENCE = PLUGIN / "evidence" / "evidence.py"

_STUDIO_DIR = os.environ.get("SHORTS_STUDIO_DIR")
STUDIO = Path(_STUDIO_DIR) if _STUDIO_DIR else None
if STUDIO is not None:
    INPUTS = STUDIO / "_kit/lookdev/inputs"
    WAV = INPUTS / "r6-S49-cut15.wav"
    WORDS = INPUTS / "r6-S49-cut15.words.json"
    CANON = STUDIO / "_kit/lookdev/r4-S49-J"
else:
    INPUTS = WAV = WORDS = CANON = None
STUDIO_REQUIRED = (WAV, WORDS, CANON)
SANDBOX = PLUGIN / "kit" / "offline.sb"
OUT = INST = VIDEO = RESULTS = None  # scratch, bound per module by _scratch()


@pytest.fixture(scope="module", autouse=True)
def _scratch(tmp_path_factory):
    global OUT, INST, VIDEO, RESULTS
    OUT = tmp_path_factory.mktemp("style-J")
    INST = OUT / "s49-cut15"
    VIDEO = INST / "look.mp4"
    RESULTS = INST / "preflight"


UNLICENSED = ("Comic Sans", "Marker Felt", "Chalkduster", "SF Pro", "SF Mono")


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def run(cmd, **kw):
    return subprocess.run([str(c) for c in cmd], capture_output=True, text=True, **kw)


CANON_FILES = [] if CANON is None else [CANON / "project/index.html", CANON / "project/kit/gsap.min.js", CANON / "project/assets/narration.wav",
               CANON / "project/assets/narration.words.json", CANON / "project/assets/sfx.wav",
               CANON / "project/assets/fonts/Virgil.woff2", CANON / "project/assets/fonts/ComicShanns.woff2",
               CANON / "sfx_gen.py", CANON / "notes.md", CANON / "look.mp4", CANON / "look_v2.mp4"]


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
    fresh = os.environ.get("STYLE_J_FORCE_RENDER") == "1"
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
    assert len(p["bounds"]) == 5 and p["bounds"][0] == 0.0
    assert all(b - a >= 1.6 for a, b in zip(p["bounds"], p["bounds"][1:]))
    assert abs(p["bounds"][4] - p["endcard"]["start"]) < 1e-6
    assert p["endcard"]["start"] >= lwe - 0.05
    assert p["duration"] - max(p["endcard"]["start"], lwe) >= 1.5
    starts = sorted(round(w["start"], 4) for w in words)
    # inner page edges are word starts; every spoken word is written exactly once, on its page
    assert all(b in starts for b in p["bounds"][1:4])
    written = [w["text"] for pg in p["pages"] for w in pg["body"] + pg["hero"] + pg["after"]]
    assert written == [w["text"] for w in sorted(words, key=lambda w: w["start"])]
    for k, pg in enumerate(p["pages"]):
        assert pg["hero"], k
        for w in pg["body"] + pg["hero"] + pg["after"]:
            assert p["bounds"][k] <= w["start"] < p["bounds"][k + 1]
        if k:  # the whip into a page never starts before the previous page's last word ends
            prev = p["pages"][k - 1]
            assert pg["trans"] >= max(w["end"] for w in prev["body"] + prev["hero"] + prev["after"]) - 1e-6
        assert pg["doodleT0"] < pg["doodleT1"] <= pg["end"]
    # the canonical heroes fall out of the S49 words without any copy override
    assert [w["text"] for w in p["pages"][1]["hero"]] == ["habit"]
    assert [w["text"] for w in p["pages"][0]["hero"]] == ["Effective", "Challenge."]
    assert p["slam"] and p["slam"]["page"] == 1 and p["slam"]["big"] == "WORK"
    assert p["slam"]["t"] < p["pages"][2]["trans"]
    assert p["audio"]["bus_passed"] is True


def test_instantiated_html_has_no_placeholders_and_uses_kit(built):
    html = (INST / "project/index.html").read_text()
    for tok in ("__J_DURATION__", "__J_VO_DURATION__", "__J_DATA__", "__J_TITLE__"):
        assert tok not in html
    dur = f'data-duration="{built["plan"]["duration"]:.6f}"'
    assert html.count(dur) == 2  # root + sfx <audio>; the voice slot is the narration's length
    assert 'id="narration" src="assets/voice_norm.wav" data-start="0" data-duration="15.0' in html
    for bad in UNLICENSED:  # D4: bundled OFL only, no system fallbacks either
        assert bad not in html, bad
    assert "local(" not in html
    assert sha(INST / "project/kit/endcard.js") == sha(PLUGIN / "kit/endcard.js")
    for f in ("LilitaOne.woff2", "ComicShanns.woff2", "Virgil.woff2"):
        assert sha(INST / "project/assets/fonts" / f) == sha(PLUGIN / "kit/fonts" / f)
    assert sha(INST / "project/assets/brand/keller-ai-logo.svg") == sha(PLUGIN / "kit/brand/keller-ai-logo.svg")


def test_template_has_no_unlicensed_fonts():
    html = (STYLE / "template/index.html").read_text()
    for bad in UNLICENSED:
        assert bad not in html, bad


def test_build_rejects_words_past_the_narration(tmp_path):
    """Planted negative: a word that ends after --duration must fail with exit 2."""
    words = json.loads(WORDS.read_text())
    r = run([sys.executable, BUILD_PY, "--wav", WAV, "--words", WORDS, "--out", tmp_path / "x",
             "--duration", str(max(w["end"] for w in words) - 1.0)])
    assert r.returncode == 2
    assert "after the narration" in r.stderr


def test_build_rejects_missing_wav_and_bad_content(tmp_path):
    r = run([sys.executable, BUILD_PY, "--wav", tmp_path / "nope.wav", "--words", WORDS, "--out", tmp_path / "x"])
    assert r.returncode == 2 and "wav not found" in r.stderr
    cj = tmp_path / "c.json"
    cj.write_text(json.dumps({"bogus": 1}))
    r = run([sys.executable, BUILD_PY, "--wav", WAV, "--words", WORDS, "--out", tmp_path / "y", "--content", cj])
    assert r.returncode == 2 and "unknown keys" in r.stderr
    cj.write_text(json.dumps({"doodles": ["doc", "robots", "patched", "castle"]}))
    r = run([sys.executable, BUILD_PY, "--wav", WAV, "--words", WORDS, "--out", tmp_path / "z", "--content", cj])
    assert r.returncode == 2 and "doodles" in r.stderr


@pytest.mark.studio("_kit/lookdev/inputs/r6-S49.wav", "_kit/lookdev/inputs/r6-S49.words.json")
def test_template_is_script_agnostic(tmp_path):
    """A different narration (the full S49 words, first ~20 s) builds a different plan, and a
    content override changes the copy without touching the timing."""
    words = [w for w in json.loads((INPUTS / "r6-S49.words.json").read_text()) if w["end"] <= 20.0]
    wj = tmp_path / "w.json"
    wj.write_text(json.dumps(words))
    cj = tmp_path / "c.json"
    cj.write_text(json.dumps({"labels": ["one", "two", "three", "four"], "slam": False, "emph": ["circle"] * 4}))
    r = run([sys.executable, BUILD_PY, "--wav", INPUTS / "r6-S49.wav", "--words", wj, "--out", tmp_path / "o",
             "--duration", "20.0", "--content", cj, "--json"])
    assert r.returncode == 0, r.stderr
    p = json.loads(r.stdout)
    assert p["lastWordEnd"] == round(max(w["end"] for w in words), 4)
    assert p["bounds"][4] == p["endcard"]["start"]
    assert p["slam"] is None and [pg["label"] for pg in p["pages"]] == ["one", "two", "three", "four"]
    assert p["pages"][3]["hero"] != [{"text": "banking.", "start": 14.08, "end": 15.04}]
    pr = run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
              tmp_path / "o/project/assets/voice_norm.wav"])
    assert abs(float(pr.stdout) - min(20.0, p["endcard"]["start"])) < 0.02
    assert f'data-duration="{p["duration"]:.6f}"' in (tmp_path / "o/project/index.html").read_text()


# ---------------------------------------------------------------- pixel check: the drawn lower frame

def band_ink(video, times, top=1280, bottom=1790, luma=160):
    """Fraction of pixels in y [top, bottom) (above the footer) darker than `luma` (navy ink or
    vermilion marker on cream paper), per sample time. Measured on this render: empty paper with
    its dots, fibre, grain and vignette never goes below luma 204, and navy ink under the whip's
    vertical smear sits near 150, so 160 separates ink from paper."""
    import numpy as np
    out = []
    for t in times:
        r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", str(video), "-frames:v", "1",
                            "-vf", f"crop=1080:{bottom - top}:0:{top},format=gray", "-f", "rawvideo", "-"],
                           capture_output=True, timeout=120)
        assert r.returncode == 0, r.stderr.decode()[-500:]
        a = np.frombuffer(r.stdout, dtype=np.uint8)
        out.append(float((a < luma).mean()))
    return out


def test_lower_band_has_ink_on_every_sample(rendered):
    """Every content frame before the end card has a drawing or writing above the footer in the
    lower third (the end card is the kit's flat brand card, gated by preflight/endcard.py)."""
    times = [round(0.25 + i * 0.5, 2) for i in range(int((rendered["endcard"]["start"] - 0.3) / 0.5))]
    ink = band_ink(VIDEO, times)
    (RESULTS / "lower_band.json").write_text(json.dumps(dict(zip(map(str, times), ink))))
    assert min(ink) >= 0.002, [(t, v) for t, v in zip(times, ink) if v < 0.002]


def test_lower_band_check_catches_an_empty_band(tmp_path):
    """Planted negative: a frame of the style's paper colour scores 0."""
    v = tmp_path / "paper.mp4"
    r = run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=0xFBF7F0:s=1080x1920:d=1:r=30", "-pix_fmt", "yuv420p", v])
    assert r.returncode == 0, r.stderr
    assert band_ink(v, [0.5])[0] < 0.002


def test_legibility_catches_the_canonical_footer_size(built, tmp_path):
    """Planted negative: a copy of the instance with the footer back at the canonical 34 px must
    fail the legibility preflight with small_type."""
    proj = tmp_path / "project"
    shutil.copytree(INST / "project", proj)
    html = (proj / "index.html").read_text()
    needle = ".foot .l { position: absolute; left: 136px; bottom: 0; font-size: 46px;"
    assert needle in html
    (proj / "index.html").write_text(html.replace(needle, needle.replace("46px", "34px")))
    r = run([sys.executable, PRE / "legibility.py", proj, "--samples", "3", "--json"], timeout=900)
    assert r.returncode == 1, r.stderr
    d = json.loads(r.stdout)
    assert any(f["kind"] == "small_type" and "34" in f["detail"] for f in d["findings"]), d["findings"]


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
    assert d["metrics"]["min_rendered_px_seen"] >= 44


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
