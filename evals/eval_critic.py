#!/usr/bin/env python3
"""Critic eval: does the rubric-driven critic rank renders the way the owner does?

For each subject (a style key, or a variant key such as K16) the eval:
  1. resolves the canonical render from styles/styles.json (read-only),
  2. runs evidence/evidence.py report on it (contact sheets + preflight checks),
  3. asks the critic for {score, mustFix, keep} using critic/rubric.md and that evidence,
then compares critic scores with the owner's ranks from feedback/calibration.json
(cohorts["global"]) by Spearman rank correlation. Subjects outside the global cohort
(K16 lives in "K-variants") are scored and reported but are not part of the Spearman set.

A planted negative is always included: a black, silent render generated with ffmpeg.
The critic must score it at or below --broken-max (default 3).

The default critic is a real model call: headless `claude -p --model opus` with the rubric,
the preflight report and the contact sheets passed inline as image blocks over
--input-format stream-json (no tools are enabled, so the critic only sees what it is sent).
The critic is blinded to the style's identity: no key, name, directory or registry notes.

CLI
  eval_critic.py --out DIR [--subjects A,C,D,E,G,H,K16] [--calibration P] [--rubric P]
                 [--styles P] [--model opus] [--effort LEVEL] [--jobs 4]
                 [--threshold 0.6] [--broken-max 3] [--timeout 900] [--json]
Exit: 0 when rho >= threshold and broken score <= broken-max, 1 otherwise,
2 on a usage, IO, evidence or critic error.

Render paths resolve against the registry's lookdev_root under $SHORTS_STUDIO_DIR
(feedback.studio_path); an unset variable is a usage error (exit 2).
"""
from __future__ import annotations

import argparse
import base64
import json
import math
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

JSON = dict[str, Any]

ROOT = Path(__file__).resolve().parents[1]
STYLES = ROOT / "styles" / "styles.json"
RUBRIC = ROOT / "critic" / "rubric.md"
CALIBRATION = ROOT / "feedback" / "calibration.json"
EVIDENCE = ROOT / "evidence" / "evidence.py"

sys.path.insert(0, str(ROOT))
from feedback import feedback  # noqa: E402

DEFAULT_SUBJECTS = ("A", "C", "D", "E", "G", "H", "K16")
BROKEN = "BROKEN"

# Narration word timings per canonical render, relative to lookdev_root. Taken from each
# render's notes.md (dir-C/notes.md, dir-D/notes.md: s61_open10; r2-15s-E/G notes:
# r2-15s; r6-S49-K16/notes.md: r6-S49). A and H share their round's script and duration.
WORDS = {
    "A": "inputs/s61_open10.words.json",
    "C": "inputs/s61_open10.words.json",
    "D": "inputs/s61_open10.words.json",
    "E": "inputs/r2-15s.words.json",
    "G": "inputs/r2-15s.words.json",
    "H": "inputs/r2-15s.words.json",
    "K16": "inputs/r6-S49.words.json",
    BROKEN: "inputs/s61_open10.words.json",
}

