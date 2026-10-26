#!/usr/bin/env python3
"""Build a self-contained side-by-side HTML review page whose rating inputs export feedback rows.

  build_review_page.py build --render STYLE[/VARIANT]=VIDEO ... --out PAGE.html
                             [--cohort C; default K-variants for K/<variant>, else global] [--title T] [--styles P]
  build_review_page.py submit EXPORT.json|- [--store P; default $CLAUDE_PLUGIN_DATA/feedback/ratings.jsonl] [--styles P]

VIDEO is a path relative to the registry's lookdev_root (the `video` field of the feedback-row schema).
The page is one HTML file (inline CSS and JS, no network); the videos are file:// links to the
renders. "Export JSON" yields a JSON array of rows in the feedback-row schema. `submit` validates each
row with feedback.validate_row and appends it to the store via feedback.append_row, all or nothing.
Exit codes: 0 ok, 2 usage/IO error or a row that fails validation (no pass/fail state).
"""
from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT))
from feedback import feedback  # noqa: E402

RUBRIC_SLUGS = ("busy_frame", "pacing", "alignment", "captions", "polish")

# Pure logic, no DOM access; the test runs this exact block under node.
LOGIC_JS = r"""
function buildRows(config, inputs, rater, ts) {
  rater = (rater || "").trim();
  if (!rater) throw new Error("rater is required");
  const tags = s => (s || "").split(",").map(t => t.trim()).filter(t => t.length > 0);
  const rows = [];
  config.items.forEach((item, i) => {
    const inp = inputs[i] || {};
    const raw = (inp.score === undefined || inp.score === null) ? "" : String(inp.score).trim();
    if (raw === "") return;
    const score = Number(raw);
    if (!Number.isFinite(score) || score < 1 || score > 10)
      throw new Error(item.label + ": score must be a number in 1-10");
    rows.push({
      style_key: item.style_key, variant: item.variant, cohort: item.cohort,
      video: item.video, rater: rater, score: score, score_origin: "rater",
      liked: tags(inp.liked), disliked: tags(inp.disliked), text: inp.text || "",
      ts: ts, source: null
    });
  });
  if (rows.length === 0) throw new Error("no ratings entered");
  return rows;
}
"""

UI_JS = r"""
const config = JSON.parse(document.getElementById("config").textContent);
const $ = id => document.getElementById(id);
function collect() {
  return config.items.map((_, i) => ({
    score: $("score" + i).value, liked: $("liked" + i).value,
    disliked: $("disliked" + i).value, text: $("text" + i).value }));
}
function nowIso() {
  const d = new Date(), p = n => String(n).padStart(2, "0"), o = -d.getTimezoneOffset();
  const a = Math.abs(o);
  return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate()) + "T" + p(d.getHours()) +
    ":" + p(d.getMinutes()) + ":" + p(d.getSeconds()) + (o < 0 ? "-" : "+") + p(Math.floor(a / 60)) +
    ":" + p(a % 60);
}
$("export").onclick = () => {
  try {
    const rows = buildRows(config, collect(), $("rater").value, nowIso());
    $("out").value = JSON.stringify(rows, null, 2);
    $("msg").textContent = rows.length + " row(s) ready. Save as a .json file and run: " +
      "build_review_page.py submit FILE.json";
  } catch (e) { $("out").value = ""; $("msg").textContent = "Error: " + e.message; }
};
$("download").onclick = () => {
  if (!$("out").value) { $("export").click(); }
  if (!$("out").value) return;
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([$("out").value], {type: "application/json"}));
  a.download = "ratings-" + ($("rater").value.trim() || "rater") + ".json";
  a.click();
};
$("playall").onclick = () => document.querySelectorAll("video").forEach(v => { v.currentTime = 0; v.play(); });
$("pauseall").onclick = () => document.querySelectorAll("video").forEach(v => v.pause());
"""

CSS = """
body{font:14px system-ui,sans-serif;margin:16px;background:#111;color:#eee}
h1{font-size:18px}.bar{display:flex;gap:8px;align-items:center;margin-bottom:12px;flex-wrap:wrap}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px}
.card{background:#1c1c1c;border-radius:6px;padding:8px}.card video{width:100%;background:#000}
.card label{display:block;margin-top:6px;font-size:12px;color:#aaa}
.card input,.card textarea,.bar input{width:100%;box-sizing:border-box;background:#222;color:#eee;border:1px solid #444;padding:4px}
.bar input{width:auto}#out{width:100%;height:160px;margin-top:8px;background:#222;color:#eee;font-family:monospace}
button{padding:6px 10px}
"""


