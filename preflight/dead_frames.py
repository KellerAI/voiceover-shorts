#!/usr/bin/env python3
"""Dead-frame preflight: find black or near-static spans in a rendered short.

Runs one ffmpeg pass with ``freezedetect`` (near-static spans) chained into
``blackdetect`` (black spans) and reports every span at least ``min_dur`` long.

Defaults were tuned by a parameter sweep over 12 short look-dev renders
(2026-10-23):

* ``noise=0.002``: ffmpeg's default n=0.001 flags almost nothing on these
  renders. At 0.003 a full-length render with a continuous push-in by design
  jumps from about 28 spans to about 150, so 0.002 sits just below that cliff.
  At 0.002 it surfaces sub-second static title holds (for example about
  3.7-4.33 s in two cuts of one style, and 0.83-2.00 s and 12.33-12.97 s in
  another). 11 of the 12 short renders pass. No before/after pair was found
  where this detector separates a reviewer-reported dead frame from its fix.
* ``pix_th=0.10, pic_th=0.98``: blackdetect never fired on a look-dev render at
  these values. Raising pix_th to 0.30 flags the opening (0-2.03 s) of a
  dark-palette style, which is palette, not a black gap.

Adjacent freeze spans (x-y, y-z) are deliberately NOT merged: that chain is
freezedetect resetting its reference frame as slow drift (a push-in) crosses
``noise``. Merging would report slow motion as a multi-second freeze.

Contract: ``check(path, ...) -> {"name", "passed", "findings", "metrics"}``.
CLI: ``dead_frames.py VIDEO [--json] [--noise N] [--min-dur S] [--pix-th P]
[--pic-th P]``; exit 0 pass, 1 fail, 2 usage or IO error.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

NAME = "dead_frames"
DEFAULT_NOISE = 0.002
DEFAULT_MIN_DUR = 0.5
DEFAULT_PIX_TH = 0.10
DEFAULT_PIC_TH = 0.98
# A freeze span mostly covered by a black span is the same defect; report it once.
OVERLAP_DEDUPE = 0.9
PROBE_TIMEOUT_S = 60.0
# A 323 s 1080x1920 render scans in about 20 s; 15 min is a hang, not a slow file.
SCAN_TIMEOUT_S = 900.0

_FREEZE_START = re.compile(r"freeze_start:\s*([\d.]+)")
_FREEZE_END = re.compile(r"freeze_end:\s*([\d.]+)")
_BLACK = re.compile(r"black_start:\s*([\d.]+)\s+black_end:\s*([\d.]+)")


class DeadFramesError(RuntimeError):
    """Raised for usage or IO problems (missing file, ffmpeg failure)."""


def _require(tool: str) -> str:
    path = shutil.which(tool)
    if path is None:
        raise DeadFramesError(f"{tool} not found on PATH")
    return path


def _run(cmd: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, check=False, timeout=timeout
        )
    except subprocess.TimeoutExpired as exc:
        raise DeadFramesError(
            f"{Path(cmd[0]).name} timed out after {timeout}s"
        ) from exc


def probe_duration(path: Path) -> float:
    """Return the container duration in seconds via ffprobe."""
    proc = _run(
        [
            _require("ffprobe"),
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_type:format=duration",
            "-of",
            "json",
            str(path),
        ],
        PROBE_TIMEOUT_S,
    )
    if proc.returncode != 0:
        raise DeadFramesError(f"ffprobe failed on {path}: {proc.stderr.strip()}")
    try:
        info = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise DeadFramesError(f"unreadable ffprobe output for {path}") from exc
    if not info.get("streams"):
        raise DeadFramesError(f"no video stream in {path}")
    try:
        return float(info["format"]["duration"])
    except (KeyError, TypeError, ValueError) as exc:
        raise DeadFramesError(f"no duration for {path}") from exc


def parse_spans(
    stderr: str, duration: float
) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    """Parse freezedetect and blackdetect log lines into (freeze, black) spans.

    A freeze_start with no matching freeze_end runs to ``duration`` (EOF).
    """
    starts = [float(x) for x in _FREEZE_START.findall(stderr)]
    ends = [float(x) for x in _FREEZE_END.findall(stderr)]
    freeze = list(zip(starts, ends))
    if len(starts) == len(ends) + 1:
        freeze.append((starts[-1], duration))
    black = [(float(a), float(b)) for a, b in _BLACK.findall(stderr)]
    return freeze, black


def _overlap(a: tuple[float, float], b: tuple[float, float]) -> float:
    return max(0.0, min(a[1], b[1]) - max(a[0], b[0]))


def build_findings(
    freeze: list[tuple[float, float]],
    black: list[tuple[float, float]],
    min_dur: float,
) -> list[dict[str, Any]]:
    """Turn raw spans into contract findings sorted by start time."""
    findings: list[dict[str, Any]] = []
    black_kept = [s for s in black if s[1] - s[0] >= min_dur]
    for s, e in black_kept:
        findings.append(
            {
                "t_start": round(s, 3),
                "t_end": round(e, 3),
                "kind": "black",
                "detail": f"black frame span {e - s:.2f}s",
            }
        )
    for s, e in freeze:
        dur = e - s
        if dur < min_dur:
            continue
        covered = sum(_overlap((s, e), b) for b in black_kept)
        if dur > 0 and covered / dur >= OVERLAP_DEDUPE:
            continue
        findings.append(
            {
                "t_start": round(s, 3),
                "t_end": round(e, 3),
                "kind": "freeze",
                "detail": f"near-static span {dur:.2f}s",
            }
        )
    findings.sort(key=lambda f: (f["t_start"], f["kind"]))
    return findings


def check(
    path: str | Path,
    *,
    noise: float = DEFAULT_NOISE,
    min_dur: float = DEFAULT_MIN_DUR,
    pix_th: float = DEFAULT_PIX_TH,
    pic_th: float = DEFAULT_PIC_TH,
) -> dict[str, Any]:
    """Scan ``path`` for black and near-static spans of at least ``min_dur`` s.

    Raises DeadFramesError on a missing file or an ffmpeg/ffprobe failure.
    """
    video = Path(path)
    if not video.is_file():
        raise DeadFramesError(f"no such file: {video}")
    if min_dur <= 0:
        raise DeadFramesError("min_dur must be > 0")
    duration = probe_duration(video)
    vf = (
        f"freezedetect=n={noise}:d={min_dur},"
        f"blackdetect=d={min_dur}:pix_th={pix_th}:pic_th={pic_th}"
    )
    proc = _run(
        [
            _require("ffmpeg"),
            "-hide_banner",
            "-nostats",
            "-loglevel",
            "info",
            "-i",
            str(video),
            "-map",
            "0:v:0",
            "-vf",
            vf,
            "-an",
            "-f",
            "null",
            "-",
        ],
        SCAN_TIMEOUT_S,
    )
    if proc.returncode != 0:
        tail = proc.stderr.strip().splitlines()[-3:]
        raise DeadFramesError(f"ffmpeg failed on {video}: {' | '.join(tail)}")
    freeze, black = parse_spans(proc.stderr, duration)
    findings = build_findings(freeze, black, min_dur)
    flagged = sum(f["t_end"] - f["t_start"] for f in findings)
    return {
        "name": NAME,
        "passed": not findings,
        "findings": findings,
        "metrics": {
            "file": str(video),
            "duration_s": round(duration, 3),
            "freeze_spans": sum(1 for f in findings if f["kind"] == "freeze"),
            "black_spans": sum(1 for f in findings if f["kind"] == "black"),
            "flagged_s": round(flagged, 3),
            "thresholds": {
                "noise": noise,
                "min_dur": min_dur,
                "pix_th": pix_th,
                "pic_th": pic_th,
            },
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dead_frames.py",
        description="Flag black or near-static spans (ffmpeg freezedetect + blackdetect).",
    )
    parser.add_argument("video", help="rendered video file")
    parser.add_argument(
        "--json", action="store_true", help="print the result dict as JSON"
    )
    parser.add_argument(
        "--noise",
        type=float,
        default=DEFAULT_NOISE,
        help=f"freezedetect noise tolerance (default {DEFAULT_NOISE})",
    )
    parser.add_argument(
        "--min-dur",
        type=float,
        default=DEFAULT_MIN_DUR,
        help=f"minimum span length in seconds (default {DEFAULT_MIN_DUR})",
    )
    parser.add_argument(
        "--pix-th",
        type=float,
        default=DEFAULT_PIX_TH,
        help=f"blackdetect pixel threshold (default {DEFAULT_PIX_TH})",
    )
    parser.add_argument(
        "--pic-th",
        type=float,
        default=DEFAULT_PIC_TH,
        help=f"blackdetect picture ratio threshold (default {DEFAULT_PIC_TH})",
    )
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # argparse exits 2 on usage errors already
        return int(exc.code or 0)
    try:
        result = check(
            args.video,
            noise=args.noise,
            min_dur=args.min_dur,
            pix_th=args.pix_th,
            pic_th=args.pic_th,
        )
    except DeadFramesError as exc:
        print(f"dead_frames: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        status = "PASS" if result["passed"] else "FAIL"
        print(
            f"{status} {result['metrics']['file']}: "
            f"{len(result['findings'])} span(s), {result['metrics']['flagged_s']}s flagged"
        )
        for f in result["findings"]:
            print(
                f"  {f['t_start']:8.2f}-{f['t_end']:8.2f}  {f['kind']:6}  {f['detail']}"
            )
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
