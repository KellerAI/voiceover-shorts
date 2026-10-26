"""Style K16 template (styles/K): instantiate with the S49 15 s inputs, render, and gate on every preflight.

Real runs only: the builder, an offline sandboxed HyperFrames render, and the item 3/4/5/6 CLIs plus
the item 7 report. Renders and scratch go under pytest tmp dirs. The S49 inputs and the canonical
K16 dirs come from SHORTS_STUDIO_DIR; tests that need them skip without it (see conftest.py).
Planted negatives: the canonical r6-S49-K16-cut15 render (read only) fails the item 4 end-card check
that the template passes, and its bottom band is near-black where the template's floor is not.
The item 6 DOM check cannot see inside the full-bleed canvas (those samples are "indeterminate"), so
canvas type size is covered here by the plan (every hero scale >= MIN_SC) and the template's clamp.
"""
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PLUGIN = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("shorts_style_K_build", PLUGIN / "styles" / "K" / "build.py")
if _spec is None or _spec.loader is None:
    raise ImportError(f"cannot load style K build module from {PLUGIN / 'styles' / 'K' / 'build.py'}")
style_k = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(style_k)

PREFLIGHT = PLUGIN / "preflight"
EVIDENCE = PLUGIN / "evidence" / "evidence.py"
_STUDIO_DIR = os.environ.get("SHORTS_STUDIO_DIR")
STUDIO = Path(_STUDIO_DIR) if _STUDIO_DIR else None
if STUDIO is not None:
    INPUTS = STUDIO / "_kit/lookdev/inputs"
    WAV = INPUTS / "r6-S49-cut15.wav"
    WORDS = INPUTS / "r6-S49-cut15.words.json"
    CANON = STUDIO / "_kit/lookdev/r6-S49-K16"
    CANON_CUT = STUDIO / "_kit/lookdev/r6-S49-K16-cut15"
    CANON_FILES = [CANON / "project/index.html", CANON / "notes.md", CANON / "sfx_gen.py", CANON / "look.mp4",
                   CANON_CUT / "project/index.html", CANON_CUT / "notes.md", CANON_CUT / "sfx_gen.py",
                   CANON_CUT / "look.mp4"]
else:
    INPUTS = WAV = WORDS = CANON = CANON_CUT = None
    CANON_FILES = []
STUDIO_REQUIRED = (WAV, WORDS, CANON, CANON_CUT, *CANON_FILES)
BUILD = RUN = VIDEO = EVID = None  # scratch, bound per module by _scratch()
UNLICENSED = ("SF Pro", "SF Mono", "SFNS", "Comic Sans", "Marker Felt", "Chalkduster")


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


CANON_SHA = ({str(p): sha(p) for p in CANON_FILES}
             if CANON_FILES and all(p.is_file() for p in CANON_FILES) else {})


@pytest.fixture(scope="module", autouse=True)
def _scratch(tmp_path_factory):
    global BUILD, RUN, VIDEO, EVID
    BUILD = tmp_path_factory.mktemp("style-K")
    RUN = BUILD / "s49-cut15"
    VIDEO = RUN / "styleK-S49-cut15.mp4"
    EVID = RUN / "evidence"


def cli(*args):
    return subprocess.run([sys.executable, *map(str, args)], capture_output=True, text=True, timeout=1800)


def save(name, r):
    EVID.mkdir(parents=True, exist_ok=True)
    (EVID / f"{name}.json").write_text(r.stdout)
    (EVID / f"{name}.stderr.txt").write_text(r.stderr)


def band_luma(video, t, y0=1712, y1=1920):
    """Mean luma (0-255) of the rows y0..y1 of the frame at t (real ffmpeg decode)."""
    r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", str(video), "-frames:v", "1",
                        "-vf", f"crop=1080:{y1 - y0}:0:{y0},format=gray", "-f", "rawvideo", "-"],
                       capture_output=True, timeout=120, check=True)
    a = np.frombuffer(r.stdout, dtype=np.uint8)
    assert a.size == 1080 * (y1 - y0)
    return float(a.mean())


# ---------------------------------------------------------------- pure planning


