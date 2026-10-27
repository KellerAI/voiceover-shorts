"""Tests for scripts/words.py: ElevenLabs /with-timestamps `alignment` -> core words.json."""
from __future__ import annotations

import copy
import json
import math
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import words  # noqa: E402

CLI = ROOT / "scripts" / "words.py"
FIXTURES = ROOT / "tests" / "fixtures"
WORDS_FIXTURE = FIXTURES / "r2-15s.words.json"
ALIGNMENT_FIXTURE = FIXTURES / "r2-15s.alignment.json"
WAV_DURATION = 14.5  # just past the fixture's last word end (14.28669)

# Verbatim copy of the core's sentence-end rule (voiceover-shorts scripts/pick_cut.py:36, :99-100).
SENTENCE_END = re.compile(r"[.!?][\"')\]”’]*$")


def is_sentence_end(text: str) -> bool:
    return bool(SENTENCE_END.search(text.strip()))


def load_json(path: Path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def spread(tokens: list[tuple[str, float, float]]) -> dict:
    """Synthetic alignment: each token's characters spread evenly over start..end, one space per gap."""
    chars: list[str] = []
    starts: list[float] = []
    ends: list[float] = []
    for i, (text, s, e) in enumerate(tokens):
        if i:
            chars.append(" ")
            starts.append(tokens[i - 1][2])
            ends.append(s)
        k = len(text)
        for j, c in enumerate(text):
            chars.append(c)
            starts.append(s + (e - s) * j / k)
            ends.append(e if j == k - 1 else s + (e - s) * (j + 1) / k)
    return {"characters": chars, "character_start_times_seconds": starts,
            "character_end_times_seconds": ends}


def run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(CLI), *args], capture_output=True, text=True)


@pytest.fixture
def response() -> dict:
    return load_json(ALIGNMENT_FIXTURE)


@pytest.fixture
def expected() -> list[dict]:
    return load_json(WORDS_FIXTURE)


# --- round trip against the core fixture -----------------------------------------------------------

def test_round_trip_reproduces_core_fixture(response, expected):
    got = words.alignment_to_words(response["alignment"], WAV_DURATION)
    assert [w["id"] for w in got] == [w["id"] for w in expected]
    assert [w["text"] for w in got] == [w["text"] for w in expected]
    for g, e in zip(got, expected):
        assert set(g) == {"id", "text", "start", "end"}
        assert isinstance(g["start"], float) and isinstance(g["end"], float)
        assert abs(g["start"] - e["start"]) <= 1e-3, (g, e)
        assert abs(g["end"] - e["end"]) <= 1e-3, (g, e)
        assert g["start"] == round(g["start"], 6) and g["end"] == round(g["end"], 6)


def test_round_trip_keeps_core_sentence_ends(response, expected):
    got = words.alignment_to_words(response["alignment"], WAV_DURATION)
    want = [w["text"] for w in expected if is_sentence_end(w["text"])]
    assert want == ["agents.", "off.", "terms."]
    assert [w["text"] for w in got if is_sentence_end(w["text"])] == want


def test_fixture_normalized_alignment_differs(response):
    # Guards the planted negative below: the fixture's normalized_alignment must really differ.
    assert response["normalized_alignment"] != response["alignment"]


# --- punctuation, tags, merging ------------------------------------------------------------------

def test_punctuation_stays_attached():
    al = spread([("plain", 0.0, 0.4), ("terms.", 0.5, 1.0), ("Next,", 1.2, 1.5), ("word", 1.6, 2.0)])
    got = words.alignment_to_words(al, 5.0)
    assert [w["text"] for w in got] == ["plain", "terms.", "Next,", "word"]
    assert is_sentence_end(got[1]["text"])
    assert not is_sentence_end(got[2]["text"])


