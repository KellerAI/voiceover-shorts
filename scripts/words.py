#!/usr/bin/env python3
"""words.py - build a core-compatible words.json from an ElevenLabs /with-timestamps alignment.

Input is the response's `alignment` object (`characters`, `character_start_times_seconds`,
`character_end_times_seconds`). `normalized_alignment` is never read: its characters are the
normalized text (e.g. digits spelled out), not the words the viewer sees.

Output matches the core plugin's words.json: a JSON array of {"id": "w<i>", "text", "start", "end"},
times in seconds rounded to 6 places, punctuation attached to its word so the core's sentence-end
rule (pick_cut.py SENTENCE_END) still finds "terms." and the like.

Algorithm (alignment_to_words):
  1. Validate: equal-length lists, N > 0, finite times, end >= start, starts non-decreasing (1e-6).
  2. Whitespace-delimited bracketed spans ("[clears throat]") are treated as whitespace, then
     any whitespace character closes the current token.
  3. Token text = its characters joined; start = first character's start; end = max of its ends.
  4. Drop whole-token tags such as "[laughs]".
  5. A token with no alphanumeric character merges into the previous word (end = max); with no
     previous word it is prepended to the next word.
  6. Monotonic and clamped: start = max(start, prev.start); end = max(end, start);
     end = min(end, wav_duration); start = min(start, end).
  7. Ids w0..w(n-1).

Usage (offline rebuild from a saved response):
  python3 words.py --from-alignment NAME.alignment.json --wav-duration SEC --out NAME.words.json
The input file is the saved response object; its `alignment` key is used. The output is opened
with mode "x": an existing file is never overwritten. Exit 0 = written; exit 1 = error (stderr).
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys

ALIGNMENT_KEYS = ("characters", "character_start_times_seconds", "character_end_times_seconds")
TAG = re.compile(r"^\[[^\[\]]*\]$")
START_TOLERANCE = 1e-6


def _finite_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _validated(alignment: object) -> tuple[list[str], list[float], list[float]]:
    if not isinstance(alignment, dict):
        raise ValueError("alignment must be a JSON object")
    missing = [k for k in ALIGNMENT_KEYS if k not in alignment]
    if missing:
        raise ValueError(f"alignment is missing {', '.join(missing)}")
    chars, starts, ends = (alignment[k] for k in ALIGNMENT_KEYS)
    if not (isinstance(chars, list) and isinstance(starts, list) and isinstance(ends, list)):
        raise ValueError("alignment fields must be lists")
    n = len(chars)
    if n == 0 or len(starts) != n or len(ends) != n:
        raise ValueError(f"alignment lists must have equal length > 0 (got {n}, {len(starts)}, {len(ends)})")
    for i, (c, s, e) in enumerate(zip(chars, starts, ends)):
        if not isinstance(c, str):
            raise ValueError(f"character {i} is not a string: {c!r}")
        if not (_finite_number(s) and _finite_number(e)):
            raise ValueError(f"character {i} has a non-finite time: start={s!r} end={e!r}")
        if e < s:
            raise ValueError(f"character {i} ends before it starts: start={s} end={e}")
        if i and s < starts[i - 1] - START_TOLERANCE:
            raise ValueError(f"character {i} starts before character {i - 1}: {s} < {starts[i - 1]}")
    return chars, [float(s) for s in starts], [float(e) for e in ends]


def _tag_mask(chars: list[str]) -> list[bool]:
    """True for characters inside a whitespace-delimited bracketed span such as "[clears throat]".

    The span runs from a "[" at text start or after whitespace to the next "]" (no "[" between)
    at text end or before whitespace, so multi-word tags are dropped before tokenizing while
    "[laughs]." (not whitespace-delimited) is kept.
    """
    n = len(chars)
    mask = [False] * n
    i = 0
    while i < n:
        if chars[i] == "[" and (i == 0 or chars[i - 1].isspace()):
            j = i + 1
            while j < n and chars[j] not in "[]":
                j += 1
            if j < n and chars[j] == "]" and (j == n - 1 or chars[j + 1].isspace()):
                for k in range(i, j + 1):
                    mask[k] = True
                i = j + 1
                continue
        i += 1
    return mask


def _tokens(chars: list[str], starts: list[float], ends: list[float]) -> list[list]:
    """Whitespace-separated tokens as [text, start, end]; bracketed tag spans count as whitespace."""
    tokens: list[list] = []
    cur: list | None = None
    for c, s, e, tag in zip(chars, starts, ends, _tag_mask(chars)):
        if tag or c.isspace():
            if cur is not None:
                tokens.append(cur)
                cur = None
        elif cur is None:
            cur = [c, s, e]
        else:
            cur[0] += c
            cur[2] = max(cur[2], e)
    if cur is not None:
        tokens.append(cur)
    return tokens


def alignment_to_words(alignment: dict, wav_duration: float) -> list[dict]:
    """Return core words.json entries for an ElevenLabs `alignment` object. Pure: no I/O.

    Raises ValueError on an invalid alignment, an invalid wav_duration, or no words at all.
    """
    if not _finite_number(wav_duration) or wav_duration <= 0:
        raise ValueError(f"wav_duration must be a finite number > 0 (got {wav_duration!r})")
    chars, starts, ends = _validated(alignment)
    tokens = [t for t in _tokens(chars, starts, ends) if not TAG.match(t[0])]

    merged: list[list] = []
    pending: list | None = None  # leading punctuation waiting for the first word
    for text, s, e in tokens:
        if not any(ch.isalnum() for ch in text):
            if merged:
                merged[-1][0] += text
                merged[-1][2] = max(merged[-1][2], e)
            elif pending is None:
                pending = [text, s, e]
            else:
                pending[0] += text
                pending[2] = max(pending[2], e)
            continue
        if pending is not None:
            text, s, e = pending[0] + text, min(pending[1], s), max(pending[2], e)
            pending = None
        merged.append([text, s, e])
    if not merged:
        raise ValueError("alignment contains no words")

    out: list[dict] = []
    prev_start: float | None = None
    for i, (text, s, e) in enumerate(merged):
        if prev_start is not None:
            s = max(s, prev_start)
        e = max(e, s)
        e = min(e, float(wav_duration))
        s = min(s, e)
        prev_start = s
        out.append({"id": f"w{i}", "text": text, "start": round(s, 6), "end": round(e, 6)})
    return out


def covers_text(alignment: dict, text: str) -> bool:
    """True when the alignment's characters spell exactly the words of `text` (whitespace-insensitive).

    A truncated or different alignment returns False. Raises ValueError on an invalid alignment.
    """
    chars, _, _ = _validated(alignment)
    return "".join(chars).split() == text.split()


def load_alignment(path: str) -> dict:
    """The `alignment` object of a saved /with-timestamps response. Never `normalized_alignment`."""
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    if not isinstance(doc, dict) or "alignment" not in doc:
        raise ValueError(f"{path}: no top-level 'alignment' object")
    return doc["alignment"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Rebuild a core words.json from a saved ElevenLabs alignment.")
    ap.add_argument("--from-alignment", required=True, metavar="ALIGNMENT_JSON",
                    help="saved /with-timestamps response (its 'alignment' key is used)")
    ap.add_argument("--wav-duration", type=float, required=True, metavar="SEC",
                    help="duration of the narration WAV; word times are clamped to it")
    ap.add_argument("--out", required=True, help="words.json to create (never overwritten)")
    a = ap.parse_args(argv)
    try:
        result = alignment_to_words(load_alignment(a.from_alignment), a.wav_duration)
        with open(a.out, "x", encoding="utf-8") as f:
            json.dump(result, f, indent=1, ensure_ascii=False)
            f.write("\n")
    except FileExistsError:
        sys.stderr.write(f"words.py: {a.out} already exists; refusing to overwrite\n")
        return 1
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"words.py: {exc}\n")
        return 1
    sys.stdout.write(json.dumps({"status": "OK", "words": len(result), "out": a.out}) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