@pytest.fixture(scope="module")
def plan():
    return style_k.plan_timeline(style_k.load_words(WORDS))["T"]


@pytest.mark.studio
def test_end_card_after_last_word_with_hold(plan):
    assert plan["lastWordEnd"] == pytest.approx(15.04)
    assert plan["ecStart"] >= plan["lastWordEnd"]
    assert plan["END"] - max(plan["ecStart"], plan["lastWordEnd"]) >= 1.5


@pytest.mark.studio
def test_beats_come_from_the_words(plan):
    assert [h["t"] for h in plan["headline"]] == ["EFFECTIVE", "CHALLENGE"]
    assert plan["stageStart"] < plan["headline"][0]["s"] < plan["levelStart"]
    neg = next(h for h in plan["hero"] if h["kind"] == "not")
    assert neg["words"][neg["key"]]["t"] == "NOT" and plan["neg"]["t"] == pytest.approx(11.88)
    assert neg["t1"] - plan["neg"]["t"] >= 0.55  # NOT stays up long enough to read
    assert plan["hero"][-1]["kind"] == "power" and plan["powerOn"] == pytest.approx(14.08)
    assert any(h["kind"] == "gag" for h in plan["hero"])


@pytest.mark.studio
def test_hero_band_never_empty_and_type_never_small(plan):
    hero = plan["hero"]
    assert hero[0]["t0"] == pytest.approx(plan["levelStart"])
    for a, b in zip(hero, hero[1:]):
        assert b["t0"] == pytest.approx(a["t1"]) and a["t1"] > a["t0"]
    for h in hero:
        assert h["sc"] >= style_k.MIN_SC
        for w in h["words"]:
            assert set(w["t"]) <= style_k.GLYPHS
            assert style_k.tw(len(w["t"]), h["sc"]) <= style_k.TEXT_W
        assert len(h["lines"]) * h["pitch"] <= style_k.HERO_BOTTOM - style_k.HERO_TOP


def test_planted_negative_unfittable_word_is_rejected():
    assert style_k.fit_lines(["X" * 30]) is None
    assert style_k.fit_lines(["SUPERCALIFRAGILISTIC"], scales=(5, 4, 3)) is None
    assert style_k.fit_lines(["SUPERCALIFRAGILISTIC"])["sc"] == 2


def test_sanitize_keeps_only_bitmap_glyphs():
    assert style_k.sanitize("model’s work — café & co;") == "MODEL'S WORK - CAF + CO"


def test_too_few_words_is_usage_error():
    with pytest.raises(style_k.BuildError):
        style_k.load_words([{"text": "hi", "start": 0, "end": 1}])


def test_other_script_plans_without_s49_copy():
    ws = [{"text": t, "start": i * 0.4, "end": i * 0.4 + 0.3} for i, t in enumerate(
        "Meet the new deploy bot. It ships every branch you push, then it runs the tests twice. "
        "Teams said it never sleeps and it never misses a broken build.".split())]
    T = style_k.plan_timeline(ws)["T"]
    assert [h["t"] for h in T["headline"]] == ["DEPLOY", "BOT"]
    assert T["neg"] is not None and T["hero"][-1]["kind"] == "power"
    assert all(h["sc"] >= style_k.MIN_SC for h in T["hero"])


# ---------------------------------------------------------------- build + render


@pytest.fixture(scope="module")
def built():
    proj = RUN / "project"
    m = style_k.build(WAV, WORDS, proj)
    style_k.render(proj, VIDEO, quality="looks", workers=2)
    return m


def test_fonts_are_bundled_ofl_only(built):
    proj = Path(built["project"])
    html = (proj / "index.html").read_text()
    for bad in UNLICENSED:
        assert bad not in html
    assert sorted(p.name for p in (proj / "assets/fonts").iterdir()) == ["LilitaOne.woff2"]
    assert sha(proj / "assets/fonts/LilitaOne.woff2") == sha(PLUGIN / "kit/fonts/LilitaOne.woff2")
    assert sha(proj / "kit/endcard.js") == sha(PLUGIN / "kit/endcard.js")
    assert html.count("if (sc < MIN_SC) sc = MIN_SC;") == 2  # text() and sloppy() clamp
    assert '"minSc": 2' in html and "{{" not in html