def test_tags_are_dropped():
    al = spread([("[laughs]", 0.0, 0.3), ("Hello", 0.4, 0.8), ("[sighs]", 0.9, 1.1),
                 ("world.", 1.2, 1.6), ("[]", 1.7, 1.8)])
    got = words.alignment_to_words(al, 5.0)
    assert [(w["id"], w["text"]) for w in got] == [("w0", "Hello"), ("w1", "world.")]
    assert got[0]["start"] == pytest.approx(0.4) and got[1]["start"] == pytest.approx(1.2)


def test_bracketed_text_that_is_not_a_whole_token_is_kept():
    al = spread([("[laughs].", 0.0, 0.3), ("ok", 0.4, 0.6)])
    got = words.alignment_to_words(al, 5.0)
    assert [w["text"] for w in got] == ["[laughs].", "ok"]


def _assert_no_tag_residue(got):
    for w in got:
        for bad in ("[", "]", "clears", "throat"):
            assert bad not in w["text"]


def test_multi_word_tag_is_dropped_before_tokenizing():
    al = spread([("Hello", 0.0, 0.5), ("[clears", 0.6, 0.9), ("throat]", 1.0, 1.3), ("world", 1.4, 1.9)])
    got = words.alignment_to_words(al, 5.0)
    assert [w["text"] for w in got] == ["Hello", "world"]
    _assert_no_tag_residue(got)
    assert got[0]["start"] == pytest.approx(0.0) and got[0]["end"] == pytest.approx(0.5)
    assert got[1]["start"] == pytest.approx(1.4) and got[1]["end"] == pytest.approx(1.9)


def test_single_word_tag_mid_text_is_dropped():
    al = spread([("Hello", 0.0, 0.5), ("[laughs]", 0.6, 0.9), ("world", 1.0, 1.5)])
    got = words.alignment_to_words(al, 5.0)
    assert [w["text"] for w in got] == ["Hello", "world"]
    assert got[1]["start"] == pytest.approx(1.0)


def test_multi_word_tag_at_start_is_dropped():
    al = spread([("[clears", 0.0, 0.3), ("throat]", 0.4, 0.7), ("Hello", 0.8, 1.2), ("world", 1.3, 1.7)])
    got = words.alignment_to_words(al, 5.0)
    assert [(w["id"], w["text"]) for w in got] == [("w0", "Hello"), ("w1", "world")]
    _assert_no_tag_residue(got)
    assert got[0]["start"] == pytest.approx(0.8)


def test_multi_word_tag_at_end_is_dropped():
    al = spread([("Hello", 0.0, 0.5), ("world.", 0.6, 1.0), ("[clears", 1.1, 1.4), ("throat]", 1.5, 1.8)])
    got = words.alignment_to_words(al, 5.0)
    assert [w["text"] for w in got] == ["Hello", "world."]
    _assert_no_tag_residue(got)
    assert got[1]["end"] == pytest.approx(1.0)


def test_lone_punctuation_merges_into_previous_word():
    al = spread([("Hello", 0.0, 0.5), (",", 0.55, 0.6), ("world", 0.7, 1.0), ("!", 1.1, 1.3)])
    got = words.alignment_to_words(al, 5.0)
    assert [(w["id"], w["text"]) for w in got] == [("w0", "Hello,"), ("w1", "world!")]
    assert got[0]["start"] == pytest.approx(0.0) and got[0]["end"] == pytest.approx(0.6)
    assert got[1]["end"] == pytest.approx(1.3)
    assert is_sentence_end(got[1]["text"])


def test_lone_punctuation_after_dropped_tag_merges_into_word_before_tag():
    al = spread([("Done", 0.0, 0.4), ("[pause]", 0.5, 0.9), (".", 1.0, 1.1)])
    got = words.alignment_to_words(al, 5.0)
    assert [w["text"] for w in got] == ["Done."]
    assert got[0]["end"] == pytest.approx(1.1)


def test_leading_lone_punctuation_is_prepended_to_next_word():
    al = spread([("“", 0.0, 0.05), ("(", 0.06, 0.1), ("Hi", 0.2, 0.5), ("there.", 0.6, 1.0)])
    got = words.alignment_to_words(al, 5.0)
    assert [(w["id"], w["text"]) for w in got] == [("w0", "“(Hi"), ("w1", "there.")]
    assert got[0]["start"] == pytest.approx(0.0) and got[0]["end"] == pytest.approx(0.5)


