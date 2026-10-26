"""Tests for preflight/endcard.py (item 4). Videos are real ffmpeg-generated clips."""
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from preflight import endcard

SCRIPT = Path(__file__).resolve().parents[1] / "preflight" / "endcard.py"
LOOKDEV_WORDS = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "r2-15s.words.json"


def make_video(path, segments):
    """segments: list of (kind, seconds); kind in body|card|black. 180x320 @30fps."""
    srcs = {"body": "testsrc=s=180x320:r=30:d={d}",
            "card": "color=c=0x2a62b0:s=180x320:r=30:d={d}",
            "black": "color=c=black:s=180x320:r=30:d={d}"}
    cmd = ["ffmpeg", "-v", "error", "-y"]
    for kind, d in segments:
        cmd += ["-f", "lavfi", "-i", srcs[kind].format(d=d)]
    n = len(segments)
    cmd += ["-filter_complex", "".join(f"[{i}:v]" for i in range(n)) + f"concat=n={n}:v=1:a=0,format=yuv420p",
            "-c:v", "libx264", "-preset", "ultrafast", str(path)]
    subprocess.run(cmd, check=True)
    return path


def make_words(path, last_end, fmt="list"):
    words = [{"id": "w0", "text": "hi", "start": 0.1, "end": 0.5},
             {"id": "w1", "text": "there", "start": last_end - 0.4, "end": last_end}]
    path.write_text(json.dumps(words if fmt == "list" else {"words": words}))
    return path


def kinds(res):
    return {f["kind"] for f in res["findings"]}


def test_good_case_passes(tmp_path):
    v = make_video(tmp_path / "good.mp4", [("body", 3), ("card", 2)])
    w = make_words(tmp_path / "w.json", 3.0)
    res = endcard.check(v, w)
    assert res["passed"], res
    assert res["findings"] == []
    assert res["metrics"]["end_card_start"] == pytest.approx(3.0, abs=0.1)
    assert res["metrics"]["hold_after_last_word"] >= 1.5


def test_planted_short_end_card_fails(tmp_path):
    v = make_video(tmp_path / "short.mp4", [("body", 3), ("card", 0.3)])
    res = endcard.check(v, make_words(tmp_path / "w.json", 3.0))
    assert not res["passed"]
    assert "too_short" in kinds(res)
    assert res["metrics"]["hold_after_last_word"] < 0.5


def test_planted_cut_to_black_fails(tmp_path):
    v = make_video(tmp_path / "black.mp4", [("body", 3), ("card", 2), ("black", 0.5)])
    res = endcard.check(v, make_words(tmp_path / "w.json", 3.0))
    assert not res["passed"]
    assert "black_frame" in kinds(res)
    # the black cut must not be mistaken for the card start
    assert res["metrics"]["end_card_start"] == pytest.approx(3.0, abs=0.1)
    black = [f for f in res["findings"] if f["kind"] == "black_frame"][0]
    assert black["t_start"] == pytest.approx(5.0, abs=0.1)


def test_card_covering_last_word_fails(tmp_path):
    v = make_video(tmp_path / "cover.mp4", [("body", 2), ("card", 3)])
    res = endcard.check(v, make_words(tmp_path / "w.json", 3.0))
    assert not res["passed"]
    assert "covers_last_word" in kinds(res)


def test_card_not_found_when_no_cut(tmp_path):
    v = make_video(tmp_path / "nocut.mp4", [("card", 4)])
    res = endcard.check(v, make_words(tmp_path / "w.json", 2.0))
    assert not res["passed"] and "end_card_not_found" in kinds(res)


def test_explicit_method_and_dict_words_format(tmp_path):
    v = make_video(tmp_path / "good.mp4", [("body", 3), ("card", 2)])
    w = make_words(tmp_path / "w.json", 3.0, fmt="dict")
    assert endcard.check(v, w, end_card_start=3.0)["passed"]
    assert not endcard.check(v, w, end_card_start=4.5)["passed"]  # explicit start leaves 0.5 s


def test_real_lookdev_words_format_parses():
    assert LOOKDEV_WORDS.exists()
    assert endcard.load_last_word_end(LOOKDEV_WORDS) > 5


def test_cli_exit_codes_and_json_shape(tmp_path):
    good = make_video(tmp_path / "good.mp4", [("body", 3), ("card", 2)])
    bad = make_video(tmp_path / "bad.mp4", [("body", 3), ("card", 0.3)])
    w = make_words(tmp_path / "w.json", 3.0)

    def run(*a):
        return subprocess.run([sys.executable, str(SCRIPT), *map(str, a)], capture_output=True, text=True)

    r = run(good, w, "--json")
    assert r.returncode == 0, r.stderr
    d = json.loads(r.stdout)
    assert set(d) == {"name", "passed", "findings", "metrics"} and d["name"] == "endcard" and d["passed"] is True
    r = run(bad, w, "--json")
    assert r.returncode == 1
    f = json.loads(r.stdout)["findings"][0]
    assert set(f) == {"t_start", "t_end", "kind", "detail"}
    assert run(tmp_path / "missing.mp4", w, "--json").returncode == 2
    assert run(good, tmp_path / "nope.json", "--json").returncode == 2
    assert run().returncode == 2
