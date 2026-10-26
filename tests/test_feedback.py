import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from feedback import feedback  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "feedback" / "feedback.py"
STORE = ROOT / "feedback" / "ratings.jsonl"
STYLES = ROOT / "styles" / "styles.json"
STUDIO_SET = bool(os.environ.get(feedback.STUDIO_ENV, "").strip())
needs_studio = pytest.mark.skipif(not STUDIO_SET,
                                  reason=f"{feedback.STUDIO_ENV} unset; Shorts-Feedback.md unavailable")


def feedback_md() -> Path:
    return feedback.studio_path(json.loads(STYLES.read_text()), "feedback_file")


def run(*args, env=None):
    return subprocess.run([sys.executable, str(CLI), *map(str, args)],
                          capture_output=True, text=True, check=False, timeout=60, env=env)


@pytest.fixture
def store(tmp_path):
    """Fresh seed-only store: a copy of the committed store's seed prefix, which
    test_seed_command_rebuilds_committed_store proves equal to the real `seed` CLI output.
    The live ratings.jsonl is never touched, and real ratings appended to it later cannot
    change these tests."""
    dst = tmp_path / "ratings.jsonl"
    dst.write_text("".join(seed_lines()))
    return dst


def seed_lines():
    """The committed store's seed prefix; later collect rows are appended after it."""
    return STORE.read_text().splitlines(keepends=True)[: len(feedback.SEEDS)]


def ingest(store, tmp_path):
    out = tmp_path / "calibration.json"
    r = run("ingest", "--store", store, "--out", out)
    return r, (json.loads(out.read_text()) if out.exists() else None)


def ingest_ok(store, tmp_path) -> "tuple[subprocess.CompletedProcess[str], dict]":
    """ingest() for success-path tests: fails loudly if no calibration was written."""
    r, cal = ingest(store, tmp_path)
    assert r.returncode == 0, r.stderr
    assert cal is not None, "ingest exited 0 but wrote no calibration file"
    return r, cal


def test_seeded_store_reproduces_d_c_e(store, tmp_path):
    r, cal = ingest_ok(store, tmp_path)
    assert r.returncode == 0, r.stderr
    order = cal["cohorts"]["global"]["order"]
    assert order[:3] == ["D", "C", "E"]
    # named dislikes sit below every polish-only and leader style
    assert set(order[-3:]) == {"A", "G", "H"}
    # K16 is ranked only within its own cohort, never against D/C/E
    assert "K16" not in order and cal["cohorts"]["K-variants"]["order"] == ["K16"]
    assert cal["cohorts"]["global"]["ranks"]["D"] == 1.0


@needs_studio
def test_seed_rows_quote_their_cited_line():
    md = feedback_md()
    lines = md.read_text().splitlines()
    rows = [json.loads(x) for x in seed_lines()]
    assert len(rows) == len(feedback.SEEDS)
    for row in rows:
        name, n = row["source"].split(":")
        assert name == md.name
        assert lines[int(n) - 1] == "> " + row["text"]
        assert row["rater"] == "owner" and row["score_origin"] == "mapped"


def test_seed_fails_closed_without_studio_dir(tmp_path):
    """Planted negative: with $SHORTS_STUDIO_DIR unset, seed exits 2 and writes nothing."""
    env = {k: v for k, v in os.environ.items() if k != feedback.STUDIO_ENV}
    fresh = tmp_path / "fresh.jsonl"
    r = run("seed", "--store", fresh, env=env)
    assert r.returncode == 2 and "SHORTS_STUDIO_DIR is not set" in r.stderr
    assert not fresh.exists()


def test_studio_path_rejects_absolute_and_resolves_relative(monkeypatch):
    monkeypatch.setenv(feedback.STUDIO_ENV, "/studio")
    assert feedback.studio_path({"f": "_kit/x.md"}, "f") == Path("/studio/_kit/x.md")
    with pytest.raises(feedback.FeedbackError, match="must be relative"):
        feedback.studio_path({"f": "/abs/x.md"}, "f")
    with pytest.raises(feedback.FeedbackError, match="has no f"):
        feedback.studio_path({}, "f")


@needs_studio
def test_seed_command_rebuilds_committed_store(tmp_path):
    fresh = tmp_path / "fresh.jsonl"
    r = run("seed", "--store", fresh)
    assert r.returncode == 0, r.stderr
    assert fresh.read_text() == "".join(seed_lines())
    again = run("seed", "--store", fresh)
    assert again.returncode == 2 and "not empty" in again.stderr