def test_runs_of_whitespace_make_no_empty_words():
    al = {"characters": [" ", "a", " ", "\n", "\t", "b", " "],
          "character_start_times_seconds": [0.0, 0.1, 0.2, 0.2, 0.2, 0.3, 0.4],
          "character_end_times_seconds": [0.1, 0.2, 0.2, 0.2, 0.3, 0.4, 0.5]}
    got = words.alignment_to_words(al, 5.0)
    assert [w["text"] for w in got] == ["a", "b"]


def test_no_words_at_all_raises():
    with pytest.raises(ValueError):
        words.alignment_to_words(spread([("[laughs]", 0.0, 0.3), ("...", 0.4, 0.5)]), 5.0)


# --- monotonic times and clamping ----------------------------------------------------------------

def test_clamps_to_wav_duration():
    al = spread([("one", 0.0, 0.5), ("two", 0.6, 1.4), ("three.", 1.5, 2.0)])
    got = words.alignment_to_words(al, 1.0)
    assert got[0]["end"] == pytest.approx(0.5)
    assert got[1]["start"] == pytest.approx(0.6) and got[1]["end"] == 1.0
    assert got[2]["start"] == 1.0 and got[2]["end"] == 1.0
    assert all(w["end"] <= 1.0 for w in got)


def test_fixture_clamped_at_wav_duration(response):
    got = words.alignment_to_words(response["alignment"], 14.0)
    assert got[-1]["text"] == "terms."
    assert got[-1]["start"] == pytest.approx(13.40669) and got[-1]["end"] == 14.0


def test_starts_made_monotonic_within_tolerance():
    al = {"characters": ["A", " ", "B"],
          "character_start_times_seconds": [1.0, 1.0, 1.0 - 5e-7],
          "character_end_times_seconds": [1.1, 1.1, 1.2]}
    got = words.alignment_to_words(al, 5.0)
    assert got[1]["start"] == 1.0
    assert all(w["end"] >= w["start"] for w in got)


# --- validation (planted negatives) --------------------------------------------------------------

def test_mismatched_lengths_raise(response):
    al = copy.deepcopy(response["alignment"])
    al["character_end_times_seconds"].pop()
    with pytest.raises(ValueError):
        words.alignment_to_words(al, WAV_DURATION)


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
@pytest.mark.parametrize("key", ["character_start_times_seconds", "character_end_times_seconds"])
def test_non_finite_time_raises(response, key, bad):
    al = copy.deepcopy(response["alignment"])
    al[key][5] = bad
    with pytest.raises(ValueError):
        words.alignment_to_words(al, WAV_DURATION)


def test_empty_alignment_raises():
    with pytest.raises(ValueError):
        words.alignment_to_words({"characters": [], "character_start_times_seconds": [],
                                  "character_end_times_seconds": []}, 5.0)


def test_end_before_start_raises():
    with pytest.raises(ValueError):
        words.alignment_to_words({"characters": ["a"], "character_start_times_seconds": [0.5],
                                  "character_end_times_seconds": [0.4]}, 5.0)


def test_decreasing_start_beyond_tolerance_raises():
    with pytest.raises(ValueError):
        words.alignment_to_words({"characters": ["a", "b"], "character_start_times_seconds": [0.5, 0.49],
                                  "character_end_times_seconds": [0.6, 0.6]}, 5.0)


def test_missing_key_raises(response):
    al = copy.deepcopy(response["alignment"])
    del al["character_start_times_seconds"]
    with pytest.raises(ValueError):
        words.alignment_to_words(al, WAV_DURATION)


@pytest.mark.parametrize("dur", [0.0, -1.0, math.nan, math.inf])
def test_bad_wav_duration_raises(response, dur):
    with pytest.raises(ValueError):
        words.alignment_to_words(response["alignment"], dur)


