"""Contract test: the add-on's words.json is valid input for the core plugin's scripts/pick_cut.py.

The core repo is found through VOS_CORE_DIR (its root; pick_cut.py is loaded from
$VOS_CORE_DIR/scripts/pick_cut.py, never copied). When VOS_CORE_DIR is unset the tests skip
locally but FAIL under CI (env CI set to anything except "", "0", "false", "no"), so a CI run can
never go green without checking the contract. VOS_CORE_DIR set to a directory without
scripts/pick_cut.py always fails.

Oracle: the core's recorded pick_cut run on the r2-15s narration (core
evals/02-look-cut-from-words/prompt.md): `--target 15 --lo 12.5 --hi 17.5` on a 14.3 s WAV chose
"terms." (w45) at cut 14.233333 in gap 13.843917-14.3 and rejected "agents." and "off.". The
sentence-end gaps below are the gapStart/gapEnd values that run printed.
"""
from __future__ import annotations

import functools
import importlib.util
import json
import math
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest

ROOT = Path(__file__).resolve().parents[1]
Words = list[dict[str, Any]]
CORE_ENV = "VOS_CORE_DIR"
CLI = ROOT / "scripts" / "words.py"
ALIGNMENT_FIXTURE = ROOT / "tests" / "fixtures" / "r2-15s.alignment.json"
WAV_DURATION = 14.3  # the r2-15s narration WAV (core eval 02 prompt: "exactly 14.3 s long")
TARGET, LO, HI = 15.0, 12.5, 17.5
# Sentence-end silence gaps from the recorded core run (gapStart, gapEnd per evaluated row).
RECORDED_GAPS = ((4.697625, 5.188083), (12.056729, 12.2775), (13.843917, 14.3))
RECORDED_EVALUATED = [
    ("w17", "agents.", 5.133333, "cut 5.133 outside 12.5-17.5 s"),
    ("w39", "off.", 12.2, "cut 12.200 outside 12.5-17.5 s"),
    ("w45", "terms.", 14.233333, "in window"),
]
RECORDED_TRANSCRIPT = (
    "In this short, I want to walk through a failure mode that worries me more than sloppy agents. "
    "It's the rigorous agent run that does exactly what the plan says, passes every check, and still "
    "leaves the project worse off. Here's the setup in plain terms."
)


def _under_ci() -> bool:
    return os.environ.get("CI", "").strip().lower() not in ("", "0", "false", "no")


