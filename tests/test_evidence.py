import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from evidence import evidence as ev  # noqa: E402

SCRIPT = Path(__file__).resolve().parents[1] / "evidence" / "evidence.py"


def _mk(path: Path, expr: str, dur: int):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", expr,
                    "-t", str(dur), "-pix_fmt", "yuv420p", str(path)], check=True)


@pytest.fixture(scope="module")
def clip(tmp_path_factory):
    p = tmp_path_factory.mktemp("c") / "clip.mp4"
    _mk(p, "testsrc=size=320x240:rate=24", 8)
    return p


def cli(*a):
    return subprocess.run([sys.executable, str(SCRIPT), *a], capture_output=True, text=True)


def test_load_beats_words_and_thinning(tmp_path):
    w = tmp_path / "w.json"
    w.write_text(json.dumps({"words": [{"word": "a", "start": 0.0}, {"word": "b", "start": 0.5},
                                       {"word": "c", "start": 2.1}, {"word": "d", "start": 5.0}]}))
    assert ev.load_beats(w, None, 2.0) == [0.0, 2.1, 5.0]
    assert ev.load_beats(None, "4,1", 0.0) == [1.0, 4.0]


def test_load_beats_errors(tmp_path):
    with pytest.raises(ev.EvidenceError):
        ev.load_beats(None, None, 2.0)
    bad = tmp_path / "b.json"
    bad.write_text('[{"word": "x"}]')
    with pytest.raises(ev.EvidenceError):
        ev.load_beats(bad, None, 2.0)


def test_sample_per_scene():
    beats = [1, 3, 5, 12, 14]
    got = ev.sample_per_scene([float(b) for b in beats], [10.0], 1)
    assert got == [(1.0, 0), (12.0, 1)]


def test_sheets_real_ffmpeg(clip, tmp_path):
    r = cli("sheets", str(clip), "--out", str(tmp_path), "--beats", "1,4,7.5")
    assert r.returncode == 0, r.stderr
    entries = json.loads((tmp_path / "sheets.json").read_text())
    assert len(entries) == 3
    assert all(e["frames"] == 8 for e in entries)
    from_png = [Path(e["path"]) for e in entries]
    assert all(p.stat().st_size > 1000 and p.read_bytes()[:4] == b"\x89PNG" for p in from_png)
    # 4 columns x 2 rows of 240px-wide tiles + padding => wider than one frame
    w = int.from_bytes(from_png[0].read_bytes()[16:20], "big")
    assert w >= 4 * 240


def test_beat_past_end_dropped(clip, tmp_path):
    r = cli("sheets", str(clip), "--out", str(tmp_path), "--beats", "2,99")
    assert r.returncode == 0, r.stderr
    assert len(json.loads((tmp_path / "sheets.json").read_text())) == 1


def test_long_video_scene_sampling(tmp_path):
    # 2 scenes (testsrc then color flash) of 4 s each; treat >5s as long
    a, b, v = tmp_path / "a.mp4", tmp_path / "b.mp4", tmp_path / "v.mp4"
    _mk(a, "testsrc=size=320x240:rate=24", 4)
    _mk(b, "color=c=red:size=320x240:rate=24", 4)
    lst = tmp_path / "l.txt"
    lst.write_text(f"file '{a}'\nfile '{b}'\n")
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
                    "-i", str(lst), "-c", "copy", str(v)], check=True)
    out = tmp_path / "o"
    r = cli("sheets", str(v), "--out", str(out), "--beats", "0.5,1.5,2.5,5,6", "--min-gap", "0.5",
            "--long-after", "5", "--per-scene", "1")
    assert r.returncode == 0, r.stderr
    entries = json.loads((out / "sheets.json").read_text())
    assert [e["scene"] for e in entries] == [0, 1]
    assert len(entries) == 2  # 5 beats in, 1 per scene out


def _fake_pre(tmp_path, name, passed):
    d = {"name": name, "passed": passed, "findings": [] if passed else
         [{"t_start": 1, "t_end": 2, "kind": "x", "detail": "d"}], "metrics": {"m": 1}}
    f = tmp_path / f"{name}.json"
    f.write_text(json.dumps(d))
    return f


def test_report_merge_pass_and_planted_fail(clip, tmp_path):
    ok = _fake_pre(tmp_path, "good", True)
    bad = _fake_pre(tmp_path, "bad", False)
    r = cli("report", str(clip), "--out", str(tmp_path / "r1"), "--beats", "1", "--merge", str(ok))
    assert r.returncode == 0, r.stderr
    rep = json.loads((tmp_path / "r1" / "preflight_report.json").read_text())
    assert rep["passed"] is True and rep["checks"][0]["name"] == "good" and len(rep["sheets"]) == 1
    r = cli("report", str(clip), "--out", str(tmp_path / "r2"), "--beats", "1",
            "--merge", str(ok), "--merge", str(bad))
    assert r.returncode == 1
    rep = json.loads((tmp_path / "r2" / "preflight_report.json").read_text())
    assert rep["passed"] is False and [c["passed"] for c in rep["checks"]] == [True, False]


def test_run_real_preflight_cli_subprocess(clip, tmp_path, monkeypatch):
    pre = tmp_path / "pre"
    pre.mkdir()
    (pre / "stub.py").write_text(
        "import sys, json\n"
        "bad = 'FAIL' in sys.argv\n"
        "print(json.dumps({'name':'stub','passed':not bad,'findings':[],'metrics':{}}))\n"
        "sys.exit(1 if bad else 0)\n")
    monkeypatch.setattr(ev, "PREFLIGHT_DIR", pre)
    assert ev.run_preflight("stub {video}", clip)["passed"] is True
    assert ev.run_preflight("stub FAIL", clip)["passed"] is False
    missing = ev.run_preflight("nope", clip)
    assert missing["findings"][0]["kind"] == "runner_error"


def test_usage_errors_exit_2(clip, tmp_path):
    assert cli("sheets", str(tmp_path / "missing.mp4"), "--out", str(tmp_path), "--beats", "1").returncode == 2
    assert cli("sheets", str(clip), "--out", str(tmp_path)).returncode == 2
    assert cli("bogus").returncode == 2
