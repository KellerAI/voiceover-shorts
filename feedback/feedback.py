#!/usr/bin/env python3
"""Per-video human ratings store for Shorts Studio, and the critic calibration set built from it.

Usage:
  feedback.py collect --style KEY --video PATH --rater NAME --score N
                      [--variant V] [--cohort C] [--liked TAG ...] [--disliked TAG ...]
                      [--text TEXT] [--ts ISO8601] [--store PATH] [--styles PATH]
  feedback.py collect --interactive [--store PATH] [--styles PATH]
  feedback.py export [--format json|csv] [--store PATH] [--styles PATH]
  feedback.py ingest [--store PATH] [--out PATH] [--styles PATH]
  feedback.py seed   [--store PATH] [--feedback-md PATH] [--styles PATH]

Store: ratings.jsonl, append-only, one JSON object per line (schema: ROW_FIELDS).
A later rating by the same rater for the same (style_key, variant, cohort, video) supersedes
an earlier one when ingesting; earlier lines are never rewritten.

Calibration set: calibration.json, written by `ingest`. evals/eval_critic.py reads
`cohorts["global"]["order"]` (and `ranks` for Spearman) instead of a hard-coded order.

Where they live: the shipped feedback/ratings.jsonl and feedback/calibration.json are read-only
seeds. The user's own store and calibration live in $CLAUDE_PLUGIN_DATA/feedback/; the first
write there copies the seeds in (see user_feedback_dir). Writes (collect, seed, ingest) go to
an explicit --store/--out or to that directory; with $CLAUDE_PLUGIN_DATA unset and no explicit
path they exit 2. Nothing is ever written inside the plugin root. Reads (export, and the
read_store/read_calibration helpers) use the user file when it exists, else the seed.

Studio paths: the registry's `lookdev_root` and `feedback_file` are relative to the shorts-studio
checkout named by $SHORTS_STUDIO_DIR (see studio_path). Commands that need them exit 2 when it
is unset.

Exit: 0 ok, 2 usage/IO error or a malformed row (the whole run is refused).
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
# Shipped, read-only seeds. STORE/CALIBRATION keep their names for importers.
STORE = SEED_STORE = PLUGIN_ROOT / "feedback" / "ratings.jsonl"
CALIBRATION = SEED_CALIBRATION = PLUGIN_ROOT / "feedback" / "calibration.json"
DATA_ENV = "CLAUDE_PLUGIN_DATA"
STORE_NAME, CALIBRATION_NAME = "ratings.jsonl", "calibration.json"
STYLES = PLUGIN_ROOT / "styles" / "styles.json"
SCHEMA_VERSION = 1
SCORE_MIN, SCORE_MAX = 1, 10
GLOBAL = "global"

# field -> (accepted types, nullable)
ROW_FIELDS: dict[str, tuple[tuple[type, ...], bool]] = {
    "style_key": ((str,), False),
    "variant": ((str,), True),
    "cohort": ((str,), False),
    "video": ((str,), False),
    "rater": ((str,), False),
    "score": ((int, float), False),
    "score_origin": ((str,), False),
    "liked": ((list,), False),
    "disliked": ((list,), False),
    "text": ((str,), False),
    "ts": ((str,), False),
    "source": ((str,), True),
}
SCORE_ORIGINS = ("rater", "mapped")

# Seeds: the owner's judgments in Shorts-Feedback.md (registry `feedback_file`).
# The owner gave no numbers (critic/rubric.md "Calibration exemplars"); score_origin "mapped"
# marks scores placed on the rubric anchors by this tool, not by the owner:
# leaders in the 5-6 "usable with fixes" band ordered D > C > E (rubric.md, ":21"),
# polish-only B and F at 4.5 (below leader E, above dislikes),
# named dislikes A, G, H at 4 (rubric.md "Dislikes cost more than polish"),
# K16 ranked only within the K variants (rubric.md, ":326"), so it gets its own cohort.
SEEDS: list[dict[str, Any]] = [
    {"line": 36, "style_key": "A", "variant": None, "cohort": GLOBAL,
     "video": "share/round1_S61_A-kinetic-type.mp4", "score": 4.0,
     "liked": ["content_fit:press-data"], "disliked": ["pacing", "polish"]},
    {"line": 50, "style_key": "B", "variant": None, "cohort": GLOBAL,
     "video": "share/round1_S61_B-cinematic-depth.mp4", "score": 4.5,
     "liked": ["content_fit:workflow"], "disliked": ["polish"]},
    {"line": 64, "style_key": "C", "variant": None, "cohort": GLOBAL,
     "video": "share/round1_S61_C-product-ui.mp4", "score": 5.5,
     "liked": ["close_second", "content_fit:ui"], "disliked": []},
    {"line": 78, "style_key": "D", "variant": None, "cohort": GLOBAL,
     "video": "share/round1_S61_D-reel-native.mp4", "score": 6.0,
     "liked": ["best_so_far", "content_fit:symbolic"], "disliked": ["alignment"]},
    {"line": 96, "style_key": "E", "variant": None, "cohort": GLOBAL,
     "video": "share/round2_S61_E-ui-kinetic-type.mp4", "score": 5.0,
     "liked": ["clean"], "disliked": ["polish"]},
    {"line": 110, "style_key": "F", "variant": None, "cohort": GLOBAL,
     "video": "share/round2_S61_F-isometric.mp4", "score": 4.5,
     "liked": ["decent_style"], "disliked": ["polish"]},
    {"line": 124, "style_key": "G", "variant": None, "cohort": GLOBAL,
     "video": "share/round2_S61_G-editorial-print.mp4", "score": 4.0,
     "liked": [], "disliked": ["busy_frame"]},
    {"line": 138, "style_key": "H", "variant": None, "cohort": GLOBAL,
     "video": "share/round2_S61_H-terminal-dataviz.mp4", "score": 4.0,
     "liked": [], "disliked": ["busy_frame", "polish"]},
    {"line": 326, "style_key": "K", "variant": "K16", "cohort": "K-variants",
     "video": "share/round6_S49_K16-pixel-arcade-16bit.mp4", "score": 5.0,
     "liked": ["best_arcade_version"], "disliked": ["captions"]},
]
SEED_TS = "2026-10-22T00:00:00+00:00"  # Shorts-Feedback.md:3 date


class FeedbackError(Exception):
    """Usage, IO or validation error; the CLI maps it to exit 2."""


def load_registry(styles_path: Path) -> dict[str, Any]:
    try:
        data = json.loads(styles_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise FeedbackError(f"cannot read styles registry {styles_path}: {e}") from e
    return {s["key"]: s for s in data["styles"]} | {"__meta__": data}


STUDIO_ENV = "SHORTS_STUDIO_DIR"


def studio_path(meta: dict[str, Any], field: str) -> Path:
    """Resolve a registry path field (lookdev_root, feedback_file) against $SHORTS_STUDIO_DIR.

    The registry stores these relative to the studio checkout so it carries no machine paths.
    Fails closed: an unset variable, a missing field or an absolute value is an error.
    """
    rel = meta.get(field)
    if not isinstance(rel, str) or not rel:
        raise FeedbackError(f"styles registry has no {field}")
    if Path(rel).is_absolute():
        raise FeedbackError(f"styles registry {field} must be relative to ${STUDIO_ENV}, got {rel}")
    base = os.environ.get(STUDIO_ENV, "").strip()
    if not base:
        raise FeedbackError(f"${STUDIO_ENV} is not set; set it to the shorts-studio checkout "
                            f"that holds {rel}")
    return Path(base) / rel


def user_feedback_dir() -> Path | None:
    """$CLAUDE_PLUGIN_DATA/feedback, or None when the variable is unset or blank."""
    base = os.environ.get(DATA_ENV, "").strip()
    if not base:
        return None
    if not Path(base).is_absolute():
        raise FeedbackError(f"${DATA_ENV} must be an absolute path, got {base}")
    return Path(base) / "feedback"


def _user_file(name: str) -> Path | None:
    d = user_feedback_dir()
    return None if d is None else d / name


def _same_path(a: Path, b: Path | None) -> bool:
    return b is not None and Path(a).resolve() == Path(b).resolve()


def _refuse_plugin_root(path: Path) -> None:
    """Fail closed on any write target inside the plugin root (the shipped seeds included)."""
    if Path(path).resolve().is_relative_to(PLUGIN_ROOT):
        raise FeedbackError(f"refusing to write {path}: it is inside the plugin root {PLUGIN_ROOT}, "
                            f"whose feedback files are read-only seeds; write to "
                            f"${DATA_ENV}/feedback/ or another path outside the plugin")


def write_target(explicit: Path | None, name: str, flag: str) -> Path:
    """Where a write goes: the explicit path, else $CLAUDE_PLUGIN_DATA/feedback/<name>.

    Fails closed when neither is available, or when the target is inside the plugin root.
    """
    path = explicit if explicit is not None else _user_file(name)
    if path is None:
        raise FeedbackError(f"${DATA_ENV} is not set and no {flag} was given; refusing to write "
                            f"(the shipped feedback files are read-only seeds). Set ${DATA_ENV} "
                            f"or pass {flag} PATH outside the plugin root")
    _refuse_plugin_root(path)
    return path


def read_store(explicit: Path | None = None) -> Path:
    """The store to read: explicit, else the user store when it exists, else the shipped seed."""
    if explicit is not None:
        return explicit
    user = _user_file(STORE_NAME)
    return user if user is not None and user.exists() else SEED_STORE


def read_calibration(explicit: Path | None = None) -> Path:
    """The calibration set to read: explicit, else the user one when it exists, else the seed."""
    if explicit is not None:
        return explicit
    user = _user_file(CALIBRATION_NAME)
    return user if user is not None and user.exists() else SEED_CALIBRATION


def seed_user_store(store: Path) -> None:
    """First write to $CLAUDE_PLUGIN_DATA/feedback/ratings.jsonl: copy the shipped seeds in.

    No-op for any other path or when the user store already exists. The calibration seed is
    copied alongside it (unless one is already there) so the pair stays consistent until the
    next ingest.
    """
    if store.exists() or not _same_path(store, _user_file(STORE_NAME)):
        return
    cal = store.parent / CALIBRATION_NAME
    try:
        store.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(SEED_STORE, store)
        if not cal.exists():
            shutil.copyfile(SEED_CALIBRATION, cal)
    except OSError as e:
        raise FeedbackError(f"cannot seed user store {store} from {SEED_STORE}: {e}") from e


def _registry_styles(registry: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in registry.items() if k != "__meta__"}


def validate_row(row: Any, registry: dict[str, Any]) -> dict[str, Any]:
    """Return the row if it matches the schema; raise FeedbackError naming the first problem."""
    if not isinstance(row, dict):
        raise FeedbackError("row is not a JSON object")
    missing = [f for f in ROW_FIELDS if f not in row]
    if missing:
        raise FeedbackError(f"missing field(s): {', '.join(missing)}")
    extra = sorted(set(row) - set(ROW_FIELDS))
    if extra:
        raise FeedbackError(f"unknown field(s): {', '.join(extra)}")
    for field, (types, nullable) in ROW_FIELDS.items():
        value = row[field]
        if value is None and nullable:
            continue
        if isinstance(value, bool) or not isinstance(value, types):
            raise FeedbackError(f"{field}: wrong type {type(value).__name__}")
    for field in ("style_key", "cohort", "video", "rater"):
        if not row[field].strip():
            raise FeedbackError(f"{field}: empty")
    score = float(row["score"])
    if not SCORE_MIN <= score <= SCORE_MAX:
        raise FeedbackError(f"score: {row['score']} outside {SCORE_MIN}-{SCORE_MAX}")
    if row["score_origin"] not in SCORE_ORIGINS:
        raise FeedbackError(f"score_origin: must be one of {SCORE_ORIGINS}")
    for field in ("liked", "disliked"):
        if not all(isinstance(t, str) and t.strip() for t in row[field]):
            raise FeedbackError(f"{field}: tags must be non-empty strings")
    styles = _registry_styles(registry)
    style = styles.get(row["style_key"])
    if style is None:
        raise FeedbackError(f"style_key: {row['style_key']!r} not in styles registry")
    if row["variant"] is not None:
        variants = {v["key"] for v in style.get("variants", [])}
        if row["variant"] not in variants:
            raise FeedbackError(f"variant: {row['variant']!r} not a variant of style {row['style_key']}")
    try:
        ts = datetime.fromisoformat(row["ts"])
    except ValueError as e:
        raise FeedbackError(f"ts: not ISO-8601: {row['ts']!r}") from e
    if ts.tzinfo is None:
        raise FeedbackError("ts: needs a timezone offset")
    return row


def load_store(store: Path, registry: dict[str, Any]) -> list[dict[str, Any]]:
    """Read every row; any malformed line refuses the whole store."""
    try:
        text = store.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError as e:
        raise FeedbackError(f"cannot read store {store}: {e}") from e
    rows = []
    for n, line in enumerate(text.splitlines(), start=1):
        try:
            row = json.loads(line)
        except json.JSONDecodeError as e:
            raise FeedbackError(f"{store}:{n}: not valid JSON ({e.msg})") from e
        try:
            rows.append(validate_row(row, registry))
        except FeedbackError as e:
            raise FeedbackError(f"{store}:{n}: {e}") from e
    return rows


def append_row(store: Path, row: dict[str, Any], registry: dict[str, Any], *,
               seed: bool = True) -> None:
    """Validate the row and the existing store, then append one line. Never rewrites.

    Refuses a store inside the plugin root. A first write to the user store copies the
    shipped seeds in first, unless seed is False (the `seed` command builds it from scratch).
    """
    _refuse_plugin_root(store)
    validate_row(row, registry)
    if seed:
        seed_user_store(store)
    load_store(store, registry)
    store.parent.mkdir(parents=True, exist_ok=True)
    with store.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n")


def subject(row: dict[str, Any]) -> str:
    return str(row["variant"] or row["style_key"])


def _average_ranks(order: list[str], means: dict[str, float]) -> dict[str, float]:
    """1-based ranks with ties sharing the mean of their positions (Spearman convention)."""
    ranks: dict[str, float] = {}
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and means[order[j + 1]] == means[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def build_calibration(rows: list[dict[str, Any]], store: Path) -> dict[str, Any]:
    """Latest rating per (rater, style_key, variant, cohort, video) wins; mean per subject per cohort."""
    latest: dict[tuple[str, str, str | None, str, str], tuple[datetime, int, dict[str, Any]]] = {}
    for idx, row in enumerate(rows):
        key = (row["rater"], row["style_key"], row["variant"], row["cohort"], row["video"])
        stamp = (datetime.fromisoformat(row["ts"]), idx)
        if key not in latest or stamp >= latest[key][:2]:
            latest[key] = (stamp[0], idx, row)
    cohorts: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for _, _, row in latest.values():
        cohorts.setdefault(row["cohort"], {}).setdefault(subject(row), []).append(row)
    out_cohorts: dict[str, Any] = {}
    for cohort in sorted(cohorts):
        subjects = cohorts[cohort]
        means = {s: round(sum(float(r["score"]) for r in rs) / len(rs), 4) for s, rs in subjects.items()}
        order = sorted(means, key=lambda s: (-means[s], s))
        out_cohorts[cohort] = {
            "order": order,
            "ranks": _average_ranks(order, means),
            "subjects": {
                s: {
                    "style_key": subjects[s][0]["style_key"],
                    "variant": subjects[s][0]["variant"],
                    "mean_score": means[s],
                    "n": len(subjects[s]),
                    "ratings": sorted(
                        ({"rater": r["rater"], "video": r["video"], "score": r["score"],
                          "score_origin": r["score_origin"], "text": r["text"], "source": r["source"],
                          "ts": r["ts"]} for r in subjects[s]),
                        key=lambda d: (d["ts"], d["rater"], d["video"])),
                }
                for s in sorted(subjects)
            },
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "store": _plugin_relative(store),
        "rows_read": len(rows),
        "ratings_used": len(latest),
        "rule": "latest ts per (rater, style_key, variant, cohort, video); mean per subject; "
                "order by mean desc then subject; ties share averaged ranks",
        "cohorts": out_cohorts,
    }


def _plugin_relative(path: Path) -> str:
    """Path relative to the plugin root when it lies inside it, else the path as given."""
    try:
        return Path(path).resolve().relative_to(PLUGIN_ROOT).as_posix()
    except ValueError:
        return str(path)


def ingest(store: Path, out: Path, registry: dict[str, Any]) -> dict[str, Any]:
    _refuse_plugin_root(out)
    seed_user_store(store)
    rows = load_store(store, registry)
    if not rows:
        raise FeedbackError(f"store {store} has no ratings")
    cal = build_calibration(rows, store)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(cal, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    return cal


def export(rows: list[dict[str, Any]], fmt: str) -> str:
    if fmt == "json":
        return json.dumps(rows, indent=2, sort_keys=True, ensure_ascii=False)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(ROW_FIELDS))
    writer.writeheader()
    for row in rows:
        writer.writerow({k: (";".join(v) if isinstance(v, list) else v) for k, v in row.items()})
    return buf.getvalue()


def seed_rows(feedback_md: Path) -> list[dict[str, Any]]:
    """Build seed rows, quoting each cited line of the feedback file verbatim."""
    try:
        lines = feedback_md.read_text(encoding="utf-8").splitlines()
    except OSError as e:
        raise FeedbackError(f"cannot read {feedback_md}: {e}") from e
    rows = []
    for s in SEEDS:
        n = s["line"]
        if n > len(lines) or not lines[n - 1].startswith("> "):
            raise FeedbackError(f"{feedback_md.name}:{n} is not a quoted judgment line")
        rows.append({
            "style_key": s["style_key"], "variant": s["variant"], "cohort": s["cohort"],
            "video": s["video"], "rater": "owner", "score": s["score"], "score_origin": "mapped",
            "liked": s["liked"], "disliked": s["disliked"], "text": lines[n - 1][2:].strip(),
            "ts": SEED_TS, "source": f"{feedback_md.name}:{n}",
        })
    return rows


def _split_tags(values: list[str] | None) -> list[str]:
    return [t.strip() for v in values or [] for t in v.split(",") if t.strip()]


def _prompt_row(registry: dict[str, Any]) -> dict[str, Any]:
    styles = ", ".join(sorted(_registry_styles(registry)))

    def ask(label: str, default: str = "") -> str:
        got = input(f"{label}{f' [{default}]' if default else ''}: ").strip()
        return got or default

    score_txt = ask("score 1-10 (5 usable with fixes, 7 shippable, 9 matches reel)")
    try:
        score: float = float(score_txt)
    except ValueError as e:
        raise FeedbackError(f"score: not a number: {score_txt!r}") from e
    style_key = ask(f"style key ({styles})")
    return {
        "style_key": style_key,
        "variant": ask("variant (blank for none)") or None,
        "cohort": ask("cohort", GLOBAL),
        "video": ask("video path (relative to lookdev_root)"),
        "rater": ask("rater"),
        "score": score,
        "score_origin": "rater",
        "liked": _split_tags([ask("liked tags (comma separated)")]),
        "disliked": _split_tags([ask("disliked tags (comma separated)")]),
        "text": ask("free text"),
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": None,
    }


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--store", type=Path, default=None,
                        help=f"default: ${DATA_ENV}/feedback/{STORE_NAME} (reads fall back to the seed)")
    common.add_argument("--styles", type=Path, default=STYLES)
    c = sub.add_parser("collect", parents=[common], help="append one rating")
    c.add_argument("--interactive", action="store_true")
    c.add_argument("--style")
    c.add_argument("--variant")
    c.add_argument("--cohort", default=GLOBAL)
    c.add_argument("--video")
    c.add_argument("--rater")
    c.add_argument("--score", type=float)
    c.add_argument("--liked", action="append", help="tag; repeat or comma-separate")
    c.add_argument("--disliked", action="append", help="tag; repeat or comma-separate")
    c.add_argument("--text", default="")
    c.add_argument("--ts", help="ISO-8601 with offset; default now (UTC)")
    e = sub.add_parser("export", parents=[common], help="print all rows")
    e.add_argument("--format", choices=("json", "csv"), default="json")
    i = sub.add_parser("ingest", parents=[common], help="write the calibration set")
    i.add_argument("--out", type=Path, default=None,
                   help=f"default: ${DATA_ENV}/feedback/{CALIBRATION_NAME}")
    s = sub.add_parser("seed", parents=[common], help="append the owner's recorded judgments to an empty store")
    s.add_argument("--feedback-md", type=Path)
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        registry = load_registry(args.styles)
        if args.cmd == "collect":
            if args.interactive:
                row = _prompt_row(registry)
            else:
                needed = {"--style": args.style, "--video": args.video,
                          "--rater": args.rater, "--score": args.score}
                absent = [k for k, v in needed.items() if v is None]
                if absent:
                    raise FeedbackError(f"collect needs {', '.join(absent)} (or --interactive)")
                row = {
                    "style_key": args.style, "variant": args.variant, "cohort": args.cohort,
                    "video": args.video, "rater": args.rater, "score": args.score,
                    "score_origin": "rater", "liked": _split_tags(args.liked),
                    "disliked": _split_tags(args.disliked), "text": args.text,
                    "ts": args.ts or datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "source": None,
                }
            store = write_target(args.store, STORE_NAME, "--store")
            append_row(store, row, registry)
            print(json.dumps(row, sort_keys=True))
        elif args.cmd == "export":
            print(export(load_store(read_store(args.store), registry), args.format))
        elif args.cmd == "ingest":
            out = write_target(args.out, CALIBRATION_NAME, "--out")
            # A user store that does not exist yet is seeded by ingest(); with no
            # $CLAUDE_PLUGIN_DATA and no --store, the shipped seed is only read.
            store = args.store or _user_file(STORE_NAME) or SEED_STORE
            cal = ingest(store, out, registry)
            summary = {c: v["order"] for c, v in cal["cohorts"].items()}
            print(json.dumps({"out": str(out), "rows_read": cal["rows_read"], "order": summary}))
        elif args.cmd == "seed":
            store = write_target(args.store, STORE_NAME, "--store")
            if load_store(store, registry):
                raise FeedbackError(f"store {store} is not empty; seed only fills an empty store")
            md = args.feedback_md or studio_path(registry["__meta__"], "feedback_file")
            for row in seed_rows(md):
                append_row(store, row, registry, seed=False)
            print(json.dumps({"store": str(store), "seeded": len(SEEDS)}))
    except FeedbackError as e:
        print(f"feedback: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