def test_planted_contradictory_rating_changes_order(store, tmp_path):
    before = len(store.read_text().splitlines())
    r = run("collect", "--store", store, "--style", "E", "--rater", "owner", "--score", "9",
            "--video", "share/round2_S61_E-ui-kinetic-type.mp4", "--liked", "clean",
            "--text", "planted contradiction", "--ts", "2026-10-23T10:00:00+00:00")
    assert r.returncode == 0, r.stderr
    assert len(store.read_text().splitlines()) == before + 1  # appended, not rewritten
    r, cal = ingest_ok(store, tmp_path)
    assert r.returncode == 0, r.stderr
    g = cal["cohorts"]["global"]
    assert g["order"][:3] == ["E", "D", "C"]
    # the later rating superseded the seed for the same rater and video
    assert g["subjects"]["E"]["n"] == 1 and g["subjects"]["E"]["mean_score"] == 9.0
    assert cal["rows_read"] == before + 1 and cal["ratings_used"] == before


def test_second_rater_is_averaged_not_superseded(store, tmp_path):
    r = run("collect", "--store", store, "--style", "C", "--rater", "guest", "--score", "10",
            "--video", "share/round1_S61_C-product-ui.mp4", "--ts", "2026-10-23T10:00:00+00:00")
    assert r.returncode == 0, r.stderr
    _, cal = ingest_ok(store, tmp_path)
    c = cal["cohorts"]["global"]["subjects"]["C"]
    assert c["n"] == 2 and c["mean_score"] == pytest.approx((5.5 + 10) / 2)
    assert cal["cohorts"]["global"]["order"][0] == "C"


def good_row():
    return json.loads(seed_lines()[0])


@pytest.mark.parametrize("mutate,needle", [
    (lambda r: r.update(score=11), "outside"),
    (lambda r: r.update(score=0.5), "outside"),
    (lambda r: r.update(score="6"), "score: wrong type"),
    (lambda r: r.update(score=True), "score: wrong type"),
    (lambda r: r.update(style_key="Z"), "not in styles registry"),
    (lambda r: r.update(variant="K99", style_key="K"), "not a variant"),
    (lambda r: r.pop("ts"), "missing field"),
    (lambda r: r.update(ts="2026-10-22"), "timezone"),
    (lambda r: r.update(ts="yesterday"), "not ISO-8601"),
    (lambda r: r.update(liked="clean"), "liked: wrong type"),
    (lambda r: r.update(disliked=[""]), "non-empty strings"),
    (lambda r: r.update(rater=" "), "rater: empty"),
    (lambda r: r.update(extra=1), "unknown field"),
    (lambda r: r.update(score_origin="guess"), "score_origin"),
])
def test_malformed_row_refuses_ingest(store, tmp_path, mutate, needle):
    row = good_row()
    mutate(row)
    with store.open("a") as fh:
        fh.write(json.dumps(row) + "\n")
    n = len(store.read_text().splitlines())
    r, cal = ingest(store, tmp_path)
    assert r.returncode == 2 and cal is None
    assert f"ratings.jsonl:{n}:" in r.stderr and needle in r.stderr


def test_non_json_line_refuses_ingest(store, tmp_path):
    with store.open("a") as fh:
        fh.write("{not json\n")
    r, cal = ingest(store, tmp_path)
    assert r.returncode == 2 and cal is None and "not valid JSON" in r.stderr


def test_collect_rejects_bad_score_without_appending(store):
    before = store.read_text()
    r = run("collect", "--store", store, "--style", "D", "--rater", "owner", "--score", "11",
            "--video", "share/round1_S61_D-reel-native.mp4")
    assert r.returncode == 2 and "outside" in r.stderr
    assert store.read_text() == before


def test_collect_missing_args_is_usage_error(store):
    r = run("collect", "--store", store, "--style", "D")
    assert r.returncode == 2 and "--video" in r.stderr and "--score" in r.stderr


def test_collect_refuses_when_store_already_corrupt(store):
    with store.open("a") as fh:
        fh.write("garbage\n")
    before = store.read_text()
    r = run("collect", "--store", store, "--style", "D", "--rater", "x", "--score", "5",
            "--video", "v.mp4")
    assert r.returncode == 2 and store.read_text() == before


def test_export_json_and_csv(store):
    j = run("export", "--store", store)
    assert j.returncode == 0
    rows = json.loads(j.stdout)
    assert [r["style_key"] for r in rows] == [s["style_key"] for s in feedback.SEEDS]
    c = run("export", "--store", store, "--format", "csv")
    assert c.returncode == 0
    lines = c.stdout.strip().splitlines()
    assert lines[0].split(",")[:2] == ["style_key", "variant"] and len(lines) == 1 + len(rows)


