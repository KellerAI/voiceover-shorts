#!/usr/bin/env python3
"""Preflight: end card must hold >= 1.5 s after the last spoken word.

Rules (all must hold to pass):
  too_short      end card visible for < --min-hold seconds after lastWordEnd
                 (visible = duration - max(card_start, lastWordEnd)).
  black_frame    any frame in the end-card window [max(card_start, lastWordEnd),
                 duration] is near-black (mean luma <= --black-luma), i.e. the
                 video cuts or fades to black.
  covers_last_word  the card starts before lastWordEnd (minus --cover-tol), so it
                 is drawn over the last word.
  end_card_not_found  detection method could not locate a card.

Inputs
  video          path to the rendered video (needs ffmpeg + ffprobe on PATH)
  words          words JSON: a list of {"id","text","start","end"} (the lookdev
                 *.words.json format), or {"words": [...]}. lastWordEnd = max end.

End-card detection (--method)
  scene (default)  decode the video as small gray frames at --fps, compute the
                   mean absolute difference between consecutive frames and take
                   the LAST hard cut (diff > --scene-threshold) that lands on a
                   non-black frame. Cuts into black are skipped on purpose so a
                   trailing black cut does not masquerade as the card. Assumes
                   the card is entered by a hard cut; use explicit otherwise.
  explicit         --end-card-start SECONDS (also overrides scene if given).

CLI
  endcard.py VIDEO WORDS_JSON [--method scene|explicit] [--end-card-start S]
             [--min-hold 1.5] [--fps 30] [--scene-threshold 25]
             [--black-luma 18] [--cover-tol 0.05] [--json]
  Exit 0 pass, 1 fail, 2 usage/IO error.

JSON: {"name": "endcard", "passed": bool,
       "findings": [{"t_start","t_end","kind","detail"}],
       "metrics": {"duration","last_word_end","end_card_start","method",
                   "hold_after_last_word","min_hold","min_tail_luma"}}
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

import numpy as np

W, H = 32, 56


class PreflightIOError(Exception):
    pass


def load_last_word_end(words_path) -> float:
    try:
        data = json.loads(Path(words_path).read_text())
    except (OSError, ValueError) as e:
        raise PreflightIOError(f"cannot read words json {words_path}: {e}")
    if isinstance(data, dict):
        data = data.get("words")
    if not isinstance(data, list) or not data:
        raise PreflightIOError("words json must be a non-empty list (or {'words': [...]})")
    try:
        return max(float(w["end"]) for w in data)
    except (KeyError, TypeError, ValueError) as e:
        raise PreflightIOError(f"word entries need numeric 'end': {e}")


def probe_duration(video) -> float:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(video)],
            capture_output=True, text=True, check=True).stdout.strip()
        return float(out)
    except (OSError, subprocess.CalledProcessError, ValueError) as e:
        detail = getattr(e, "stderr", "") or e
        raise PreflightIOError(f"ffprobe failed for {video}: {detail}")


def decode_frames(video, fps: int) -> np.ndarray:
    cmd = ["ffmpeg", "-v", "error", "-i", str(video), "-vf",
           f"fps={fps},scale={W}:{H},format=gray", "-f", "rawvideo", "-"]
    try:
        p = subprocess.run(cmd, capture_output=True, check=True)
    except (OSError, subprocess.CalledProcessError) as e:
        detail = getattr(e, "stderr", b"")
        raise PreflightIOError(f"ffmpeg decode failed: {detail.decode(errors='replace') or e}")
    n = len(p.stdout) // (W * H)
    if n == 0:
        raise PreflightIOError("no video frames decoded")
    return np.frombuffer(p.stdout, np.uint8)[: n * W * H].reshape(n, W * H)


def detect_card_start(frames, fps, threshold, black_luma):
    luma = frames.mean(axis=1)
    diffs = np.abs(frames[1:].astype(np.int16) - frames[:-1].astype(np.int16)).mean(axis=1)
    for i in range(len(diffs) - 1, -1, -1):
        if diffs[i] > threshold and luma[i + 1] > black_luma:
            return (i + 1) / fps
    return None


def _runs(mask, fps):
    runs, start = [], None
    for i, m in enumerate(mask):
        if m and start is None:
            start = i
        if not m and start is not None:
            runs.append((start / fps, i / fps))
            start = None
    if start is not None:
        runs.append((start / fps, len(mask) / fps))
    return runs


def check(video, words, method="scene", end_card_start=None, min_hold=1.5, fps=30,
          scene_threshold=25.0, black_luma=18.0, cover_tol=0.05) -> dict:
    last_end = load_last_word_end(words)
    duration = probe_duration(video)
    frames = decode_frames(video, fps)
    luma = frames.mean(axis=1)
    findings = []
    if end_card_start is not None:
        method = "explicit"
    if method == "explicit":
        if end_card_start is None:
            raise PreflightIOError("--method explicit needs --end-card-start")
        card = float(end_card_start)
    elif method == "scene":
        card = detect_card_start(frames, fps, scene_threshold, black_luma)
    else:
        raise PreflightIOError(f"unknown method {method}")

    metrics = {"duration": round(duration, 3), "last_word_end": round(last_end, 3),
               "end_card_start": None if card is None else round(card, 3),
               "method": method, "min_hold": min_hold}
    if card is None:
        findings.append({"t_start": last_end, "t_end": duration, "kind": "end_card_not_found",
                         "detail": "no hard cut to a non-black frame found; pass --end-card-start"})
        metrics.update(hold_after_last_word=None, min_tail_luma=None)
        return {"name": "endcard", "passed": False, "findings": findings, "metrics": metrics}

    win_start = max(card, last_end)
    hold = duration - win_start
    metrics["hold_after_last_word"] = round(hold, 3)
    if hold < min_hold:
        findings.append({"t_start": win_start, "t_end": duration, "kind": "too_short",
                         "detail": f"end card visible {hold:.2f}s after lastWordEnd, need >= {min_hold}s"})
    if card < last_end - cover_tol:
        findings.append({"t_start": card, "t_end": last_end, "kind": "covers_last_word",
                         "detail": f"end card starts {card:.2f}s, before lastWordEnd {last_end:.2f}s"})
    idx0 = min(int(win_start * fps), len(luma) - 1)
    tail = luma[idx0:]
    metrics["min_tail_luma"] = round(float(tail.min()), 2)
    for a, b in _runs(tail <= black_luma, fps):
        findings.append({"t_start": round(win_start + a, 3), "t_end": round(win_start + b, 3),
                         "kind": "black_frame",
                         "detail": f"near-black frames (luma <= {black_luma}) in end-card window"})
    return {"name": "endcard", "passed": not findings, "findings": findings, "metrics": metrics}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("video")
    ap.add_argument("words")
    ap.add_argument("--method", choices=["scene", "explicit"], default="scene")
    ap.add_argument("--end-card-start", type=float)
    ap.add_argument("--min-hold", type=float, default=1.5)
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--scene-threshold", type=float, default=25.0)
    ap.add_argument("--black-luma", type=float, default=18.0)
    ap.add_argument("--cover-tol", type=float, default=0.05)
    ap.add_argument("--json", action="store_true")
    try:
        a = ap.parse_args(argv)
    except SystemExit as e:
        return 0 if e.code == 0 else 2
    try:
        res = check(a.video, a.words, a.method, a.end_card_start, a.min_hold, a.fps,
                    a.scene_threshold, a.black_luma, a.cover_tol)
    except PreflightIOError as e:
        print(f"endcard: error: {e}", file=sys.stderr)
        return 2
    if a.json:
        print(json.dumps(res, indent=2))
    else:
        print(("PASS" if res["passed"] else "FAIL"), json.dumps(res["metrics"]))
        for f in res["findings"]:
            print(f"  [{f['kind']}] {f['t_start']:.2f}-{f['t_end']:.2f}s {f['detail']}", file=sys.stderr)
    return 0 if res["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
