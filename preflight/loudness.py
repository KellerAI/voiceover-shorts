#!/usr/bin/env python3
"""Loudness preflight: voice -14 LUFS +-1, true peak <= -1.5 dBTP, SFX >= 12 dB under voice peak.

Usage:
  loudness.py MIX [--voice VOICE.wav] [--sfx SFX.wav] [--json]
  loudness.py --voice VOICE.wav [--sfx SFX.wav] [--json]

Integrated loudness is measured on the voice stem when given, else on the mix.
True peak is measured on the mix when given, else on the voice stem.
The SFX rule needs both stems (sample peak of sfx <= voice sample peak - 12 dB);
without both it is reported in metrics as skipped, not passed.
Exit: 0 pass, 1 fail, 2 usage/IO error.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

TARGET_LUFS = -14.0
LUFS_TOL = 1.0
MAX_TP = -1.5
SFX_MARGIN_DB = 12.0


class LoudnessError(Exception):
    pass


def _ffmpeg(path: str, af: str) -> str:
    if not Path(path).is_file():
        raise LoudnessError(f"not a file: {path}")
    try:
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostdin", "-i", path, "-af", af, "-f", "null", "-"],
            capture_output=True, text=True, check=False)
    except FileNotFoundError as e:
        raise LoudnessError("ffmpeg not found") from e
    if r.returncode != 0:
        raise LoudnessError(f"ffmpeg failed on {path}: {r.stderr.strip()[-300:]}")
    return r.stderr


def _num(s: str) -> float:
    return float("-inf") if "inf" in s else float(s)


def measure(path: str) -> dict:
    """Return integrated LUFS, LRA and true peak (dBTP) via ebur128=peak=true."""
    err = _ffmpeg(path, "ebur128=peak=true")
    summary = err[err.rfind("Summary:"):]
    i = re.search(r"\bI:\s+(-?[\d.]+|-?inf)\s+LUFS", summary)
    tp = re.search(r"True peak:\s*\n\s*Peak:\s+(-?[\d.]+|-?inf)\s+dBFS", summary)
    lra = re.search(r"\bLRA:\s+([\d.]+)\s+LU", summary)
    if not (i and tp):
        raise LoudnessError(f"could not parse ebur128 summary for {path}")
    return {"lufs": _num(i.group(1)), "true_peak_dbtp": _num(tp.group(1)),
            "lra": float(lra.group(1)) if lra else None}


def sample_peak(path: str) -> float:
    err = _ffmpeg(path, "astats=measure_perchannel=none:measure_overall=Peak_level")
    m = re.findall(r"Peak level dB:\s+(-?[\d.]+|-?inf)", err)
    if not m:
        raise LoudnessError(f"could not parse astats peak for {path}")
    return _num(m[-1])


def check(mix: str | None = None, voice: str | None = None, sfx: str | None = None) -> dict:
    lufs_src = voice or mix
    tp_src = mix or voice
    if not lufs_src or not tp_src:
        raise LoudnessError("need a mix or a voice stem")
    findings: list[dict] = []
    metrics: dict = {"target_lufs": TARGET_LUFS, "lufs_tolerance": LUFS_TOL,
                     "max_true_peak_dbtp": MAX_TP, "sfx_margin_db": SFX_MARGIN_DB}
    lm = measure(lufs_src)
    tm = lm if tp_src == lufs_src else measure(tp_src)
    metrics.update(lufs_source=lufs_src, lufs=lm["lufs"], lra=lm["lra"],
                   true_peak_source=tp_src, true_peak_dbtp=tm["true_peak_dbtp"])
    if abs(lm["lufs"] - TARGET_LUFS) > LUFS_TOL:
        findings.append({"t_start": None, "t_end": None, "kind": "lufs_out_of_range",
                         "detail": f"{lm['lufs']:.1f} LUFS vs {TARGET_LUFS} +-{LUFS_TOL}"})
    if tm["true_peak_dbtp"] > MAX_TP:
        findings.append({"t_start": None, "t_end": None, "kind": "true_peak_too_high",
                         "detail": f"{tm['true_peak_dbtp']:.1f} dBTP > {MAX_TP}"})
    if voice and sfx:
        vp, sp = sample_peak(voice), sample_peak(sfx)
        metrics.update(voice_peak_db=vp, sfx_peak_db=sp, sfx_under_voice_db=vp - sp)
        if sp > vp - SFX_MARGIN_DB:
            findings.append({"t_start": None, "t_end": None, "kind": "sfx_too_loud",
                             "detail": f"sfx peak {sp:.1f} dB is {vp - sp:.1f} dB under voice peak "
                                       f"{vp:.1f} dB; need >= {SFX_MARGIN_DB}"})
    else:
        metrics["sfx_check"] = "skipped: needs both --voice and --sfx stems"
    return {"name": "loudness", "passed": not findings, "findings": findings, "metrics": metrics}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("mix", nargs="?")
    p.add_argument("--voice")
    p.add_argument("--sfx")
    p.add_argument("--json", action="store_true")
    try:
        a = p.parse_args(argv)
    except SystemExit as e:
        return 0 if e.code == 0 else 2
    try:
        res = check(a.mix, a.voice, a.sfx)
    except LoudnessError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if a.json:
        print(json.dumps(res, indent=2))
    else:
        print(f"loudness: {'PASS' if res['passed'] else 'FAIL'}")
        for f in res["findings"]:
            print(f"  {f['kind']}: {f['detail']}")
    return 0 if res["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