def parse_render(spec: str, cohort: str | None, registry: dict[str, Any]) -> dict[str, Any]:
    """STYLE[/VARIANT]=VIDEO -> one page item; reject anything the feedback-row schema would reject."""
    if "=" not in spec:
        raise feedback.FeedbackError(f"--render {spec!r}: expected STYLE[/VARIANT]=VIDEO")
    left, video = spec.split("=", 1)
    style, _, variant = left.partition("/")
    if cohort is None:  # K variants rank only within their own cohort
        cohort = "K-variants" if style == "K" and variant else feedback.GLOBAL
    row = {"style_key": style, "variant": variant or None, "cohort": cohort, "video": video,
          "rater": "x", "score": 1, "score_origin": "rater", "liked": [], "disliked": [],
          "text": "", "ts": "2000-01-01T00:00:00+00:00", "source": None}
    try:
        feedback.validate_row(row, registry)
    except feedback.FeedbackError as e:
        raise feedback.FeedbackError(f"--render {spec!r}: {e}") from e
    root = feedback.studio_path(registry["__meta__"], "lookdev_root")
    return {"style_key": style, "variant": variant or None, "cohort": cohort, "video": video,
            "label": variant or style, "src": (root / video).as_uri()}


def render_page(items: list[dict[str, Any]], title: str) -> str:
    config = json.dumps({"items": [{k: v for k, v in it.items() if k != "src"} for it in items]})
    config = config.replace("</", "<\\/")
    cards = []
    for i, it in enumerate(items):
        cards.append(
            f'<div class="card"><b>{html.escape(it["label"])}</b> '
            f'<small>{html.escape(it["video"])}</small>'
            f'<video controls preload="metadata" src="{html.escape(it["src"], quote=True)}"></video>'
            f'<label>Score 1-10<input id="score{i}" type="number" min="1" max="10" step="0.5"></label>'
            f'<label>Liked tags (comma-separated)<input id="liked{i}" list="slugs"></label>'
            f'<label>Disliked tags (comma-separated)<input id="disliked{i}" list="slugs"></label>'
            f'<label>Notes<textarea id="text{i}" rows="2"></textarea></label></div>')
    slugs = "".join(f'<option value="{s}">' for s in RUBRIC_SLUGS)
    return (
        f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{html.escape(title)}</title>'
        f"<style>{CSS}</style></head><body><h1>{html.escape(title)}</h1>"
        '<div class="bar"><label>Rater <input id="rater" placeholder="your name"></label>'
        '<button id="playall">Play all</button><button id="pauseall">Pause all</button>'
        '<button id="export">Export JSON</button><button id="download">Download</button></div>'
        f'<datalist id="slugs">{slugs}</datalist><div class="grid">{"".join(cards)}</div>'
        '<p id="msg"></p><textarea id="out" readonly></textarea>'
        f'<script type="application/json" id="config">{config}</script>'
        f'<script id="logic">{LOGIC_JS}</script><script id="ui">{UI_JS}</script></body></html>')


def cmd_build(args: argparse.Namespace) -> int:
    registry = feedback.load_registry(args.styles)
    items = [parse_render(s, args.cohort, registry) for s in args.render]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render_page(items, args.title), encoding="utf-8")
    print(json.dumps({"out": str(args.out), "items": len(items)}))
    return 0


def cmd_submit(args: argparse.Namespace) -> int:
    registry = feedback.load_registry(args.styles)
    try:
        text = sys.stdin.read() if str(args.file) == "-" else args.file.read_text(encoding="utf-8")
        rows = json.loads(text)
    except (OSError, json.JSONDecodeError) as e:
        raise feedback.FeedbackError(f"cannot read export {args.file}: {e}") from e
    if not isinstance(rows, list) or not rows:
        raise feedback.FeedbackError("export must be a non-empty JSON array of rows")
    for n, row in enumerate(rows, 1):  # validate everything first: all or nothing
        try:
            feedback.validate_row(row, registry)
        except feedback.FeedbackError as e:
            raise feedback.FeedbackError(f"row {n}: {e}") from e
    store = feedback.write_target(args.store, feedback.STORE_NAME, "--store")
    for row in rows:
        feedback.append_row(store, row, registry)
    print(json.dumps({"store": str(store), "appended": len(rows)}))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--styles", type=Path, default=feedback.STYLES)
    b = sub.add_parser("build", parents=[common], help="write the review page")
    b.add_argument("--render", action="append", required=True, metavar="STYLE[/VARIANT]=VIDEO")
    b.add_argument("--out", type=Path, required=True)
    b.add_argument("--cohort", default=None,
                   help="default: K-variants for K/<variant>, else global")
    b.add_argument("--title", default="Shorts review")
    s = sub.add_parser("submit", parents=[common], help="validate an exported JSON and append it")
    s.add_argument("file", type=Path)
    s.add_argument("--store", type=Path, default=None,
                   help=f"default: ${feedback.DATA_ENV}/feedback/{feedback.STORE_NAME}")
    args = p.parse_args(argv)
    try:
        return cmd_build(args) if args.cmd == "build" else cmd_submit(args)
    except feedback.FeedbackError as e:
        print(f"build_review_page: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
