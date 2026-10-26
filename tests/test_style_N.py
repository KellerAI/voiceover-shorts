"""Style N template (styles/N, gallery placard): instantiate with the S49 15 s inputs, render for
real, gate the render with every preflight (items 3-6) and the item 7 report.

Real runs only: build.py (ffmpeg + kit/sfx_bus), a sandboxed offline HyperFrames render, and the
preflight CLIs as subprocesses. Renders and scratch go under a pytest tmp dir;
the S49 inputs and the canonical dir come from SHORTS_STUDIO_DIR (tests skip without it). The render
is reused within one pytest session when index.html, the audio and the kit
end card are unchanged (keyed by a hash); set STYLE_N_FORCE_RENDER=1 to force a fresh one.

Planted negatives: words past the narration, a missing WAV, an unknown --content key, and an
instance copy with the placard captions back at the canonical 36 px, which the legibility
preflight must flag.
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
STYLE = PLUGIN / "styles" / "N"
BUILD_PY = STYLE / "build.py"
PRE = PLUGIN / "preflight"
EVIDENCE = PLUGIN / "evidence" / "evidence.py"

_STUDIO_DIR = os.environ.get("SHORTS_STUDIO_DIR")
STUDIO = Path(_STUDIO_DIR) if _STUDIO_DIR else None
if STUDIO is not None:
    INPUTS = STUDIO / "_kit/lookdev/inputs"
    WAV = INPUTS / "r6-S49-cut15.wav"
    WORDS = INPUTS / "r6-S49-cut15.words.json"
    CANON = STUDIO / "_kit/lookdev/r5N-S49-N"
else:
    INPUTS = WAV = WORDS = CANON = None
STUDIO_REQUIRED = (WAV, WORDS, CANON)
SANDBOX = PLUGIN / "kit" / "offline.sb"
OUT = INST = VIDEO = RESULTS = None  # scratch, bound per module by _scratch()


@pytest.fixture(scope="module", autouse=True)
def _scratch(tmp_path_factory):
    global OUT, INST, VIDEO, RESULTS
    OUT = tmp_path_factory.mktemp("style-N")
    INST = OUT / "s49-cut15"
    VIDEO = INST / "look.mp4"
    RESULTS = INST / "preflight"


D4 = ("Comic Sans", "Marker Felt", "Chalkduster", "SF Pro", "SF Mono")


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def run(cmd, **kw):
    return subprocess.run([str(c) for c in cmd], capture_output=True, text=True, **kw)


CANON_FILES = [] if CANON is None else [CANON / "project/index.html", CANON / "project/kit/gsap.min.js", CANON / "project/assets/narration.wav",
               CANON / "project/assets/narration.words.json", CANON / "project/assets/sfx.wav",
               CANON / "project/assets/fonts/ComicShanns.woff2", CANON / "project/assets/fonts/LilitaOne.woff2",
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
    fresh = os.environ.get("STYLE_N_FORCE_RENDER") == "1"
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
    rooms = p["rooms"]
    # the five S49 clauses are the canonical's five bays, in its motif order
    assert [r["motif"] for r in rooms] == ["circles", "chart", "terminal", "merge", "brand"]
    # whips land just after each room's first word (canonical: "It" 4.40 -> 4.45, "The" 10.00 -> 10.05)
    assert rooms[1]["land"] == pytest.approx(4.45) and rooms[1]["pan"] == pytest.approx(3.85)
    for a, b in zip(rooms, rooms[1:]):
        assert b["land"] - b["pan"] == pytest.approx(0.6)
        assert b["pan"] >= a["land"] + 0.8
        assert b["pan"] > a["head"][-1]["t"]          # every headline word pops before the whip away
        assert b["land"] >= b["first"]
    starts = {round(w["start"], 4) for w in words}
    assert [h["text"] for h in rooms[0]["head"]] == ["Effective", "Challenge."]
    assert all(h["t"] in starts for h in rooms[0]["head"])
    assert [h["hl"] for h in rooms[3]["head"]][-1] is True and rooms[3]["head"][-1]["text"] == "independence."
    assert rooms[-1]["caption"] == "And it borrows a fix from banking."
    assert p["intro"]["text"] == "A Keller AI brief" and p["intro"]["swap"] == pytest.approx(2.5)
    assert p["audio"]["bus_passed"] is True


def test_instantiated_html_has_no_placeholders_and_uses_kit(built):
    html = (INST / "project/index.html").read_text()
    for tok in ("__N_DURATION__", "__N_VO_DURATION__", "__N_DATA__", "__N_TITLE__"):
        assert tok not in html
    dur = f'data-duration="{built["plan"]["duration"]:.6f}"'
    assert html.count(dur) == 2  # root + sfx <audio>; the voice slot is the narration's length
    assert re.search(r'id="vo" src="assets/voice_norm.wav" data-start="0" data-duration="15\.0', html)
    css = html.split("<style>", 1)[1].split("</style>", 1)[0]
    live = re.sub(r"<!--.*?-->", "", html, flags=re.S)  # the template's doc comment names the swaps
    for bad in D4 + ("Menlo",):  # D4, plus the canonical's Menlo terminal swapped for Comic Shanns
        assert bad not in live, bad
    assert 'url("assets/fonts/ComicShanns.woff2")' in css and 'url("assets/fonts/LilitaOne.woff2")' in css
    assert sha(INST / "project/kit/endcard.js") == sha(PLUGIN / "kit/endcard.js")
    for f in ("LilitaOne.woff2", "ComicShanns.woff2"):
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


def test_build_rejects_missing_wav_and_bad_content(tmp_path):
    r = run([sys.executable, BUILD_PY, "--wav", tmp_path / "nope.wav", "--words", WORDS, "--out", tmp_path / "x"])
    assert r.returncode == 2 and "wav not found" in r.stderr
    cj = tmp_path / "c.json"
    cj.write_text(json.dumps({"bogus": 1}))
    r = run([sys.executable, BUILD_PY, "--wav", WAV, "--words", WORDS, "--out", tmp_path / "y", "--content", cj])
    assert r.returncode == 2 and "unknown keys" in r.stderr


@pytest.mark.studio("_kit/lookdev/inputs/r6-S49.wav", "_kit/lookdev/inputs/r6-S49.words.json")
def test_template_is_script_agnostic(tmp_path):
    """A different narration (the full S49 words, first ~20 s) builds a different gallery."""
    words = [w for w in json.loads((INPUTS / "r6-S49.words.json").read_text()) if w["end"] <= 20.0]
    wj = tmp_path / "w.json"
    wj.write_text(json.dumps(words))
    r = run([sys.executable, BUILD_PY, "--wav", INPUTS / "r6-S49.wav", "--words", wj, "--out", tmp_path / "o",
             "--duration", "20.0", "--json"])
    assert r.returncode == 0, r.stderr
    p = json.loads(r.stdout)
    assert p["lastWordEnd"] == round(max(w["end"] for w in words), 4)
    assert p["rooms"][-1]["motif"] == "brand" and p["rooms"][0]["motif"] == "circles"
    assert p["rooms"][-1]["caption"] != "And it borrows a fix from banking."
    assert all(r2["land"] < p["endcard"]["start"] for r2 in p["rooms"])
    pr = run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
              tmp_path / "o/project/assets/voice_norm.wav"])
    assert abs(float(pr.stdout) - min(20.0, p["endcard"]["start"])) < 0.02
    assert f'data-duration="{p["duration"]:.6f}"' in (tmp_path / "o/project/index.html").read_text()


def test_legibility_catches_the_canonical_small_caption(built, tmp_path):
    """Planted negative: a copy of the instance with the placard captions back at the canonical
    36 px must be flagged as small_type by the real legibility preflight while a placard shows."""
    proj = tmp_path / "project"
    shutil.copytree(INST / "project", proj)
    html = (proj / "index.html").read_text()
    needle = ".placard .cap { font-size: 46px;"
    assert html.count(needle) == 1
    (proj / "index.html").write_text(html.replace(needle, ".placard .cap { font-size: 36px;"))
    t = built["plan"]["rooms"][2]["land"] + 1.2  # terminal room, placard faded in
    r = run([sys.executable, PRE / "legibility.py", proj, "--at", f"{t:.2f}", "--json"], timeout=600)
    assert r.returncode == 1, r.stderr
    d = json.loads(r.stdout)
    assert any(f["kind"] == "small_type" and "model" in f["detail"] for f in d["findings"]), d["findings"]


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


def test_legibility_room_arrivals(rendered):
    """Dense samples over the opening and every whip landing, where placards are still fading in."""
    ts = [0.0, 0.2, 0.5, 0.8]
    for r in rendered["rooms"][1:]:
        ts += [r["pan"] + 0.3, r["land"], r["land"] + 0.15, r["land"] + 0.4]
    ts += [rendered["endcard"]["start"] + 0.05, rendered["duration"] - 0.05]
    d = preflight("legibility", [INST / "project", "--at", ",".join(f"{t:.2f}" for t in ts)], "legibility_arrivals")
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
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "evidence_summary.json").write_text(json.dumps(
        {"passed": rep["passed"], "checks": [{"name": c["name"], "passed": c["passed"]} for c in rep["checks"]]}))
    assert rep["passed"] is True
    assert sorted(c["name"] for c in rep["checks"]) == ["dead_frames", "endcard", "legibility", "loudness"]
    assert all(c["passed"] for c in rep["checks"])
    assert rep["sheets"]
