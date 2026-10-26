#!/usr/bin/env python3
"""Evidence: beat-window contact sheets and a merged preflight report.

CLI (exit 0 ok/pass, 1 a preflight check failed, 2 usage or IO error):

  evidence.py sheets VIDEO --out DIR (--words words.json | --beats 1.0,2.5)
                           [--window 2.0] [--fps 4] [--min-gap 2.0]
                           [--long-after 60] [--per-scene 1] [--scene-threshold 0.3]
  evidence.py report VIDEO --out DIR [--run "NAME ARGS..."]... [--merge JSON]...
                           [sheet options as above] [--no-sheets]
      --run   runs preflight/NAME.py with ARGS ({video} is replaced by VIDEO) and --json
      --merge folds an existing preflight result dict (or list of them) into the report

Output layout under DIR:
  sheets/beat_<index:03d>_<t:08.3f>s.png   one contact sheet per beat (fps*window frames, 4 columns)
  sheets.json                              [{index, t_start, t_end, path, frames, scene}]
  preflight_report.json                    see build_report()

preflight_report.json:
  {"video": str, "duration": float, "passed": bool,
   "checks": [{"name", "passed", "findings": [{"t_start","t_end","kind","detail"}], "metrics": {}}],
   "sheets": [<sheets.json entries>], "beats": [float], "sampling": {...}}
"""
from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

PREFLIGHT_DIR = Path(__file__).resolve().parents[1] / "preflight"
COLUMNS = 4


class EvidenceError(Exception):
    """Usage or IO problem (CLI exit 2)."""


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


def probe_duration(video: Path) -> float:
    p = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
              "-of", "default=nw=1:nk=1", str(video)])
    if p.returncode != 0:
        raise EvidenceError(f"ffprobe failed on {video}: {p.stderr.strip()}")
    try:
        return float(p.stdout.strip())
    except ValueError as exc:
        raise EvidenceError(f"ffprobe gave no duration for {video}") from exc


def load_beats(words_json: Path | None, beats_arg: str | None, min_gap: float) -> list[float]:
    """Beats from a words JSON or an explicit list, sorted, thinned to >= min_gap apart.

    Words JSON: {"beats": [..]} | [{"start": s, ...}] | {"words": [{"start": s, ...}]}.
    """
    raw: list[float] = []
    if beats_arg is not None:
        try:
            raw = [float(x) for x in beats_arg.split(",") if x.strip()]
        except ValueError as exc:
            raise EvidenceError(f"bad --beats value: {beats_arg!r}") from exc
    elif words_json is not None:
        try:
            data = json.loads(words_json.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise EvidenceError(f"cannot read words JSON {words_json}: {exc}") from exc
        if isinstance(data, dict) and "beats" in data:
            raw = [float(x) for x in data["beats"]]
        else:
            words = data.get("words") if isinstance(data, dict) else data
            if not isinstance(words, list):
                raise EvidenceError("words JSON must be a list, or an object with 'words' or 'beats'")
            for w in words:
                if not isinstance(w, dict) or "start" not in w:
                    raise EvidenceError(f"word entry without 'start': {w!r}")
                raw.append(float(w["start"]))
    else:
        raise EvidenceError("give --words or --beats")
    if any(b < 0 for b in raw):
        raise EvidenceError("negative beat time")
    out: list[float] = []
    for b in sorted(raw):
        if not out or b - out[-1] >= min_gap:
            out.append(round(b, 3))
    return out


def detect_scene_cuts(video: Path, threshold: float) -> list[float]:
    p = _run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(video), "-an",
              "-vf", f"select='gt(scene,{threshold})',showinfo", "-f", "null", "-"])
    if p.returncode != 0:
        raise EvidenceError(f"scene detection failed: {p.stderr.strip()[-300:]}")
    return [float(m) for m in re.findall(r"pts_time:([0-9.]+)", p.stderr)]


def sample_per_scene(beats: list[float], cuts: list[float], per_scene: int) -> list[tuple[float, int]]:
    """Keep at most per_scene beats per scene, evenly spread; returns (beat, scene_index)."""
    scenes: dict[int, list[float]] = {}
    for b in beats:
        idx = sum(1 for c in cuts if c <= b)
        scenes.setdefault(idx, []).append(b)
    picked: list[tuple[float, int]] = []
    for idx, bs in scenes.items():
        if len(bs) <= per_scene:
            chosen = bs
        else:
            step = len(bs) / per_scene
            chosen = [bs[int(i * step)] for i in range(per_scene)]
        picked.extend((b, idx) for b in chosen)
    return sorted(picked)


def make_sheet(video: Path, t: float, window: float, fps: int, out_png: Path, duration: float) -> int:
    frames = max(1, int(round(window * fps)))
    rows = -(-frames // COLUMNS)
    vf = f"fps={fps},scale=240:-2,tile={COLUMNS}x{rows}:padding=2:margin=2"
    p = _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{t:.3f}",
              "-t", f"{window:.3f}", "-i", str(video), "-an", "-vf", vf,
              "-frames:v", "1", str(out_png)])
    if p.returncode != 0 or not out_png.exists():
        raise EvidenceError(f"contact sheet at {t}s failed: {p.stderr.strip()}")
    return frames


