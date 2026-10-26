"""Tests for preflight/dead_frames.py (item 3).

Clips are generated with real ffmpeg at test time; no mocks. The planted bad
clip is 4 s at 30 fps: 0-1 s moving testsrc2, 1-2 s black, 2-3 s one frame
held (frozen), 3-4 s moving testsrc2. The clean clip is 4 s of testsrc2.
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from preflight import dead_frames

SCRIPT = Path(__file__).resolve().parents[1] / "preflight" / "dead_frames.py"
SIZE = "270x480"
FRAME = 1 / 30


def test_ffmpeg_present():
    # Fail closed: a missing ffmpeg must not turn this suite into a zero-run green.
    assert shutil.which("ffmpeg") and shutil.which("ffprobe")


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True, timeout=120)


@pytest.fixture(scope="module")
def clips(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    d = tmp_path_factory.mktemp("dead_frames")
    clean, bad = d / "clean.mp4", d / "bad.mp4"
    _ffmpeg(
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=s={SIZE}:r=30:d=4",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        str(clean),
    )
    graph = (
        f"testsrc2=s={SIZE}:r=30:d=1[a];"
        f"color=c=black:s={SIZE}:r=30:d=1[b];"
        f"testsrc2=s={SIZE}:r=30:d=1,trim=end_frame=1,tpad=stop_mode=clone:stop=29[c];"
        f"testsrc2=s={SIZE}:r=30:d=1[e];"
        "[a][b][c][e]concat=n=4:v=1:a=0,format=yuv420p[out]"
    )
    _ffmpeg("-filter_complex", graph, "-map", "[out]", "-c:v", "libx264", str(bad))
    return {"clean": clean, "bad": bad}


def _run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )


def _json(stdout: str) -> dict:
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        pytest.fail(f"CLI did not print JSON: {exc}: {stdout!r}")


def _assert_contract(result: dict) -> None:
    assert set(result) == {"name", "passed", "findings", "metrics"}
    assert result["name"] == "dead_frames"
    assert isinstance(result["passed"], bool)
    assert isinstance(result["metrics"], dict)
    for f in result["findings"]:
        assert set(f) == {"t_start", "t_end", "kind", "detail"}
        assert f["kind"] in {"black", "freeze"}
        assert f["t_end"] > f["t_start"]


def test_planted_bad_clip_fails_with_both_spans(clips):
    result = dead_frames.check(clips["bad"])
    _assert_contract(result)
    assert not result["passed"]
    kinds = {f["kind"]: f for f in result["findings"]}
    assert set(kinds) == {"black", "freeze"}, result["findings"]
    black, freeze = kinds["black"], kinds["freeze"]
    assert black["t_start"] == pytest.approx(1.0, abs=FRAME)
    assert black["t_end"] == pytest.approx(2.0, abs=FRAME)
    assert freeze["t_start"] == pytest.approx(2.0, abs=FRAME)
    assert freeze["t_end"] == pytest.approx(3.0, abs=2 * FRAME)
    assert result["metrics"]["black_spans"] == 1
    assert result["metrics"]["freeze_spans"] == 1
    assert result["metrics"]["duration_s"] == pytest.approx(4.0, abs=FRAME)


def test_clean_moving_clip_passes(clips):
    result = dead_frames.check(clips["clean"])
    _assert_contract(result)
    assert result["passed"]
    assert result["findings"] == []
    assert result["metrics"]["flagged_s"] == 0


def test_min_dur_above_planted_spans_passes(clips):
    # Planted spans are ~1 s; a 1.5 s floor must ignore them (threshold is live).
    result = dead_frames.check(clips["bad"], min_dur=1.5)
    assert result["passed"]
    assert result["metrics"]["thresholds"]["min_dur"] == 1.5


def test_cli_json_exit_codes(clips):
    bad = _run_cli(str(clips["bad"]), "--json")
    assert bad.returncode == 1, bad.stderr
    _assert_contract(_json(bad.stdout))

    clean = _run_cli(str(clips["clean"]), "--json")
    assert clean.returncode == 0, clean.stderr
    clean_result = _json(clean.stdout)
    _assert_contract(clean_result)
    assert clean_result["passed"]


def test_cli_io_and_usage_errors(tmp_path):
    missing = _run_cli(str(tmp_path / "nope.mp4"), "--json")
    assert missing.returncode == 2
    assert "no such file" in missing.stderr

    not_video = tmp_path / "x.mp4"
    not_video.write_text("not a video")
    assert _run_cli(str(not_video), "--json").returncode == 2

    assert _run_cli().returncode == 2
    assert _run_cli("a.mp4", "--noise", "abc").returncode == 2


def test_parse_spans_freeze_to_eof_and_dedupe():
    log = (
        "black_start:1 black_end:2 black_duration:1\n"
        "lavfi.freezedetect.freeze_start: 1\n"
        "lavfi.freezedetect.freeze_end: 2\n"
        "lavfi.freezedetect.freeze_start: 5.5\n"
    )
    freeze, black = dead_frames.parse_spans(log, duration=7.0)
    assert freeze == [(1.0, 2.0), (5.5, 7.0)]
    assert black == [(1.0, 2.0)]
    findings = dead_frames.build_findings(freeze, black, min_dur=0.5)
    # The 1-2 s freeze is the black span; it is reported once, as black.
    assert [(f["kind"], f["t_start"], f["t_end"]) for f in findings] == [
        ("black", 1.0, 2.0),
        ("freeze", 5.5, 7.0),
    ]


def test_adjacent_freeze_spans_are_not_merged():
    log = "".join(
        f"lavfi.freezedetect.freeze_start: {s}\nlavfi.freezedetect.freeze_end: {e}\n"
        for s, e in [(1.0, 1.6), (1.6, 2.2)]
    )
    freeze, black = dead_frames.parse_spans(log, duration=3.0)
    findings = dead_frames.build_findings(freeze, black, min_dur=0.5)
    assert len(findings) == 2