CRIT_SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "number", "minimum": 1, "maximum": 10},
        "mustFix": {"type": "array", "items": {"type": "string"}},
        "keep": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["score", "mustFix", "keep"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = (
    "You are the Shorts Studio critic. Score one rendered vertical short strictly by the "
    "rubric you are given, using only the evidence in this message. Return only the "
    "structured result {score, mustFix, keep}."
)


class EvalError(Exception):
    """Usage, IO, evidence or critic failure (exit 2)."""


# ---------------------------------------------------------------- ranks and Spearman

def average_ranks(values: list[float], descending: bool = False) -> list[float]:
    """1-based ranks; tied values share the mean of the ranks they span."""
    order = sorted(range(len(values)), key=lambda i: values[i], reverse=descending)
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        mean = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = mean
        i = j + 1
    return ranks


def pearson(x: list[float], y: list[float]) -> float | None:
    n = len(x)
    if n < 2 or n != len(y):
        return None
    mx, my = sum(x) / n, sum(y) / n
    sxy = sum((a - mx) * (b - my) for a, b in zip(x, y))
    sxx = sum((a - mx) ** 2 for a in x)
    syy = sum((b - my) ** 2 for b in y)
    if sxx == 0 or syy == 0:
        return None
    return sxy / math.sqrt(sxx * syy)


def spearman(owner_ranks: dict[str, float], scores: dict[str, float]) -> JSON:
    """Spearman rho (Pearson on average ranks, tie-correct) over the shared subjects.

    owner_ranks: rank per subject, 1 = best (calibration.json convention).
    scores: critic score per subject, higher = better.
    Both sides are re-ranked within the shared subset.
    """
    subjects = sorted(set(owner_ranks) & set(scores))
    j = average_ranks([float(owner_ranks[s]) for s in subjects])
    c = average_ranks([float(scores[s]) for s in subjects], descending=True)
    rho = pearson(j, c)
    return {
        "n": len(subjects),
        "subjects": subjects,
        "owner_ranks": dict(zip(subjects, j)),
        "critic_ranks": dict(zip(subjects, c)),
        "rho": None if rho is None else round(rho, 4),
    }


# ---------------------------------------------------------------- inputs

def load_json(path: Path) -> JSON:
    try:
        data: JSON = json.loads(Path(path).read_text())
        return data
    except (OSError, json.JSONDecodeError) as exc:
        raise EvalError(f"cannot read {path}: {exc}") from exc


def load_calibration(path: Path) -> JSON:
    """Return cohorts["global"]: {order, ranks}. Never falls back to a built-in order."""
    cal = load_json(path)
    try:
        glob = cal["cohorts"]["global"]
        order, ranks = list(glob["order"]), dict(glob["ranks"])
    except (KeyError, TypeError) as exc:
        raise EvalError(f"{path}: missing cohorts.global.order/ranks") from exc
    if set(order) != set(ranks):
        raise EvalError(f"{path}: global order and ranks name different subjects")
    return {"order": order, "ranks": ranks, "cohorts": sorted(cal["cohorts"])}


def lookdev_root(registry: JSON) -> Path:
    """The registry's lookdev_root under $SHORTS_STUDIO_DIR; unset or absolute is an EvalError."""
    try:
        return feedback.studio_path(registry, "lookdev_root")
    except feedback.FeedbackError as exc:
        raise EvalError(str(exc)) from exc


def resolve_subject(registry: JSON, subject: str) -> JSON:
    """Map a subject (style key or variant key) to its canonical render, read-only."""
    for style in registry["styles"]:
        if style["key"] == subject:
            mp4s = [f for f in style["source_files"]
                    if f.endswith(".mp4") and "_review" not in f]
            if not mp4s:
                raise EvalError(f"style {subject}: no canonical .mp4 in source_files")
            rel_video = Path(mp4s[0])
            entry = style
            break
        variant = next((v for v in style["variants"] if v["key"] == subject), None)
        if variant:
            rel_video = Path(variant["dir"]) / "look.mp4"
            entry = style
            break
    else:
        raise EvalError(f"unknown subject {subject!r} (not a style or variant key)")
    if subject not in WORDS:
        raise EvalError(f"no word-timing file mapped for subject {subject!r}")
    root = lookdev_root(registry)
    video = root / rel_video
    words = root / WORDS[subject]
    for p in (video, words):
        if not p.is_file():
            raise EvalError(f"{subject}: missing {p}")
    return {"subject": subject, "style_key": entry["key"], "video": video,
            "words": words, "entry": entry}


def blinded_entry(entry: JSON) -> JSON:
    """Registry fields the rubric asks for, minus anything that names the style."""
    return {k: entry[k] for k in ("content_fit", "fonts", "font_license", "publishable")
            if k in entry}


# ---------------------------------------------------------------- planted negative

def make_broken(out: Path, duration: float = 10.166667) -> Path:
    """Black, silent 1080x1920 render: unwatchable by the rubric's broken floor."""
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i", f"color=c=black:s=1080x1920:r=30:d={duration}",
           "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
           "-t", str(duration), "-c:v", "libx264", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-shortest", str(out)]
    try:
        # ubs:ignore - false positive: ubs taint regex (ubs-python.sh:801, input\\s*\\( with no word boundary) matches "input(" inside stream_input( and the "-p" literal then taints p/cmd; argv is a list, shell=False
        p = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise EvalError(f"ffmpeg failed making broken render: {exc}") from exc
    if p.returncode != 0 or not out.is_file():
        raise EvalError(f"ffmpeg failed making broken render: {p.stderr.strip()}")
    return out


# ---------------------------------------------------------------- evidence

def run_evidence(video: Path, words: Path, out: Path) -> JSON:
    """evidence.py report with the video-only preflights. Exit 0/1 are both valid."""
    cmd = [sys.executable, str(EVIDENCE), "report", str(video), "--out", str(out),
           "--words", str(words),
           "--run", "dead_frames {video}",
           "--run", f"endcard {{video}} {words}",
           "--run", "loudness {video}"]
    try:
        # ubs:ignore - false positive: ubs taint regex (ubs-python.sh:801, input\\s*\\( with no word boundary) matches "input(" inside stream_input( and the "-p" literal then taints p/cmd; argv is a list, shell=False
        p = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=1800)
    except subprocess.TimeoutExpired as exc:
        raise EvalError(f"evidence.py timed out on {video}") from exc
    if p.returncode not in (0, 1):
        raise EvalError(f"evidence.py exit {p.returncode} on {video}: {p.stderr.strip()}")
    return load_json(out / "preflight_report.json")