def build_sheets(video: Path, out: Path, beats: list[float], window: float = 2.0, fps: int = 4,
                 long_after: float = 60.0, per_scene: int = 1,
                 scene_threshold: float = 0.3) -> dict:
    duration = probe_duration(video)
    sampling = {"long": duration > long_after, "window": window, "fps": fps,
                "beats_in": len(beats), "per_scene": None, "scene_cuts": []}
    in_range = [b for b in beats if b < duration]
    scene_of: dict[float, int] = {}
    if sampling["long"]:
        cuts = detect_scene_cuts(video, scene_threshold)
        sampling["per_scene"] = per_scene
        sampling["scene_cuts"] = cuts
        chosen = sample_per_scene(in_range, cuts, per_scene)
        in_range = [b for b, _ in chosen]
        scene_of = dict(chosen)
    sheets_dir = out / "sheets"
    sheets_dir.mkdir(parents=True, exist_ok=True)
    entries = []
    for i, t in enumerate(in_range):
        png = sheets_dir / f"beat_{i:03d}_{t:08.3f}s.png"
        frames = make_sheet(video, t, window, fps, png, duration)
        entries.append({"index": i, "t_start": t, "t_end": round(min(t + window, duration), 3),
                        "path": str(png), "frames": frames, "scene": scene_of.get(t)})
    sampling["sheets_out"] = len(entries)
    (out / "sheets.json").write_text(json.dumps(entries, indent=2))
    return {"duration": duration, "sheets": entries, "beats": in_range, "sampling": sampling}


def _runner_error(name: str, detail: str) -> dict:
    return {"name": name, "passed": False, "metrics": {},
            "findings": [{"t_start": 0.0, "t_end": 0.0, "kind": "runner_error", "detail": detail}]}


def _normalize(d: dict, fallback_name: str) -> dict:
    need = {"name", "passed", "findings", "metrics"}
    if not isinstance(d, dict) or not need <= d.keys():
        return _runner_error(fallback_name, f"result violates preflight contract: {str(d)[:200]}")
    return {"name": str(d["name"]), "passed": bool(d["passed"]),
            "findings": list(d["findings"]), "metrics": dict(d["metrics"])}


def run_preflight(spec: str, video: Path) -> dict:
    """Run one preflight CLI (spec = 'NAME ARGS...'); exit 0 pass, 1 fail, other = runner error."""
    parts = shlex.split(spec.replace("{video}", shlex.quote(str(video))))
    if not parts:
        return _runner_error("?", "empty --run spec")
    name, args = parts[0], parts[1:]
    script = PREFLIGHT_DIR / f"{name}.py"
    if not script.exists():
        return _runner_error(name, f"no such preflight module: {script}")
    p = _run([sys.executable, str(script), *args, "--json"])
    if p.returncode not in (0, 1):
        return _runner_error(name, f"exit {p.returncode}: {p.stderr.strip()[-300:]}")
    try:
        res = _normalize(json.loads(p.stdout), name)
    except json.JSONDecodeError:
        return _runner_error(name, f"non-JSON stdout: {p.stdout[:200]!r}")
    if (p.returncode == 0) != res["passed"]:
        return _runner_error(name, f"exit code {p.returncode} disagrees with passed={res['passed']}")
    return res


def merge_file(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"cannot read --merge {path}: {exc}") from exc
    items = data if isinstance(data, list) else [data]
    return [_normalize(d, path.stem) for d in items]


def build_report(video: Path, checks: list[dict], sheet_info: dict | None, duration: float) -> dict:
    return {
        "video": str(video),
        "duration": duration,
        "passed": all(c["passed"] for c in checks),
        "checks": checks,
        "sheets": sheet_info["sheets"] if sheet_info else [],
        "beats": sheet_info["beats"] if sheet_info else [],
        "sampling": sheet_info["sampling"] if sheet_info else None,
    }


def _add_sheet_args(p: argparse.ArgumentParser, required: bool) -> None:
    p.add_argument("--words", type=Path)
    p.add_argument("--beats")
    p.add_argument("--window", type=float, default=2.0)
    p.add_argument("--fps", type=int, default=4)
    p.add_argument("--min-gap", type=float, default=2.0)
    p.add_argument("--long-after", type=float, default=60.0)
    p.add_argument("--per-scene", type=int, default=1)
    p.add_argument("--scene-threshold", type=float, default=0.3)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("sheets")
    sp.add_argument("video", type=Path)
    sp.add_argument("--out", type=Path, required=True)
    _add_sheet_args(sp, True)
    rp = sub.add_parser("report")
    rp.add_argument("video", type=Path)
    rp.add_argument("--out", type=Path, required=True)
    rp.add_argument("--run", action="append", default=[])
    rp.add_argument("--merge", action="append", type=Path, default=[])
    rp.add_argument("--no-sheets", action="store_true")
    _add_sheet_args(rp, False)
    try:
        args = ap.parse_args(argv)
    except SystemExit as e:
        return 2 if e.code else 0
    try:
        if not args.video.is_file():
            raise EvidenceError(f"video not found: {args.video}")
        args.out.mkdir(parents=True, exist_ok=True)
        info = None
        if args.cmd == "sheets" or not args.no_sheets:
            beats = load_beats(args.words, args.beats, args.min_gap)
            info = build_sheets(args.video, args.out, beats, args.window, args.fps,
                                args.long_after, args.per_scene, args.scene_threshold)
        if args.cmd == "sheets":
            if info is None:
                raise EvidenceError("internal error: sheets command produced no sheet info")
            print(json.dumps(info["sheets"], indent=2))
            return 0
        checks = [run_preflight(s, args.video) for s in args.run]
        for m in args.merge:
            checks.extend(merge_file(m))
        report = build_report(args.video, checks, info,
                              info["duration"] if info else probe_duration(args.video))
        (args.out / "preflight_report.json").write_text(json.dumps(report, indent=2))
        print(json.dumps(report, indent=2))
        return 0 if report["passed"] else 1
    except EvidenceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
