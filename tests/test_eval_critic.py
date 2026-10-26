"""Tests for evals/eval_critic.py.

The critic is a real model call in production. Tests marked STUB below replace it with a
labelled stub so the ranking, gating and plumbing logic can be checked offline. A stub
result is never acceptance evidence; only a live critic run is.
Evidence generation, ffmpeg and the calibration file are real in every test.
Tests that read real renders need $SHORTS_STUDIO_DIR and skip when it is unset.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from evals import eval_critic as ec  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "evals" / "eval_critic.py"
needs_studio = pytest.mark.skipif(not os.environ.get("SHORTS_STUDIO_DIR", "").strip(),
                                  reason="SHORTS_STUDIO_DIR unset; real renders unavailable")


# ---------------------------------------------------------------- ranks / Spearman

def test_average_ranks_ties():
    assert ec.average_ranks([1, 2, 3, 7, 7, 7]) == [1, 2, 3, 5, 5, 5]
    assert ec.average_ranks([9, 5, 5, 1], descending=True) == [1, 2.5, 2.5, 4]


def test_spearman_perfect_and_reversed():
    j: dict[str, float] = {"D": 1, "C": 2, "E": 3}
    assert ec.spearman(j, {"D": 6, "C": 5, "E": 4})["rho"] == 1.0
    assert ec.spearman(j, {"D": 4, "C": 5, "E": 6})["rho"] == -1.0


def test_spearman_constant_scores_is_none():
    assert ec.spearman({"D": 1, "C": 2}, {"D": 5, "C": 5})["rho"] is None


def test_spearman_matches_scipy_with_ties():
    stats = pytest.importorskip("scipy.stats")  # optional cross-check dependency
    j = {"D": 1.0, "C": 2.0, "E": 3.0, "A": 7.0, "G": 7.0, "H": 7.0}
    c = {"D": 5.5, "C": 6.0, "E": 5.0, "A": 4.0, "G": 4.5, "H": 4.0}
    got = ec.spearman(j, c)
    subj = got["subjects"]
    want = stats.spearmanr([-j[s] for s in subj], [c[s] for s in subj]).statistic
    assert got["rho"] == pytest.approx(want, abs=1e-4)


def test_spearman_ignores_subjects_outside_calibration():
    got = ec.spearman({"D": 1, "C": 2, "E": 3}, {"D": 6, "C": 5, "E": 4, "K16": 1})
    assert got["n"] == 3 and "K16" not in got["subjects"]


# ---------------------------------------------------------------- calibration source

def test_order_comes_from_calibration_file(tmp_path):
    """Swapping D and C in a copy of the real calibration set changes rho: no built-in order."""
    real = json.loads(ec.CALIBRATION.read_text())
    cal = ec.load_calibration(ec.CALIBRATION)
    scores: dict[str, float] = {"D": 6, "C": 5, "E": 4, "A": 3, "G": 2, "H": 1}
    rho_real = ec.spearman(cal["ranks"], scores)["rho"]

    g = real["cohorts"]["global"]
    g["ranks"]["D"], g["ranks"]["C"] = g["ranks"]["C"], g["ranks"]["D"]
    swapped = tmp_path / "calibration.json"
    swapped.write_text(json.dumps(real))
    rho_swapped = ec.spearman(ec.load_calibration(swapped)["ranks"], scores)["rho"]
    assert rho_real != rho_swapped


def test_real_calibration_global_cohort_excludes_k16():
    cal = ec.load_calibration(ec.CALIBRATION)
    assert "K16" not in cal["ranks"] and "K-variants" in cal["cohorts"]
    assert set(ec.DEFAULT_SUBJECTS) - {"K16"} <= set(cal["ranks"])


def test_calibration_missing_global_is_error(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"cohorts": {"K-variants": {"order": [], "ranks": {}}}}))
    with pytest.raises(ec.EvalError):
        ec.load_calibration(p)


# ---------------------------------------------------------------- subjects, blinding

def test_resolve_fails_closed_without_studio_dir(monkeypatch):
    """Planted negative: no $SHORTS_STUDIO_DIR, or an absolute lookdev_root, is an EvalError."""
    reg = json.loads(ec.STYLES.read_text())
    monkeypatch.delenv("SHORTS_STUDIO_DIR", raising=False)
    with pytest.raises(ec.EvalError, match="SHORTS_STUDIO_DIR is not set"):
        ec.resolve_subject(reg, "D")
    monkeypatch.setenv("SHORTS_STUDIO_DIR", "/nonexistent-studio")
    assert ec.lookdev_root(reg) == Path("/nonexistent-studio") / reg["lookdev_root"]
    with pytest.raises(ec.EvalError, match="must be relative"):
        ec.lookdev_root(dict(reg, lookdev_root="/abs/lookdev"))


@needs_studio
def test_resolve_default_subjects_to_existing_canonical_renders():
    reg = json.loads(ec.STYLES.read_text())
    for s in ec.DEFAULT_SUBJECTS:
        r = ec.resolve_subject(reg, s)
        assert r["video"].is_file() and r["words"].is_file()
    assert ec.resolve_subject(reg, "K16")["video"].parent.name == "r6-S49-K16"
    assert ec.resolve_subject(reg, "D")["video"].name == "lookD.mp4"  # not the review copy
    with pytest.raises(ec.EvalError):
        ec.resolve_subject(reg, "ZZ")


@needs_studio
def test_blinded_entry_drops_identity():
    reg = json.loads(ec.STYLES.read_text())
    entry = ec.resolve_subject(reg, "G")["entry"]
    b = ec.blinded_entry(entry)
    assert set(b) <= {"content_fit", "fonts", "font_license", "publishable"}
    assert "Editorial" not in json.dumps(b) and "Shorts-Feedback" not in json.dumps(b)


# ---------------------------------------------------------------- critic parsing

def test_validate_crit_rejects_bad_shapes():
    assert ec.validate_crit({"score": 5, "mustFix": [], "keep": ["x"]})["score"] == 5.0
    for bad in ({"score": 11, "mustFix": [], "keep": []},
                {"score": True, "mustFix": [], "keep": []},
                {"score": 5, "mustFix": "busy", "keep": []},
                None):
        with pytest.raises(ec.EvalError):
            ec.validate_crit(bad)


def test_parse_stream_result():
    """Canned stream-json lines in the shape observed from a live `claude -p` run."""
    ok = "\n".join([
        json.dumps({"type": "system", "subtype": "init"}),
        json.dumps({"type": "result", "subtype": "success", "is_error": False,
                    "permission_denials": [], "total_cost_usd": 0.1,
                    "structured_output": {"score": 4, "mustFix": ["pacing 0-2 s: slow"],
                                          "keep": []}}),
    ])
    assert ec.parse_stream_result(ok)["score"] == 4.0
    denied = ok.replace('"permission_denials": []',
                        '"permission_denials": [{"tool_name": "Read"}]')
    with pytest.raises(ec.EvalError):
        ec.parse_stream_result(denied)
    with pytest.raises(ec.EvalError):
        ec.parse_stream_result(json.dumps({"type": "system"}))


# ---------------------------------------------------------------- broken render (real ffmpeg)

def test_broken_render_is_black_and_fails_dead_frames(tmp_path):
    video = ec.make_broken(tmp_path / "broken.mp4", duration=3.0)
    p = subprocess.run([sys.executable, str(ROOT / "preflight" / "dead_frames.py"),
                        str(video), "--json"], capture_output=True, text=True, check=False,
                       timeout=300)
    assert p.returncode == 1, p.stderr
    assert not json.loads(p.stdout)["passed"]


# ---------------------------------------------------------------- end to end with a STUB critic

def stub_from_scores(table):
    """STUB critic (not a model call): returns a fixed score per blind label.

    It records every packet so the test can check what a real critic would be sent.
    """
    class StubCritic:
        def __init__(self):
            self.seen = []

        def __call__(self, packet):
            self.seen.append(packet)
            return {"score": table[packet["label"]], "mustFix": [], "keep": []}

    return StubCritic()


@pytest.fixture(scope="module")
def e2e(tmp_path_factory):
    """Real evidence.py + ffmpeg on three real renders; only the critic is stubbed."""
    if not os.environ.get("SHORTS_STUDIO_DIR", "").strip():
        pytest.skip("SHORTS_STUDIO_DIR unset; real renders unavailable")
    out = tmp_path_factory.mktemp("eval")
    return out


def run_stub(out, labels_to_scores, **kw):
    # Plan order is the subject list, then BROKEN: R1=D, R2=G, R3=A, R4=BROKEN.
    critic = stub_from_scores(labels_to_scores)
    res = ec.evaluate(["D", "G", "A"], out, critic, jobs=4, **kw)
    return res, critic


def test_e2e_stub_agreeing_order_passes(e2e):
    res, critic = run_stub(e2e, {"R1": 6, "R2": 4.5, "R3": 4, "R4": 2})
    assert res["spearman"]["rho"] >= 0.6 and res["broken_ok"] and res["passed"]
    assert res["broken"]["failed_checks"]  # the black render fails real preflight gates
    assert (e2e / "eval_result.json").is_file()
    for pk in critic.seen:  # blinded: no style dir or key in what the critic sees
        assert "dir-D" not in pk["text"] and "r2-15s-G" not in pk["text"]
        assert "Render under review: R" in pk["text"] and pk["images"]
        assert all(Path(p).is_file() for p in pk["images"])


def test_e2e_stub_reversed_order_fails(e2e):
    """Planted negative: a critic ranking the dislikes above D must fail the gate."""
    res, _ = run_stub(e2e, {"R1": 3, "R2": 6, "R3": 6, "R4": 2})
    assert res["spearman"]["rho"] < 0 and not res["passed"]


def test_e2e_stub_broken_scored_high_fails(e2e):
    """Planted negative: perfect ranking, but a broken render scored 5 must fail."""
    res, _ = run_stub(e2e, {"R1": 6, "R2": 4.5, "R3": 4, "R4": 5})
    assert res["spearman"]["rho"] >= 0.6 and not res["broken_ok"] and not res["passed"]


def test_cli_usage_error_exit_2(tmp_path):
    p = subprocess.run([sys.executable, str(CLI)], capture_output=True, text=True, check=False,
                       timeout=60)
    assert p.returncode == 2
    p = subprocess.run([sys.executable, str(CLI), "--out", str(tmp_path), "--subjects", "ZZ"],
                       capture_output=True, text=True, check=False, timeout=120)
    assert p.returncode == 2 and "unknown subject" in p.stderr