def test_render_duration_matches_plan(built):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(VIDEO)],
                       capture_output=True, text=True, check=True)
    assert float(r.stdout) == pytest.approx(built["duration"], abs=0.1)


def test_lower_band_filled_where_canonical_was_black(built):
    # title, level, gag, power-up approach; 12.0 is avoided because the canonical NOT hit flashes the
    # whole frame white (its band luma is 50 there, measured), which is a flash, not a filled band
    times = [1.0, 5.0, 9.0, 13.0]
    ours = [band_luma(VIDEO, t) for t in times]
    canon = [band_luma(CANON_CUT / "look.mp4", t) for t in times]
    (EVID).mkdir(parents=True, exist_ok=True)
    (EVID / "lower_band_luma.json").write_text(json.dumps({"times": times, "template": ours, "canonical": canon}))
    assert min(ours) > 30, ours
    assert max(canon) < 18, canon  # planted negative: the no-subs cut left a black band


def test_dead_frames_pass(built):
    r = cli(PREFLIGHT / "dead_frames.py", VIDEO, "--json")
    save("dead_frames", r)
    assert r.returncode == 0, r.stdout + r.stderr
    assert json.loads(r.stdout)["passed"] is True


def test_endcard_pass(built):
    r = cli(PREFLIGHT / "endcard.py", VIDEO, WORDS, "--json")
    save("endcard", r)
    assert r.returncode == 0, r.stdout + r.stderr
    res = json.loads(r.stdout)
    assert res["passed"] is True
    assert res["metrics"]["end_card_start"] == pytest.approx(built["end_card_start"], abs=0.05)


def test_loudness_pass(built):
    r = cli(PREFLIGHT / "loudness.py", VIDEO, "--voice", built["voice"], "--sfx", built["sfx"], "--json")
    save("loudness", r)
    assert r.returncode == 0, r.stdout + r.stderr
    res = json.loads(r.stdout)
    assert res["passed"] is True
    assert "sfx_under_voice_db" in res["metrics"]  # the SFX rule actually ran


def test_legibility_pass(built):
    r = cli(PREFLIGHT / "legibility.py", built["project"], "--json")
    save("legibility", r)
    assert r.returncode == 0, r.stdout + r.stderr
    res = json.loads(r.stdout)
    assert res["passed"] is True
    m = res["metrics"]
    assert m["text_runs_measured"] > 0  # the end card's DOM text
    assert m["min_rendered_px_seen"] >= 44


def test_evidence_report_all_checks_pass(built):
    out = RUN / "report"
    r = cli(EVIDENCE, "report", VIDEO, "--out", out, "--words", WORDS,
            "--run", "dead_frames {video}",
            "--run", f"endcard {{video}} {WORDS}",
            "--run", f"loudness {{video}} --voice {built['voice']} --sfx {built['sfx']}",
            "--run", f"legibility {built['project']}")
    save("evidence_cli", r)
    assert r.returncode == 0, r.stdout + r.stderr
    rep = json.loads((out / "preflight_report.json").read_text())
    assert rep["passed"] is True
    assert sorted(c["name"] for c in rep["checks"]) == ["dead_frames", "endcard", "legibility", "loudness"]
    assert all(c["passed"] for c in rep["checks"])


# ---------------------------------------------------------------- planted negative + originals


@pytest.mark.studio
def test_planted_negative_canonical_cut15_fails_endcard():
    r = cli(PREFLIGHT / "endcard.py", CANON_CUT / "look.mp4", WORDS, "--json")
    save("negative_canonical_cut15_endcard", r)
    assert r.returncode == 1, r.stdout + r.stderr
    kinds = {f["kind"] for f in json.loads(r.stdout)["findings"]}
    assert {"covers_last_word", "too_short"} <= kinds


@pytest.mark.studio
def test_canonical_sources_untouched():
    assert CANON_SHA, "canonical sources were not hashed at import"
    for p, h in CANON_SHA.items():
        assert sha(p) == h, p
