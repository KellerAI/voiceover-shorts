"""Style C template (styles/C): instantiate with the S49 15 s inputs, render, and gate on every preflight.

Real runs only: the builder, an offline sandboxed HyperFrames render, and the item 3/4/5/6 CLIs plus
the item 7 report. Renders and scratch go under pytest tmp dirs.
Planted negative: the canonical dir-C project (copied, never edited) fails the legibility check that
the template passes. The S49 inputs and the canonical dir-C come from SHORTS_STUDIO_DIR; tests that
need them skip without it (see conftest.py).
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
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "styles" / "C"))

import build as style_c  # styles/C/build.py

PLUGIN = Path(__file__).resolve().parents[1]
PREFLIGHT = PLUGIN / "preflight"
EVIDENCE = PLUGIN / "evidence" / "evidence.py"
_STUDIO_DIR = os.environ.get("SHORTS_STUDIO_DIR")
STUDIO = Path(_STUDIO_DIR) if _STUDIO_DIR else None
if STUDIO is not None:
    INPUTS = STUDIO / "_kit/lookdev/inputs"
    WAV = INPUTS / "r6-S49-cut15.wav"
    WORDS = INPUTS / "r6-S49-cut15.words.json"
    CANON = STUDIO / "_kit/lookdev/dir-C"
    CANON_FILES = [CANON / "project/index.html", CANON / "notes.md", CANON / "sfx_gen.py", CANON / "lookC.mp4"]
else:
    INPUTS = WAV = WORDS = CANON = None
    CANON_FILES = []
STUDIO_REQUIRED = (WAV, WORDS, CANON, *CANON_FILES)
BUILD = RUN = VIDEO = EVID = None  # scratch, bound per module by _scratch()


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


CANON_SHA = ({str(p): sha(p) for p in CANON_FILES}
             if CANON_FILES and all(p.is_file() for p in CANON_FILES) else {})


@pytest.fixture(scope="module", autouse=True)
def _scratch(tmp_path_factory):
    global BUILD, RUN, VIDEO, EVID
    BUILD = tmp_path_factory.mktemp("style-C")
    RUN = BUILD / "s49-cut15"
    VIDEO = RUN / "styleC-S49-cut15.mp4"
    EVID = RUN / "evidence"


def cli(*args):
    r = subprocess.run([sys.executable, *map(str, args)], capture_output=True, text=True, timeout=1800)
    return r


def save(name, r):
    EVID.mkdir(parents=True, exist_ok=True)
    (EVID / f"{name}.json").write_text(r.stdout)
    (EVID / f"{name}.stderr.txt").write_text(r.stderr)


# ---------------------------------------------------------------- pure planning


@pytest.mark.studio
def test_segment_s49_into_three_sentences():
    acts = style_c.segment_acts(style_c.load_words(WORDS))
    assert [a[-1]["text"] for a in acts] == ["Challenge.", "work.", "banking."]


def test_segment_without_punctuation_still_three_nonempty_acts():
    ws = [{"text": f"w{i}", "start": i * 0.5, "end": i * 0.5 + 0.4} for i in range(9)]
    acts = style_c.segment_acts(ws)
    assert [len(a) for a in acts] == [3, 3, 3]


@pytest.mark.studio
def test_plan_end_card_after_last_word_with_hold():
    ws = style_c.load_words(WORDS)
    T = style_c.plan_timeline(ws, 15.066667)["T"]
    assert T["ecStart"] >= T["lastWordEnd"]
    assert T["END"] - max(T["ecStart"], T["lastWordEnd"]) >= 1.5
    assert T["leg1"][1] < T["ticks"][0] and T["leg2"][1] < T["checks"][0]
    assert T["checks"] == sorted(T["checks"]) and T["ticks"] == sorted(T["ticks"])


def test_too_few_words_is_usage_error():
    with pytest.raises(style_c.BuildError):
        style_c.load_words([{"text": "hi", "start": 0, "end": 1}])


# ---------------------------------------------------------------- build + render


@pytest.fixture(scope="module")
def built():
    proj = RUN / "project"
    m = style_c.build(WAV, WORDS, proj)
    style_c.render(proj, VIDEO, quality="looks", workers=2)
    return m


def test_fonts_are_bundled_ofl_only(built):
    proj = Path(built["project"])
    html = (proj / "index.html").read_text()
    for bad in ("SF Pro", "SF Mono", "SFNS", "Comic Sans", "Marker Felt", "Chalkduster"):
        assert bad not in html
    fonts = sorted(p.name for p in (proj / "assets/fonts").iterdir())
    assert fonts == ["ComicShanns.woff2", "LilitaOne.woff2"]
    for f in fonts:
        assert sha(proj / "assets/fonts" / f) == sha(PLUGIN / "kit/fonts" / f)
    assert sha(proj / "kit/endcard.js") == sha(PLUGIN / "kit/endcard.js")


def test_render_duration_matches_plan(built):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(VIDEO)],
                       capture_output=True, text=True, check=True)
    assert float(r.stdout) == pytest.approx(built["duration"], abs=0.1)


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
    assert res["metrics"]["text_runs_measured"] > 0


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
def test_planted_negative_canonical_dir_c_fails_legibility(tmp_path):
    neg = tmp_path / "negative" / "dir-C-project"
    shutil.copytree(CANON / "project", neg)
    r = cli(PREFLIGHT / "legibility.py", neg, "--json")
    save("negative_dir_c_legibility", r)
    assert r.returncode == 1, r.stdout + r.stderr
    kinds = {f["kind"] for f in json.loads(r.stdout)["findings"]}
    assert "small_type" in kinds


@pytest.mark.studio
def test_canonical_sources_untouched():
    assert CANON_SHA, "canonical sources were not hashed at import"
    for p, h in CANON_SHA.items():
        assert sha(p) == h, p