def build_packet(rubric: str, entry: JSON, words: Path, report: JSON, label: str) -> JSON:
    """Everything the critic sees. Paths that could name the style are scrubbed."""
    scrub = {str(report["video"]): "<video>"}

    def clean(obj: Any) -> Any:
        if isinstance(obj, dict):
            return {k: clean(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [clean(v) for v in obj]
        if isinstance(obj, str):
            for a, b in scrub.items():
                obj = obj.replace(a, b)
            return obj
        return obj

    raw = load_json(words)
    wlist = raw["words"] if isinstance(raw, dict) else raw
    transcript = " ".join(f"[{w['start']:.2f}] {w['text']}" for w in wlist)
    checks = clean(report["checks"])
    sheets = [{"t_start": s["t_start"], "t_end": s["t_end"], "frames": s["frames"],
               "path": s["path"]} for s in report["sheets"]]
    text = "\n\n".join([
        "# Rubric\n\n" + rubric,
        f"# Render under review: {label}\n\nDuration: {report['duration']:.3f} s, "
        "1080x1920. The style's identity is withheld; judge only this evidence.",
        "# Registry entry (identity fields withheld)\n\n" + json.dumps(entry, indent=1),
        "# Narration transcript with word start times (s)\n\n" + transcript,
        "# Preflight report (checks)\n\n" + json.dumps(checks, indent=1),
        "# Contact sheets\n\nThe images attached above are beat-window contact sheets, "
        "in this order (4 columns, read left to right, top to bottom; 4 fps over 2 s):\n"
        + "\n".join(f"- sheet {i}: {s['t_start']:.2f}-{s['t_end']:.2f} s, "
                    f"{s['frames']} frames" for i, s in enumerate(sheets)),
        "# Task\n\nScore this render 1-10 by the rubric's anchors and rules (preflight "
        "cap, broken floor, dislikes cost more than polish). The reference reel is not "
        "attached, so do not award 9 or above. Return score, mustFix (each item starts "
        "with a category slug and a time range when it has one) and keep.",
    ])
    return {"label": label, "text": text, "images": [s["path"] for s in sheets]}


# ---------------------------------------------------------------- critic

def validate_crit(obj: object) -> JSON:
    if not isinstance(obj, dict):
        raise EvalError(f"critic returned {type(obj).__name__}, not an object")
    score = obj.get("score")
    if isinstance(score, bool) or not isinstance(score, (int, float)) or not 1 <= score <= 10:
        raise EvalError(f"critic score out of range or not a number: {score!r}")
    for k in ("mustFix", "keep"):
        if not isinstance(obj.get(k), list) or not all(isinstance(s, str) for s in obj[k]):
            raise EvalError(f"critic {k} is not a list of strings")
    return {"score": float(score), "mustFix": obj["mustFix"], "keep": obj["keep"]}


def stream_input(packet: JSON) -> str:
    content = []
    for path in packet["images"]:
        data = base64.b64encode(Path(path).read_bytes()).decode()
        content.append({"type": "image", "source": {"type": "base64",
                        "media_type": "image/png", "data": data}})
    content.append({"type": "text", "text": packet["text"]})
    return json.dumps({"type": "user", "message": {"role": "user", "content": content}}) + "\n"


def parse_stream_result(stdout: str) -> JSON:
    """Pick the final `result` event from claude's stream-json output."""
    result = None
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(ev, dict) and ev.get("type") == "result":
            result = ev
    if result is None:
        raise EvalError("claude produced no result event")
    if result.get("is_error") or result.get("subtype") != "success":
        raise EvalError(f"claude result error: {result.get('subtype')} {result.get('result')}")
    if result.get("permission_denials"):
        raise EvalError(f"claude hit permission denials: {result['permission_denials']}")
    out = validate_crit(result.get("structured_output"))
    out["cost_usd"] = result.get("total_cost_usd")
    return out


def claude_critic(model: str = "opus", effort: str | None = None,
                  timeout: float = 900) -> Callable[[JSON], JSON]:
    """Real critic: one headless `claude -p` call per render, no tools enabled."""
    def critic(packet: JSON) -> JSON:
        cmd = ["claude", "-p", "--model", model,
               "--input-format", "stream-json", "--output-format", "stream-json",
               "--verbose", "--no-session-persistence", "--strict-mcp-config",
               "--tools", "", "--system-prompt", SYSTEM_PROMPT,
               "--json-schema", json.dumps(CRIT_SCHEMA)]
        if effort:
            cmd += ["--effort", effort]
        try:
            # ubs:ignore - false positive: ubs taint regex (ubs-python.sh:801, input\\s*\\( with no word boundary) matches "input(" inside stream_input( and the "-p" literal then taints p/cmd; argv is a list, shell=False
            p = subprocess.run(cmd, input=stream_input(packet), capture_output=True,
                               text=True, check=False, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise EvalError(f"claude call failed for {packet['label']}: {exc}") from exc
        if p.returncode != 0:
            raise EvalError(f"claude exit {p.returncode} for {packet['label']}: "
                            f"{p.stderr.strip()[-2000:]}")
        return parse_stream_result(p.stdout)
    return critic


# ---------------------------------------------------------------- eval

def evaluate(subjects: list[str], out: Path, critic: Callable[[JSON], JSON], *,
             calibration: Path | None = None, rubric: Path = RUBRIC, styles: Path = STYLES,
             threshold: float = 0.6, broken_max: float = 3.0, jobs: int = 4) -> JSON:
    calibration = feedback.read_calibration(calibration)
    cal = load_calibration(calibration)
    registry = load_json(styles)
    try:
        rubric_text = Path(rubric).read_text()
    except OSError as exc:
        raise EvalError(f"cannot read rubric {rubric}: {exc}") from exc
    out.mkdir(parents=True, exist_ok=True)

    plan = [resolve_subject(registry, s) for s in subjects]
    broken_video = make_broken(out / "broken" / "broken.mp4")
    plan.append({"subject": BROKEN, "style_key": None, "video": broken_video,
                 "words": lookdev_root(registry) / WORDS[BROKEN],
                 "entry": {"content_fit": [], "fonts": {}, "font_license": "cleared",
                           "publishable": True}})

    # Blind labels follow the plan order, which is the caller's subject list.
    for i, item in enumerate(plan, 1):
        item["label"] = f"R{i}"

    def one(item: JSON) -> JSON:
        ev_dir = out / "evidence" / item["subject"]
        report = run_evidence(item["video"], item["words"], ev_dir)
        packet = build_packet(rubric_text, blinded_entry(item["entry"]), item["words"],
                              report, item["label"])
        crit = critic(packet)
        crit = {**crit, **validate_crit(crit)}
        return {"subject": item["subject"], "style_key": item["style_key"],
                "label": item["label"], "video": str(item["video"]),
                "evidence_dir": str(ev_dir), "preflight_passed": report["passed"],
                "failed_checks": [c["name"] for c in report["checks"] if not c["passed"]],
                "sheets": len(report["sheets"]), **crit}

    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        results = list(pool.map(one, plan))

    by = {r["subject"]: r for r in results}
    broken = by.pop(BROKEN)
    scores = {s: r["score"] for s, r in by.items()}
    sp = spearman(cal["ranks"], scores)
    sp["excluded"] = sorted(set(scores) - set(cal["ranks"]))
    sp["calibration"] = str(calibration)
    rho_ok = sp["rho"] is not None and sp["rho"] >= threshold
    broken_ok = broken["score"] <= broken_max
    result = {
        "subjects": by,
        "broken": broken,
        "spearman": sp,
        "threshold": threshold,
        "broken_max": broken_max,
        "rho_ok": rho_ok,
        "broken_ok": broken_ok,
        "passed": rho_ok and broken_ok,
    }
    (out / "eval_result.json").write_text(json.dumps(result, indent=2, sort_keys=True))
    return result


def summary(result: JSON) -> str:
    lines = ["subject  score  preflight_failed"]
    for s, r in sorted(result["subjects"].items(), key=lambda kv: -kv[1]["score"]):
        lines.append(f"{s:<8} {r['score']:>5.1f}  {','.join(r['failed_checks']) or '-'}")
    b = result["broken"]
    sp = result["spearman"]
    lines += [f"{BROKEN:<8} {b['score']:>5.1f}  {','.join(b['failed_checks']) or '-'}"
              f"  (must be <= {result['broken_max']})",
              f"spearman rho={sp['rho']} n={sp['n']} over {','.join(sp['subjects'])}"
              f" (excluded: {','.join(sp['excluded']) or '-'};"
              f" threshold {result['threshold']})",
              f"passed={result['passed']}"]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--subjects", default=",".join(DEFAULT_SUBJECTS))
    ap.add_argument("--calibration", type=Path, default=feedback.read_calibration(),
                    help="calibration set (default: user file in $CLAUDE_PLUGIN_DATA/feedback/ "
                         "if present, else the shipped seed)")
    ap.add_argument("--rubric", type=Path, default=RUBRIC)
    ap.add_argument("--styles", type=Path, default=STYLES)
    ap.add_argument("--model", default="opus")
    ap.add_argument("--effort", default=None)
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--threshold", type=float, default=0.6)
    ap.add_argument("--broken-max", type=float, default=3.0)
    ap.add_argument("--timeout", type=float, default=900)
    ap.add_argument("--json", action="store_true")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code == 0 else 2
    subjects = [s.strip() for s in args.subjects.split(",") if s.strip()]
    try:
        result = evaluate(subjects, args.out,
                          claude_critic(args.model, args.effort, args.timeout),
                          calibration=args.calibration, rubric=args.rubric,
                          styles=args.styles, threshold=args.threshold,
                          broken_max=args.broken_max, jobs=args.jobs)
    except EvalError as exc:
        print(f"eval_critic: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True) if args.json else summary(result))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