def test_interactive_collect(store, monkeypatch, capsys):
    # Unit isolation: stdin answers are scripted via monkeypatched input(); the append path is real.
    answers = iter(["7", "E", "", "", "share/round2_S61_E-ui-kinetic-type.mp4", "owner",
                    "clean, legible", "", "fine now"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    assert feedback.main(["collect", "--interactive", "--store", str(store)]) == 0
    last = json.loads(store.read_text().splitlines()[-1])
    assert last["style_key"] == "E" and last["score"] == 7.0 and last["cohort"] == "global"
    assert last["variant"] is None and last["liked"] == ["clean", "legible"]
    assert last["score_origin"] == "rater" and last["source"] is None


SHIPPED = (STORE, ROOT / "feedback" / "calibration.json")


def shipped_bytes():
    return [p.read_bytes() for p in SHIPPED]


def test_ingest_writes_user_store_and_leaves_seeds_untouched(tmp_path, monkeypatch):
    """With $CLAUDE_PLUGIN_DATA set and no paths, ingest seeds and writes the user store only."""
    monkeypatch.setenv(feedback.DATA_ENV, str(tmp_path))
    before = shipped_bytes()
    r = run("ingest")
    assert r.returncode == 0, r.stderr
    user = tmp_path / "feedback"
    assert (user / "ratings.jsonl").read_bytes() == before[0]  # first write copied the seed
    cal = json.loads((user / "calibration.json").read_text())
    assert cal["cohorts"]["global"]["order"][:3] == ["D", "C", "E"]
    assert json.loads(r.stdout)["out"] == str(user / "calibration.json")
    # a later collect appends to the user store, not to the shipped seed
    c = run("collect", "--style", "E", "--rater", "owner", "--score", "9",
            "--video", "share/round2_S61_E-ui-kinetic-type.mp4", "--ts", "2026-10-23T10:00:00+00:00")
    assert c.returncode == 0, c.stderr
    assert len((user / "ratings.jsonl").read_text().splitlines()) == len(before[0].splitlines()) + 1
    assert feedback.read_store() == user / "ratings.jsonl"
    assert shipped_bytes() == before


def test_collect_seeds_user_store_on_first_write(tmp_path, monkeypatch):
    monkeypatch.setenv(feedback.DATA_ENV, str(tmp_path))
    assert feedback.read_store() == STORE  # no user store yet: readers get the seed
    r = run("collect", "--style", "D", "--rater", "guest", "--score", "8",
            "--video", "share/round1_S61_D-reel-native.mp4")
    assert r.returncode == 0, r.stderr
    user = tmp_path / "feedback"
    lines = (user / "ratings.jsonl").read_text().splitlines(keepends=True)
    assert lines[:-1] == STORE.read_text().splitlines(keepends=True)
    assert json.loads(lines[-1])["rater"] == "guest"
    assert (user / "calibration.json").read_bytes() == SHIPPED[1].read_bytes()


def test_writes_fail_closed_without_plugin_data(tmp_path, monkeypatch):
    """Planted negative: $CLAUDE_PLUGIN_DATA unset and no explicit path -> exit 2, nothing written."""
    monkeypatch.delenv(feedback.DATA_ENV, raising=False)
    before = shipped_bytes()
    r = run("ingest")
    assert r.returncode == 2 and "CLAUDE_PLUGIN_DATA is not set" in r.stderr and "--out" in r.stderr
    # an explicit --store alone does not license writing the calibration into the plugin root
    store = tmp_path / "ratings.jsonl"
    store.write_text("".join(seed_lines()))
    r = run("ingest", "--store", store)
    assert r.returncode == 2 and "CLAUDE_PLUGIN_DATA is not set" in r.stderr
    r = run("collect", "--style", "D", "--rater", "x", "--score", "5", "--video", "v.mp4")
    assert r.returncode == 2 and "--store" in r.stderr
    assert shipped_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["ratings.jsonl"]


def test_explicit_paths_inside_plugin_root_are_refused(tmp_path):
    before = shipped_bytes()
    r = run("collect", "--store", STORE, "--style", "D", "--rater", "x", "--score", "5",
            "--video", "v.mp4")
    assert r.returncode == 2 and "inside the plugin root" in r.stderr
    r = run("ingest", "--store", STORE, "--out", SHIPPED[1])
    assert r.returncode == 2 and "inside the plugin root" in r.stderr
    assert shipped_bytes() == before


def test_ties_share_averaged_ranks():
    means = {"a": 5.0, "b": 4.0, "c": 4.0, "d": 3.0}
    assert feedback._average_ranks(["a", "b", "c", "d"], means) == {"a": 1, "b": 2.5, "c": 2.5, "d": 4}