# --- covers_text ---------------------------------------------------------------------------------

def test_covers_text_true_for_full_text(response):
    assert words.covers_text(response["alignment"], response["text"]) is True


def test_covers_text_false_for_truncated_alignment(response):
    al = copy.deepcopy(response["alignment"])
    for key in words.ALIGNMENT_KEYS:
        del al[key][-6:]  # drop the final "terms." -> characters end "...in plain "
    assert words.covers_text(al, response["text"]) is False


def test_covers_text_false_for_different_text(response):
    assert words.covers_text(response["alignment"], response["text"] + " Extra.") is False
    assert words.covers_text(response["alignment"], response["text"].replace("plain", "plan")) is False


# --- CLI -----------------------------------------------------------------------------------------

def test_cli_writes_words_json(tmp_path, response):
    out = tmp_path / "out.words.json"
    proc = run_cli("--from-alignment", str(ALIGNMENT_FIXTURE), "--wav-duration", str(WAV_DURATION),
                   "--out", str(out))
    assert proc.returncode == 0, proc.stderr
    assert load_json(out) == words.alignment_to_words(response["alignment"], WAV_DURATION)


def test_cli_refuses_to_overwrite(tmp_path):
    out = tmp_path / "out.words.json"
    out.write_text("SENTINEL", encoding="utf-8")
    proc = run_cli("--from-alignment", str(ALIGNMENT_FIXTURE), "--wav-duration", str(WAV_DURATION),
                   "--out", str(out))
    assert proc.returncode == 1
    assert proc.stderr.strip()
    assert out.read_text(encoding="utf-8") == "SENTINEL"


def test_cli_second_run_refuses_and_keeps_first_output(tmp_path):
    out = tmp_path / "out.words.json"
    args = ("--from-alignment", str(ALIGNMENT_FIXTURE), "--wav-duration", str(WAV_DURATION), "--out", str(out))
    assert run_cli(*args).returncode == 0
    first = out.read_bytes()
    assert run_cli(*args).returncode == 1
    assert out.read_bytes() == first


def test_cli_ignores_normalized_alignment(tmp_path, response):
    changed = copy.deepcopy(response)
    norm = changed["normalized_alignment"]
    norm["characters"] = list("twenty one " * 3)[: len(norm["characters"])]
    norm["character_start_times_seconds"] = [0.0] * len(norm["characters"])
    norm["character_end_times_seconds"] = [99.0] * len(norm["characters"])
    src = tmp_path / "changed.alignment.json"
    src.write_text(json.dumps(changed), encoding="utf-8")
    a, b = tmp_path / "a.words.json", tmp_path / "b.words.json"
    assert run_cli("--from-alignment", str(ALIGNMENT_FIXTURE), "--wav-duration", "14.5", "--out", str(a)).returncode == 0
    assert run_cli("--from-alignment", str(src), "--wav-duration", "14.5", "--out", str(b)).returncode == 0
    assert a.read_bytes() == b.read_bytes()


def test_cli_never_falls_back_to_normalized_alignment(tmp_path, response):
    src = tmp_path / "norm-only.alignment.json"
    src.write_text(json.dumps({"normalized_alignment": response["normalized_alignment"]}), encoding="utf-8")
    out = tmp_path / "out.words.json"
    proc = run_cli("--from-alignment", str(src), "--wav-duration", "14.5", "--out", str(out))
    assert proc.returncode == 1
    assert not out.exists()


def test_cli_invalid_alignment_exits_1_and_writes_nothing(tmp_path, response):
    bad = copy.deepcopy(response)
    bad["alignment"]["character_start_times_seconds"].pop()
    src = tmp_path / "bad.alignment.json"
    src.write_text(json.dumps(bad), encoding="utf-8")
    out = tmp_path / "out.words.json"
    proc = run_cli("--from-alignment", str(src), "--wav-duration", "14.5", "--out", str(out))
    assert proc.returncode == 1
    assert not out.exists()
