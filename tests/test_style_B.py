"""Style B (Cinematic depth) template: build with the S49 15 s inputs, render, and gate on preflight.

Real runs only: styles/B/build.py builds the project and renders it with hyperframes@0.8.183
under the offline sandbox (-w 2); then every preflight CLI (items 3-6) and the item 7 evidence
report run on the real render. Each CLI's stdout is saved under BUILD/preflight/ verbatim.
Planted negatives reuse the real render or project and must FAIL the same CLIs.
Everything is written under pytest tmp dirs. The S49 inputs and the canonical dir-B come from
SHORTS_STUDIO_DIR; tests that need them skip without it (see conftest.py).
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PLUGIN = Path(__file__).resolve().parents[1]
BUILD_PY = PLUGIN / "styles" / "B" / "build.py"
PRE = PLUGIN / "preflight"
EVIDENCE = PLUGIN / "evidence" / "evidence.py"
_STUDIO_DIR = os.environ.get("SHORTS_STUDIO_DIR")
STUDIO = Path(_STUDIO_DIR) if _STUDIO_DIR else None
if STUDIO is not None:
    INPUTS = STUDIO / "_kit/lookdev/inputs"
    WAV = INPUTS / "r6-S49-cut15.wav"
    WORDS = INPUTS / "r6-S49-cut15.words.json"
    CANON = STUDIO / "_kit/lookdev/dir-B"
else:
    INPUTS = WAV = WORDS = CANON = None
STUDIO_REQUIRED = (WAV, WORDS, CANON)
ROOT = BUILD = VIDEO = OUT = None  # scratch, bound per module by _scratch()
FPS = 30


def run(cmd, **kw):
    kw.setdefault("timeout", 600)
    return subprocess.run([str(c) for c in cmd], capture_output=True, text=True, **kw)


def ff(*args):
    r = run(["ffmpeg", "-hide_banner", "-nostdin", "-y", "-loglevel", "error", *args])
    assert r.returncode == 0, r.stderr


def preflight(name, *args, save=None):
    r = run([sys.executable, PRE / f"{name}.py", *args, "--json"], timeout=1200)
    if save:
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / save).write_text(r.stdout + ("\n--- stderr ---\n" + r.stderr if r.stderr else ""))
    assert r.returncode in (0, 1), f"{name} exit {r.returncode}: {r.stderr}"
    return r.returncode, json.loads(r.stdout)


@pytest.fixture(scope="module", autouse=True)
def _scratch(tmp_path_factory):
    global ROOT, BUILD, VIDEO, OUT
    ROOT = tmp_path_factory.mktemp("style-B")
    BUILD = ROOT / "s49-cut15"
    VIDEO = BUILD / "styleB-s49-cut15.mp4"
    OUT = BUILD / "preflight"


@pytest.fixture
def scratch(tmp_path):
    """Per-test scratch dir (pytest tmp_path)."""
    return tmp_path


@pytest.fixture(scope="module")
def built():
    canon_mtime = (CANON / "index.html").stat().st_mtime_ns
    BUILD.mkdir(parents=True, exist_ok=True)
    r = run([sys.executable, BUILD_PY, "--wav", WAV, "--words", WORDS, "--out", BUILD,
             "--eyebrow", "Effective challenge", "--render", VIDEO, "--quality", "delivery",
             "--workers", "2", "--json"], timeout=2400)
    assert r.returncode == 0, r.stderr[-3000:]
    m = json.loads(r.stdout)
    assert VIDEO.is_file()
    assert (CANON / "index.html").stat().st_mtime_ns == canon_mtime  # original untouched
    return m


# ------------------------------------------------------------------ build contract (no render)

def test_build_refuses_words_past_duration(scratch):  # planted negative
    r = run([sys.executable, BUILD_PY, "--wav", WAV, "--words", WORDS, "--out", scratch / "b",
             "--duration", "12.0"])
    assert r.returncode == 2
    assert "after the voice duration" in r.stderr


def test_build_refuses_long_hud_string(scratch):  # planted negative
    r = run([sys.executable, BUILD_PY, "--wav", WAV, "--words", WORDS, "--out", scratch / "b",
             "--hud-run", "THIS RUN LABEL IS FAR TOO LONG"])
    assert r.returncode == 2
    assert "HUD budget" in r.stderr


def test_build_is_script_agnostic(built):
    cfg = json.loads(Path(built["config"]).read_text())
    words = json.loads(WORDS.read_text())
    cue_text = " ".join(w["text"] for c in cfg["cues"] for w in c["words"])
    assert cue_text == " ".join(w["text"] for w in words)
    last_end = max(w["end"] for w in words)
    assert cfg["lastWordEnd"] == pytest.approx(last_end)
    assert cfg["start"] >= last_end - 0.05
    assert cfg["end"] - max(cfg["start"], last_end) >= 1.5
    html = (Path(built["project"]) / "index.html").read_text()
    for token in ("__END__", "__CONFIG_JSON__", "__LOGO__"):
        assert token not in html
    assert f'data-duration="{cfg["end"]:.6f}"' in html
    assert "s61" not in html.lower() and "Run 0461" not in html  # no script-specific leftovers


def test_render_was_offline(built):
    log = (BUILD / "render.log").read_text()
    assert "fonts.googleapis" not in log and "Google Fonts" not in log


# ------------------------------------------------------------------ preflight on the real render

def test_dead_frames_pass(built):
    code, res = preflight("dead_frames", VIDEO, save="dead_frames.json")
    assert code == 0 and res["passed"] is True, res["findings"]


def test_endcard_pass(built):
    code, res = preflight("endcard", VIDEO, built["words"], save="endcard.json")
    assert code == 0 and res["passed"] is True, res["findings"]
    m = res["metrics"]
    assert m["end_card_start"] == pytest.approx(built["plan"]["start"], abs=1.5 / FPS)
    assert m["hold_after_last_word"] >= 1.5


def test_loudness_pass_with_stems(built):
    code, res = preflight("loudness", VIDEO, "--voice", built["voice_norm"], "--sfx", built["sfx_bus"],
                          save="loudness.json")
    assert code == 0 and res["passed"] is True, res["findings"]
    assert "sfx_under_voice_db" in res["metrics"]  # the SFX rule ran (not "skipped")


def test_loudness_pass_on_delivered_mix(built):
    code, res = preflight("loudness", VIDEO, save="loudness_mix.json")
    assert code == 0 and res["passed"] is True, res["findings"]


def test_legibility_pass(built):
    code, res = preflight("legibility", built["project"], save="legibility.json")
    assert code == 0 and res["passed"] is True, res["findings"]
    m = res["metrics"]
    assert m["page_errors"] == []
    speech = [s for s in m["per_sample"] if s["t"] < built["plan"]["start"]]
    assert speech and all(s["lower_third"] == "filled" for s in speech)


def test_evidence_report_pass(built):
    rep = BUILD / "evidence"
    r = run([sys.executable, EVIDENCE, "report", VIDEO, "--out", rep, "--words", built["words"],
             "--run", "dead_frames {video}",
             "--run", f"endcard {{video}} {built['words']}",
             "--run", f"loudness {{video}} --voice {built['voice_norm']} --sfx {built['sfx_bus']}",
             "--run", f"legibility {built['project']}"], timeout=2400)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "evidence_report.txt").write_text(r.stdout + "\n--- stderr ---\n" + r.stderr)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-3000:]
    report = json.loads((rep / "preflight_report.json").read_text())
    assert report["passed"] is True
    assert sorted(c["name"] for c in report["checks"]) == ["dead_frames", "endcard", "legibility", "loudness"]
    assert all(c["passed"] for c in report["checks"])
    assert report["sheets"]


# ------------------------------------------------------------------ planted negatives on the same artefacts

def test_planted_frozen_clip_fails_dead_frames(built, scratch):
    frozen = scratch / "frozen.mp4"
    ff("-ss", "6", "-i", str(VIDEO), "-frames:v", "1", str(scratch / "f.png"))
    ff("-loop", "1", "-i", str(scratch / "f.png"), "-t", "2", "-r", "30", "-pix_fmt", "yuv420p", str(frozen))
    code, res = preflight("dead_frames", frozen)
    assert code == 1 and res["passed"] is False


def test_planted_late_word_fails_endcard(built, scratch):
    words = json.loads(Path(built["words"]).read_text())
    words.append({"text": "late", "start": built["plan"]["end"] - 0.9, "end": built["plan"]["end"] - 0.5})
    wp = scratch / "words.json"
    wp.write_text(json.dumps(words))
    code, res = preflight("endcard", VIDEO, wp)
    assert code == 1 and res["passed"] is False


def test_planted_quiet_mix_fails_loudness(built, scratch):
    quiet = scratch / "quiet.wav"
    ff("-i", str(VIDEO), "-vn", "-af", "volume=-8dB", str(quiet))
    code, res = preflight("loudness", quiet)
    assert code == 1 and any(f["kind"] == "lufs_out_of_range" for f in res["findings"])


def test_planted_small_hud_fails_legibility(built, scratch):
    proj = scratch / "project"
    shutil.copytree(built["project"], proj, dirs_exist_ok=True)
    html = (proj / "index.html").read_text()
    html = html.replace("</style>", ".hud { font-size: 22px !important; }\n</style>", 1)
    (proj / "index.html").write_text(html)
    code, res = preflight("legibility", proj)
    assert code == 1 and any(f["kind"] == "small_type" for f in res["findings"])