@functools.lru_cache(maxsize=None)
def _load(path: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location("vos_core_pick_cut", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def core_pick_cut() -> ModuleType:
    """The core's pick_cut module; skip (local) or fail (CI) when VOS_CORE_DIR is unset."""
    value = os.environ.get(CORE_ENV, "")
    if not value:
        if _under_ci():
            pytest.fail(f"{CORE_ENV} is not set under CI: the pick_cut contract test must run in CI; "
                        f"set it to the voiceover-shorts core checkout")
        pytest.skip(f"{CORE_ENV} not set: point it at the voiceover-shorts core checkout")
    path = Path(value) / "scripts" / "pick_cut.py"
    if not path.is_file():
        pytest.fail(f"{CORE_ENV}={value} has no scripts/pick_cut.py")
    return _load(str(path.resolve()))


def gaps(pc: ModuleType) -> list[Any]:
    return [pc.Gap(s, e) for s, e in RECORDED_GAPS]


def assert_pick_cut_contract(ws: object, pc: ModuleType, wav_duration: float = WAV_DURATION) -> None:
    """Raise AssertionError unless `ws` is a words list pick_cut.py can consume as words.json."""
    assert isinstance(ws, list) and ws, "words.json must be a non-empty JSON array"
    assert json.loads(json.dumps(ws)) == ws, "words.json must survive a JSON round trip"
    prev_start = -math.inf
    for i, w in enumerate(ws):
        assert isinstance(w, dict), f"word {i} is not an object: {w!r}"
        assert set(w) == {"id", "text", "start", "end"}, f"word {i} keys {sorted(w)}"
        assert w["id"] == f"w{i}", f"word {i} id {w['id']!r}"
        assert isinstance(w["text"], str) and w["text"] and w["text"].split() == [w["text"]], \
            f"word {i} text must be one non-empty token: {w['text']!r}"
        for key in ("start", "end"):
            v = w[key]
            assert isinstance(v, float) and not isinstance(v, bool) and math.isfinite(v), \
                f"word {i} {key} must be a finite float: {v!r}"
        assert 0.0 <= w["start"] <= w["end"] <= wav_duration, f"word {i} times out of order/range: {w}"
        assert w["start"] >= prev_start, f"word {i} starts before word {i - 1}"
        prev_start = w["start"]
    assert any(pc.is_sentence_end(w["text"]) for w in ws), "no word ends a sentence for pick_cut"


@pytest.fixture
def addon_words(tmp_path: Path) -> Words:
    """The add-on's words.json, written by the real words.py CLI and read back as pick_cut reads it."""
    out = tmp_path / "r2-15s.words.json"
    proc = subprocess.run([sys.executable, str(CLI), "--from-alignment", str(ALIGNMENT_FIXTURE),
                           "--wav-duration", str(WAV_DURATION), "--out", str(out)],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    with open(out, encoding="utf-8") as f:
        return cast(Words, json.load(f))


# --- the contract ---------------------------------------------------------------------------------

def test_addon_words_meet_pick_cut_contract(addon_words: Words) -> None:
    assert_pick_cut_contract(addon_words, core_pick_cut())


def test_core_sentence_end_rule_finds_the_recorded_sentence_ends(addon_words: Words) -> None:
    pc = core_pick_cut()
    ends = [(w["id"], w["text"]) for w in addon_words if pc.is_sentence_end(w["text"])]
    assert ends == [(i, t) for i, t, _, _ in RECORDED_EVALUATED]


def test_pick_on_addon_words_reproduces_recorded_core_run(addon_words: Words) -> None:
    pc = core_pick_cut()
    res = pc.pick(gaps(pc), addon_words, TARGET, LO, HI)
    assert res["status"] == "OK"
    assert res["cut"] == 14.233333
    assert (res["sentenceEndIndex"], res["sentenceEndWordId"], res["sentenceEndText"]) == (45, "w45", "terms.")
    assert (res["gapStart"], res["gapEnd"]) == (13.843917, 14.3)
    got = [(r["id"], r["text"], r["cut"], r["status"]) for r in res["evaluated"]]
    assert got == RECORDED_EVALUATED


def test_pick_on_addon_words_matches_pick_on_core_fixture(addon_words: Words) -> None:
    pc = core_pick_cut()
    core_fixture = Path(os.environ[CORE_ENV]) / "tests" / "fixtures" / "r2-15s.words.json"
    with open(core_fixture, encoding="utf-8") as f:
        core_words = json.load(f)
    a = pc.pick(gaps(pc), addon_words, TARGET, LO, HI)
    b = pc.pick(gaps(pc), core_words, TARGET, LO, HI)
    strip = ("wordStart", "wordEnd")
    assert [{k: v for k, v in r.items() if k not in strip} for r in a["evaluated"]] == \
        [{k: v for k, v in r.items() if k not in strip} for r in b["evaluated"]]
    for ra, rb in zip(a["evaluated"], b["evaluated"]):
        assert abs(ra["wordStart"] - rb["wordStart"]) <= 1e-3 and abs(ra["wordEnd"] - rb["wordEnd"]) <= 1e-3
    assert {k: a[k] for k in a if k != "evaluated"} == {k: b[k] for k in b if k != "evaluated"}


def test_write_outputs_accepts_addon_words(addon_words: Words, tmp_path: Path) -> None:
    pc = core_pick_cut()
    res = pc.pick(gaps(pc), addon_words, TARGET, LO, HI)
    out_words, out_txt = tmp_path / "cut.words.json", tmp_path / "cut.txt"
    info = pc.write_outputs("unused.wav", addon_words, res, None, str(out_words), str(out_txt))
    with open(out_words, encoding="utf-8") as f:
        kept = json.load(f)
    assert [w["id"] for w in kept] == [f"w{i}" for i in range(46)]
    assert kept[-1]["text"] == "terms." and kept[-1]["end"] == 14.233333
    assert all(w["end"] <= res["cut"] for w in kept)
    assert info["transcript"] == RECORDED_TRANSCRIPT
    assert out_txt.read_text(encoding="utf-8") == RECORDED_TRANSCRIPT + "\n"


# --- planted contract breaches: the checker and pick_cut both reject them --------------------------

def _breach(ws: Words, kind: str) -> Words:
    ws = [dict(w) for w in ws]
    if kind == "missing_end":
        del ws[45]["end"]
    elif kind == "string_time":
        ws[3]["start"] = str(ws[3]["start"])
    elif kind == "starts_decrease":
        ws[10]["start"] = ws[9]["start"] - 0.5
    elif kind == "end_past_wav":
        ws[-1]["end"] = WAV_DURATION + 1.0
    elif kind == "bad_id":
        ws[2]["id"] = "word2"
    elif kind == "split_punctuation":
        ws = [dict(w, text=w["text"].rstrip(".!?")) for w in ws]
    return ws


@pytest.mark.parametrize("kind", ["missing_end", "string_time", "starts_decrease", "end_past_wav",
                                  "bad_id", "split_punctuation"])
def test_contract_checker_rejects_breach(addon_words: Words, kind: str) -> None:
    pc = core_pick_cut()
    with pytest.raises(AssertionError):
        assert_pick_cut_contract(_breach(addon_words, kind), pc)


def test_pick_cut_breaks_on_missing_end(addon_words: Words) -> None:
    pc = core_pick_cut()
    with pytest.raises(KeyError):
        pc.pick(gaps(pc), _breach(addon_words, "missing_end"), TARGET, LO, HI)


def test_pick_cut_finds_no_cut_when_punctuation_is_detached(addon_words: Words) -> None:
    pc = core_pick_cut()
    with pytest.raises(pc.CutFail):
        pc.pick(gaps(pc), _breach(addon_words, "split_punctuation"), TARGET, LO, HI)


# --- the VOS_CORE_DIR gate itself -----------------------------------------------------------------

def test_gate_fails_under_ci_when_core_dir_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(CORE_ENV, raising=False)
    monkeypatch.setenv("CI", "true")
    with pytest.raises(pytest.fail.Exception):
        core_pick_cut()


@pytest.mark.parametrize("ci", [None, "", "0", "false"])
def test_gate_skips_outside_ci_when_core_dir_unset(monkeypatch: pytest.MonkeyPatch, ci: str | None) -> None:
    monkeypatch.delenv(CORE_ENV, raising=False)
    if ci is None:
        monkeypatch.delenv("CI", raising=False)
    else:
        monkeypatch.setenv("CI", ci)
    with pytest.raises(pytest.skip.Exception):
        core_pick_cut()


def test_gate_fails_when_core_dir_has_no_pick_cut(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(CORE_ENV, str(tmp_path))
    monkeypatch.delenv("CI", raising=False)
    with pytest.raises(pytest.fail.Exception):
        core_pick_cut()
