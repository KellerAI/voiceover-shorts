import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from preflight import loudness  # noqa: E402

CLI = Path(__file__).resolve().parents[1] / "preflight" / "loudness.py"


def synth(path, expr, dur=8):
    """Real ffmpeg synthesis: sine tone shaped by volume expr (dB string)."""
    subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-y", "-f", "lavfi", "-i",
                    f"sine=frequency=440:duration={dur}:sample_rate=48000",
                    "-af", expr, "-ac", "2", str(path)], check=True, capture_output=True)
    return str(path)


def tuned(tmp_path, name, target):
    """Make a tone whose measured LUFS is within 0.05 of target (iterative, real measurement)."""
    gain = target + 3.0  # sine: LUFS ~= peak dB - 3 roughly; refine below
    p = ""
    for _ in range(4):
        p = synth(tmp_path / f"{name}.wav", f"volume={gain}dB")
        m = loudness.measure(p)["lufs"]
        if abs(m - target) < 0.05:
            break
        gain += target - m
    return p


@pytest.fixture(scope="module")
def good(tmp_path_factory):
    d = tmp_path_factory.mktemp("good")
    voice = tuned(d, "voice", -14.0)
    sfx = synth(d / "sfx.wav", "volume=-40dB")
    return voice, sfx


def test_pass_mix_only(good):
    r = loudness.check(mix=good[0])
    assert r["passed"] and r["findings"] == []
    assert abs(r["metrics"]["lufs"] + 14) < 0.1
    assert "skipped" in r["metrics"]["sfx_check"]


def test_pass_with_stems(good):
    r = loudness.check(mix=good[0], voice=good[0], sfx=good[1])
    assert r["passed"], r
    assert r["metrics"]["sfx_under_voice_db"] >= 12


def test_fail_too_quiet(tmp_path):  # planted negative
    p = tuned(tmp_path, "quiet", -20.0)
    r = loudness.check(mix=p)
    assert not r["passed"]
    assert [f["kind"] for f in r["findings"]] == ["lufs_out_of_range"]


def test_fail_true_peak(tmp_path):  # planted negative: right loudness, hot peak
    # Bed near target (native sine peak is -18 dB) plus a short burst reaching ~-0 dBFS.
    p = tmp_path / "peaky.wav"
    subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-y", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=8:sample_rate=48000", "-af",
                    "volume=4dB,volume=15dB:enable='between(t,3,3.05)'",
                    "-ac", "2", str(p)], check=True, capture_output=True)
    r = loudness.check(mix=str(p))
    assert any(f["kind"] == "true_peak_too_high" for f in r["findings"]), r["metrics"]


def test_fail_sfx_too_loud(good, tmp_path):  # planted negative
    sfx = synth(tmp_path / "loud_sfx.wav", "volume=0dB")
    r = loudness.check(voice=good[0], sfx=sfx)
    assert [f["kind"] for f in r["findings"]] == ["sfx_too_loud"]


def test_cli_exit_codes_and_json(good, tmp_path):
    ok = subprocess.run([sys.executable, str(CLI), good[0], "--voice", good[0],
                         "--sfx", good[1], "--json"], capture_output=True, text=True)
    assert ok.returncode == 0, ok.stderr
    d = json.loads(ok.stdout)
    assert set(d) == {"name", "passed", "findings", "metrics"} and d["name"] == "loudness"
    bad = synth(tmp_path / "bad.wav", "volume=-40dB")
    assert subprocess.run([sys.executable, str(CLI), bad, "--json"],
                          capture_output=True, text=True).returncode == 1
    assert subprocess.run([sys.executable, str(CLI), str(tmp_path / "nope.wav"), "--json"],
                          capture_output=True, text=True).returncode == 2
    assert subprocess.run([sys.executable, str(CLI), "--json"],
                          capture_output=True, text=True).returncode == 2
