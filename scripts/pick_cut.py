#!/usr/bin/env python3
"""pick_cut.py - choose the end of a narration clip from silencedetect gaps (lookdev Prep).

Rules:
  * The cut point comes from a silencedetect gap boundary. words.json is used only to say which
    sentence a gap follows.
  * The sentence-end gap of a sentence-final word (text ending in . ! ?) is the gap that overlaps or
    follows that word and sits nearest the onset of the next word. Reason: word-level timestamps
    can have `end` times that lag the audio (they stretch to the next word), while the next word's
    `start` matches the end of the pause, e.g. "banking." end 15.04 / next start 15.12 / gap
    14.49-15.13.
  * The cut sits inside that gap: the last frame boundary (--fps) at or before gap_end - end_margin,
    and never earlier than gap_start + start_margin.
  * The cut must lie in [lo, hi]. Among the sentence ends that pass, the one nearest --target wins.
    If none passes, the result is a hard FAIL (exit 2). The clip is never extended past hi.

Usage (Prep):
  python3 pick_cut.py --wav SRC.wav --words SRC.words.json --target 15.5 --lo 13 --hi 18 \
      [--out-wav OUT.wav --out-words OUT.words.json --out-txt OUT.txt]
Prints one JSON object on stdout. Exit 0 = OK; exit 2 = FAIL (the reason also goes to stderr).
With --out-*: writes the wav (pcm_s24le, same rate and channels, sample-exact end), the words up to
and including the sentence-final word (any `end` past the cut is clamped to the cut), and the
transcript. It then checks the written wav's duration with ffprobe.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
from dataclasses import dataclass

SENTENCE_END = re.compile(r"[.!?][\"')\]”’]*$")


class CutFail(Exception):
    """No sentence-end gap gives a cut inside the window."""

    def __init__(self, message: str, evaluated: list[dict]):
        super().__init__(message)
        self.evaluated = evaluated


@dataclass(frozen=True)
class Gap:
    start: float
    end: float


def probe_duration(path: str) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    return float(out)


def probe_sample_rate(path: str) -> int:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=sample_rate",
         "-of", "csv=p=0", path],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    return int(out)


def parse_silencedetect(log: str, duration: float) -> list[Gap]:
    """Pair silence_start/silence_end lines; an unterminated trailing start runs to `duration`."""
    gaps: list[Gap] = []
    start: float | None = None
    for line in log.splitlines():
        m = re.search(r"silence_start:\s*(-?[\d.]+)", line)
        if m:
            start = max(0.0, float(m.group(1)))
            continue
        m = re.search(r"silence_end:\s*([\d.]+)", line)
        if m and start is not None:
            gaps.append(Gap(start, float(m.group(1))))
            start = None
    if start is not None:
        gaps.append(Gap(start, duration))
    return gaps


def silence_gaps(wav: str, noise: str = "-30dB", min_silence: float = 0.05) -> list[Gap]:
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", wav, "-af",
         f"silencedetect=noise={noise}:d={min_silence}", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg silencedetect failed ({proc.returncode}): {proc.stderr[-500:]}")
    return parse_silencedetect(proc.stderr, probe_duration(wav))


def is_sentence_end(text: str) -> bool:
    return bool(SENTENCE_END.search(text.strip()))


def map_gap(words: list[dict], i: int, gaps: list[Gap], tol: float) -> Gap | None:
    """Sentence-end gap for sentence-final word i: overlaps or follows the word, nearest the next onset."""
    w = words[i]
    nxt = words[i + 1] if i + 1 < len(words) else None
    anchor = float(nxt["start"]) if nxt else float(w["end"])
    best: tuple[float, Gap] | None = None
    for g in gaps:
        if g.end <= float(w["start"]):
            continue  # entirely before the word: not after/overlapping it
        if nxt is not None and g.start >= float(nxt["start"]) + tol:
            break  # past the next word: belongs to a later sentence
        dist = 0.0 if g.start <= anchor <= g.end else min(abs(anchor - g.start), abs(anchor - g.end))
        if best is None or dist < best[0]:
            best = (dist, g)
    if best is None or best[0] > tol:
        return None
    return best[1]


def cut_in_gap(g: Gap, fps: float, end_margin: float, start_margin: float) -> float | None:
    limit = g.end - end_margin
    cut = math.floor(limit * fps + 1e-6) / fps if fps > 0 else limit
    if cut < g.start + start_margin - 1e-9:
        return None
    return round(cut, 6)


def pick(gaps: list[Gap], words: list[dict], target: float, lo: float, hi: float, *,
         fps: float = 30.0, end_margin: float = 0.05, start_margin: float = 0.1,
         tol: float = 0.35) -> dict:
    """Return the chosen cut as a dict, or raise CutFail. Pure: no I/O."""
    evaluated: list[dict] = []
    ok: list[dict] = []
    for i, w in enumerate(words):
        if not is_sentence_end(str(w["text"])):
            continue
        row = {"index": i, "id": w.get("id"), "text": w["text"], "wordStart": float(w["start"]),
               "wordEnd": float(w["end"])}
        g = map_gap(words, i, gaps, tol)
        if g is None:
            row["status"] = "no sentence-end gap"
        else:
            row["gapStart"], row["gapEnd"] = round(g.start, 6), round(g.end, 6)
            cut = cut_in_gap(g, fps, end_margin, start_margin)
            if cut is None:
                row["status"] = f"gap shorter than {start_margin + end_margin:.2f} s"
            else:
                row["cut"] = cut
                if lo <= cut <= hi:
                    row["status"] = "in window"
                    ok.append(row)
                else:
                    row["status"] = f"cut {cut:.3f} outside {lo:g}-{hi:g} s"
        evaluated.append(row)
    if not ok:
        near = [r for r in evaluated if lo - 6 <= r["wordEnd"] <= hi + 6]
        detail = "; ".join(f'{r["text"]} -> {r["status"]}' for r in near) or "no sentence ends near the window"
        raise CutFail(f"FAIL: no sentence-end silence gap gives a cut in {lo:g}-{hi:g} s ({detail})", evaluated)
    best = min(ok, key=lambda r: (abs(r["cut"] - target), r["cut"]))
    return {"status": "OK", "cut": best["cut"], "sentenceEndIndex": best["index"],
            "sentenceEndWordId": best["id"], "sentenceEndText": best["text"],
            "gapStart": best["gapStart"], "gapEnd": best["gapEnd"], "evaluated": evaluated}


def write_outputs(src_wav: str, words: list[dict], res: dict, out_wav: str | None,
                  out_words: str | None, out_txt: str | None) -> dict:
    cut = res["cut"]
    kept = []
    for w in words[: res["sentenceEndIndex"] + 1]:
        w2 = dict(w)
        if float(w2["end"]) > cut:
            w2["end"] = cut
        kept.append(w2)
    info: dict = {"lastWordEnd": float(kept[-1]["end"]), "transcript": " ".join(str(w["text"]) for w in kept)}
    if out_wav:
        sr = probe_sample_rate(src_wav)
        n = round(cut * sr)
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-v", "error", "-n", "-i", src_wav, "-af", f"atrim=end_sample={n}",
             "-c:a", "pcm_s24le", out_wav],
            check=True,
        )
        dur = probe_duration(out_wav)
        info.update(wav=out_wav, durationSec=dur, samples=n)
        if abs(dur - cut) > 0.01:
            raise RuntimeError(f"written wav is {dur:.4f} s, expected {cut:.4f} s")
    if out_words:
        with open(out_words, "x", encoding="utf-8") as f:
            json.dump(kept, f)
        info["words"] = out_words
    if out_txt:
        with open(out_txt, "x", encoding="utf-8") as f:
            f.write(info["transcript"] + "\n")
        info["txt"] = out_txt
    return info


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Choose a narration cut inside a silencedetect gap (see module docstring).")
    ap.add_argument("--wav", required=True)
    ap.add_argument("--words", required=True)
    ap.add_argument("--target", type=float, required=True)
    ap.add_argument("--lo", type=float, required=True)
    ap.add_argument("--hi", type=float, required=True)
    ap.add_argument("--noise", default="-30dB")
    ap.add_argument("--min-silence", type=float, default=0.05)
    ap.add_argument("--fps", type=float, default=30.0, help="snap the cut down to this frame grid (0 = off)")
    ap.add_argument("--end-margin", type=float, default=0.05)
    ap.add_argument("--start-margin", type=float, default=0.1)
    ap.add_argument("--out-wav")
    ap.add_argument("--out-words")
    ap.add_argument("--out-txt")
    a = ap.parse_args(argv)

    with open(a.words, encoding="utf-8") as f:
        words = json.load(f)
    gaps = silence_gaps(a.wav, a.noise, a.min_silence)
    try:
        res = pick(gaps, words, a.target, a.lo, a.hi, fps=a.fps, end_margin=a.end_margin,
                   start_margin=a.start_margin)
    except CutFail as e:
        sys.stdout.write(json.dumps({"status": "FAIL", "reason": str(e), "evaluated": e.evaluated}) + "\n")
        sys.stderr.write(str(e) + "\n")
        return 2
    res.update(write_outputs(a.wav, words, res, a.out_wav, a.out_words, a.out_txt))
    if "lastWordEnd" not in res:
        res["lastWordEnd"] = min(float(words[res["sentenceEndIndex"]]["end"]), res["cut"])
    sys.stdout.write(json.dumps(res) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
