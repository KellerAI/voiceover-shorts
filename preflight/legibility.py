#!/usr/bin/env python3
"""Legibility preflight: minimum rendered type size and lower-third fill, from DOM boxes over time.

Usage:
  legibility.py PROJECT_DIR [--at T1,T2,...] [--samples N] [--json]
  legibility.py COMPOSITION.html [--at T1,T2,...] [--samples N] [--json]

Options: --min-px PX (default 44, at a 1080 px wide canvas; scaled by canvas width),
--opacity-floor A (default 0.2), --browser EXE (Chrome/Chromium executable; default
agent-browser's), --hyperframes-dist DIR (hyperframes dist/ with compiler-*.js; default: the
npx cache entry for hyperframes@0.8.183), --no-bundle (serve the HTML raw), --timeout-ms MS.

How it measures: a project dir is bundled with hyperframes' own compiler (bundleToSingleHtml,
the same step `render`/`inspect` run) so the hyperframes runtime controls clip visibility. The
page is served from 127.0.0.1 and loaded in headless Chrome through the agent-browser CLI
(Vercel; `agent-browser` on PATH), in a private session that is closed afterwards; requests to
any other host are blocked (--allowed-domains). At each sample time the page is seeked with
the same call chain hyperframes uses (__player.renderSeek, __hf.seek, __player.seek, __timelines pause+seek, then
gsap.ticker.tick) and every visible text run and media box is read from the DOM.

Rules, per sample time:
  small_type        a visible text run renders below min_px * width / 1080 (font-size times the
                    accumulated transform scale), after the opacity floor and overflow clipping.
  empty_lower_third no visible legible text (at or above the size floor), media
                    (img/video/canvas/svg, or a single SVG shape) or solid-background block
                    covers at least 0.25% of the
                    band y in [2H/3, H]. Boxes covering >= 90% of the canvas are backdrops and
                    do not count. If the band is empty but a full-bleed raster (canvas/video/img)
                    is visible, the sample is indeterminate (DOM cannot see inside the raster):
                    it is listed in metrics, not as a finding.
Sample times: --at when given, else N midpoints (i + 0.5) * duration / N, like hyperframes inspect.
Elements with data-legibility="ignore" (and their descendants) are skipped.
Exit: 0 pass, 1 fail, 2 usage/IO error.
"""

from __future__ import annotations

import argparse
import contextlib
import functools
import http.server
import json
import os
import shutil
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit

NAME = "legibility"
REF_WIDTH = 1080
DEFAULT_MIN_PX = 44.0
DEFAULT_OPACITY_FLOOR = 0.2
DEFAULT_SAMPLES = 9
FULL_BLEED_FRAC = 0.90
MIN_BAND_HIT_FRAC = (
    0.0025  # an item must cover >= 0.25% of the band (about 1728 px2 at 1080x1920)
)
HYPERFRAMES_VERSION = "0.8.183"
BUNDLE_PATH = "/__legibility_bundle.html"
MAX_EXAMPLES = 5
LOCAL_HOSTS = ("127.0.0.1", "localhost")
# Safety net: if close is never reached (killed process), the agent-browser daemon exits itself.
BROWSER_IDLE_TIMEOUT = "2m"
DEFAULT_EXECUTABLE = "agent-browser-default"

Json = dict[str, Any]

BUNDLE_JS = """
import { pathToFileURL } from "node:url";
import { readdirSync } from "node:fs";
import { join } from "node:path";
const [dist, proj] = process.argv.slice(1);
const f = readdirSync(dist).find((n) => /^compiler-[A-Za-z0-9_]+\\.js$/.test(n));
if (!f) { console.error(`no compiler-*.js in ${dist}`); process.exit(3); }
const mod = await import(pathToFileURL(join(dist, f)).href);
if (typeof mod.bundleToSingleHtml !== "function") { console.error("bundleToSingleHtml not exported"); process.exit(3); }
process.stdout.write(await mod.bundleToSingleHtml(proj));
"""

# Mirrors seekInPage + seekCompositionTimeline (hyperframes 0.8.183 dist/chunk-XOVHO3TT.js).
SEEK_JS = """
async (t) => {
  const get = (o, k) => (o !== null && (typeof o === "object" || typeof o === "function")) ? o[k] : undefined;
  const call = (fn, self, args) => { if (typeof fn !== "function") return false; fn.apply(self, args); return true; };
  const player = window.__player, hf = window.__hf, safe = Math.max(0, Number(t) || 0);
  if (call(get(player, "renderSeek"), player, [safe])) {}
  else if (call(get(hf, "seek"), hf, [safe])) {}
  else if (call(get(player, "seek"), player, [safe])) {}
  else {
    const tl = window.__timelines;
    if (tl && typeof tl === "object") for (const k of Object.keys(tl)) {
      call(get(tl[k], "pause"), tl[k], []); call(get(tl[k], "seek"), tl[k], [safe]);
    }
  }
  const ticker = get(window.gsap, "ticker"); call(get(ticker, "tick"), ticker, []);
  const wait = window.__hfWaitForSeekCompletion;
  if (typeof wait === "function") await wait.call(window, { videoFrames: true });
  await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
  if (document.fonts && document.fonts.status === "loading") {
    await Promise.race([document.fonts.ready, new Promise((r) => setTimeout(r, 500))]);
  }
}
"""

INFO_JS = """
() => {
  const root = document.querySelector("[data-composition-id][data-width][data-height]");
  const w = root ? parseInt(root.getAttribute("data-width"), 10) : 0;
  const h = root ? parseInt(root.getAttribute("data-height"), 10) : 0;
  const num = (v) => (typeof v === "function" ? v() : v);
  let d = 0;
  if (typeof (window.__hf || {}).duration === "number" && window.__hf.duration > 0) d = window.__hf.duration;
  if (!d && window.__player) { const pd = num(window.__player.duration); if (pd > 0) d = pd; }
  if (!d) { const r = document.querySelector("[data-composition-id][data-duration]");
    const a = r ? parseFloat(r.getAttribute("data-duration")) : 0; if (a > 0) d = a; }
  if (!d && window.__timelines) for (const tl of Object.values(window.__timelines)) {
    const td = num(tl && tl.duration); if (td > 0) { d = td; break; } }
  return { width: w > 0 ? w : null, height: h > 0 ? h : null, duration: d };
}
"""

# Full browser version (e.g. 156.0.8078.49); the UA string only carries the major version.
VERSION_JS = """
async () => {
  const ua = navigator.userAgentData;
  if (ua && typeof ua.getHighEntropyValues === "function") {
    const v = await ua.getHighEntropyValues(["fullVersionList"]);
    const c = (v.fullVersionList || []).find((b) => b.brand === "Chromium");
    if (c) return c.version;
  }
  const m = navigator.userAgent.match(/(?:HeadlessChrome|Chrome)\\/([0-9.]+)/);
  return m ? m[1] : navigator.userAgent;
}
"""

# Reads every visible text run and content box at the current seek position.
MEASURE_JS = """
() => {
  const alpha = (c) => { const m = (c || "").match(/rgba?\\(([^)]+)\\)/);
    if (!m) return (!c || c === "transparent" || c === "none") ? 0 : 1;
    const p = m[1].split(/[ ,\\/]+/).filter(Boolean); return p.length >= 4 ? parseFloat(p[3]) : 1; };
  const ignored = (el) => !!(el.closest && el.closest('[data-legibility="ignore"]'));
  const opacityOf = (el) => { let o = 1; for (let a = el; a; a = a.parentElement) o *= parseFloat(getComputedStyle(a).opacity) || 0; return o; };
  const scaleOf = (el) => {
    if (el instanceof SVGElement && typeof el.getScreenCTM === "function") {
      const m = el.getScreenCTM(); if (m) return Math.sqrt(Math.abs(m.a * m.d - m.b * m.c));
    }
    let s = 1;
    for (let a = el; a; a = a.parentElement) {
      const cs = getComputedStyle(a);
      if (cs.transform && cs.transform !== "none") { const m = new DOMMatrixReadOnly(cs.transform); s *= Math.sqrt(Math.abs(m.a * m.d - m.b * m.c)); }
      if (cs.scale && cs.scale !== "none") { const p = cs.scale.split(" ").map(parseFloat); s *= Math.sqrt(Math.abs(p[0] * (p.length > 1 ? p[1] : p[0]))); }
      const z = parseFloat(cs.zoom); if (z > 0) s *= z;
    }
    return s;
  };
  const clipRect = (el, r) => {
    let x0 = r.left, y0 = r.top, x1 = r.right, y1 = r.bottom;
    for (let a = el.parentElement; a && a !== document.documentElement; a = a.parentElement) {
      const cs = getComputedStyle(a);
      if (cs.overflowX !== "visible" || cs.overflowY !== "visible" || (cs.clipPath && cs.clipPath !== "none")) {
        const c = a.getBoundingClientRect();
        x0 = Math.max(x0, c.left); y0 = Math.max(y0, c.top); x1 = Math.min(x1, c.right); y1 = Math.min(y1, c.bottom);
      }
    }
    return [x0, y0, Math.max(0, x1 - x0), Math.max(0, y1 - y0)];
  };
  const shown = (el) => (typeof el.checkVisibility !== "function") || el.checkVisibility({ visibilityProperty: true });
  const texts = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    const txt = n.nodeValue.replace(/\\s+/g, " ").trim(); if (!txt) continue;
    const el = n.parentElement; if (!el || ignored(el) || !shown(el)) continue;
    if (["SCRIPT", "STYLE", "NOSCRIPT", "TITLE", "TEMPLATE"].includes(el.tagName)) continue;
    const cs = getComputedStyle(el);
    const rg = document.createRange(); rg.selectNodeContents(n);
    const r = rg.getBoundingClientRect(); if (r.width < 1 || r.height < 1) continue;
    let ink = alpha(cs.color);
    if (parseFloat(cs.webkitTextStrokeWidth) > 0) ink = Math.max(ink, alpha(cs.webkitTextStrokeColor));
    if ((cs.webkitBackgroundClip === "text" || cs.backgroundClip === "text") && cs.backgroundImage !== "none") ink = 1;
    const font = parseFloat(cs.fontSize) || 0, scale = scaleOf(el);
    texts.push({ text: txt.slice(0, 40), font_px: font, scale: scale, rendered_px: font * scale,
                 ink: ink * opacityOf(el), box: clipRect(el, r) });
  }
  const boxes = [];
  for (const el of document.body.querySelectorAll("*")) {
    if (ignored(el) || !shown(el)) continue;
    const tag = el.tagName.toLowerCase();
    let kind = null, fill = 1;
    if (["img", "video", "canvas"].includes(tag)) kind = "raster";
    else if (tag === "svg" && !(el.parentElement && el.parentElement.closest("svg"))) kind = "svg";
    else if (["path", "rect", "circle", "ellipse", "polygon", "polyline", "line", "image", "use"].includes(tag)
             && el instanceof SVGElement) {
      // SVG shapes count on their own, so a full-bleed SVG overlay (a backdrop as a whole)
      // still fills the band where it actually draws.
      const cs = getComputedStyle(el);
      const fillA = tag === "image" ? 1 : alpha(cs.fill);
      let strokeA = parseFloat(cs.strokeWidth) > 0 ? alpha(cs.stroke) : 0;
      // Draw-on strokes (one dash >= the path length, offset animated from it to 0) are invisible
      // until the offset drops: scale by the drawn fraction so undrawn paths do not count.
      if (strokeA > 0 && cs.strokeDasharray && cs.strokeDasharray !== "none" && typeof el.getTotalLength === "function") {
        const dash = parseFloat(cs.strokeDasharray), len = el.getTotalLength();
        if (len > 0 && dash >= len * 0.99) {
          const drawn = Math.max(0, Math.min(1, (dash - Math.abs(parseFloat(cs.strokeDashoffset) || 0)) / len));
          if (drawn < 0.05) strokeA = 0;
        }
      }
      fill = Math.max(fillA, strokeA);
      kind = "svg";
    } else if (!(el instanceof SVGElement)) {
      const cs = getComputedStyle(el);
      fill = Math.max(alpha(cs.backgroundColor), parseFloat(cs.borderTopWidth) > 0 ? alpha(cs.borderTopColor) : 0);
      if (fill > 0) kind = "block";
    }
    if (!kind) continue;
    const r = el.getBoundingClientRect(); if (r.width < 1 || r.height < 1) continue;
    boxes.push({ tag: tag, kind: kind, ink: fill * opacityOf(el), box: clipRect(el, r) });
  }
  return { texts: texts, boxes: boxes };
}
"""


class LegibilityError(Exception):
    pass


# ---------- pure helpers (browser-free) ----------


def scaled_min_px(min_px: float, width: int) -> float:
    """Threshold for a canvas `width` px wide, given `min_px` at REF_WIDTH."""
    return min_px * width / REF_WIDTH


def sample_times(duration: float, samples: int, at: list[float] | None) -> list[float]:
    if at:
        return sorted({round(float(t), 3) for t in at})
    if duration <= 0 or samples <= 0:
        return [0.0]
    return [round((i + 0.5) * duration / samples, 3) for i in range(samples)]


def visible_area(box: list[float], width: int, height: int) -> float:
    x, y, w, h = box
    iw = max(0.0, min(x + w, width) - max(x, 0.0))
    ih = max(0.0, min(y + h, height) - max(y, 0.0))
    return iw * ih


def band_area(box: list[float], width: int, height: int) -> float:
    """Area of `box` inside the lower-third band y in [2H/3, H], x in [0, W]."""
    x, y, w, h = box
    top = height * 2.0 / 3.0
    iw = max(0.0, min(x + w, width) - max(x, 0.0))
    ih = max(0.0, min(y + h, height) - max(y, top))
    return iw * ih


def is_full_bleed(box: list[float], width: int, height: int) -> bool:
    return visible_area(box, width, height) >= FULL_BLEED_FRAC * width * height


def classify_sample(
    sample: Json, width: int, height: int, threshold: float, opacity_floor: float
) -> Json:
    """Classify one sample's raw DOM measurement into small text runs and a lower-third status."""
    texts = [
        t
        for t in sample["texts"]
        if t["ink"] >= opacity_floor and visible_area(t["box"], width, height) >= 1.0
    ]
    small = sorted(
        (t for t in texts if t["rendered_px"] < threshold),
        key=lambda t: t["rendered_px"],
    )
    min_hit = MIN_BAND_HIT_FRAC * width * (height / 3.0)
    # Only legible text fills the band: a footer label below the size floor does not.
    filled = any(
        band_area(t["box"], width, height) >= min_hit
        for t in texts
        if t["rendered_px"] >= threshold
    )
    boxes = [b for b in sample["boxes"] if b["ink"] >= opacity_floor]
    full_raster = False
    for b in boxes:
        if is_full_bleed(b["box"], width, height):
            full_raster = full_raster or b["kind"] == "raster"
        elif band_area(b["box"], width, height) >= min_hit:
            filled = True
    status = "filled" if filled else ("indeterminate" if full_raster else "empty")
    return {
        "texts": texts,
        "small": small,
        "lower_third": status,
        "min_rendered_px": min((t["rendered_px"] for t in texts), default=None),
    }


def _fmt_run(t: Json) -> str:
    x, y = t["box"][0], t["box"][1]
    return (
        f"{t['text']!r} {t['rendered_px']:.1f}px (font {t['font_px']:.1f}px x{t['scale']:.2f})"
        f" at ({x:.0f},{y:.0f})"
    )


def evaluate(
    raw: list[Json], width: int, height: int, min_px: float, opacity_floor: float
) -> tuple[list[Json], Json]:
    """Turn per-sample raw measurements ({t, texts, boxes}) into findings and metrics."""
    threshold = scaled_min_px(min_px, width)
    findings: list[Json] = []
    per_sample = []
    empty, indeterminate, small_samples = [], [], []
    mins: list[float] = []
    runs = 0
    for s in raw:
        t = s["t"]
        c = classify_sample(s, width, height, threshold, opacity_floor)
        runs += len(c["texts"])
        if c["min_rendered_px"] is not None:
            mins.append(c["min_rendered_px"])
        if c["small"]:
            small_samples.append(t)
            ex = "; ".join(_fmt_run(r) for r in c["small"][:MAX_EXAMPLES])
            more = len(c["small"]) - MAX_EXAMPLES
            findings.append(
                {
                    "t_start": t,
                    "t_end": t,
                    "kind": "small_type",
                    "detail": f"{len(c['small'])} visible text run(s) below {threshold:.1f}px: {ex}"
                    + (f"; +{more} more" if more > 0 else ""),
                }
            )
        if c["lower_third"] == "empty":
            empty.append(t)
            findings.append(
                {
                    "t_start": t,
                    "t_end": t,
                    "kind": "empty_lower_third",
                    "detail": f"lower third y {height * 2 / 3:.0f}-{height} is empty: no legible text"
                    f" (>= {threshold:.1f}px), media, SVG shape or solid block covers"
                    f" >= {MIN_BAND_HIT_FRAC:.2%} of it",
                }
            )
        elif c["lower_third"] == "indeterminate":
            indeterminate.append(t)
        per_sample.append(
            {
                "t": t,
                "text_runs": len(c["texts"]),
                "small_runs": len(c["small"]),
                "min_rendered_px": None
                if c["min_rendered_px"] is None
                else round(c["min_rendered_px"], 2),
                "lower_third": c["lower_third"],
            }
        )
    metrics = {
        "width": width,
        "height": height,
        "ref_width": REF_WIDTH,
        "min_px": min_px,
        "min_px_scaled": round(threshold, 2),
        "opacity_floor": opacity_floor,
        "samples": [s["t"] for s in raw],
        "text_runs_measured": runs,
        "min_rendered_px_seen": round(min(mins), 2) if mins else None,
        "small_type_samples": small_samples,
        "empty_lower_third_samples": empty,
        "indeterminate_lower_third_samples": indeterminate,
        "per_sample": per_sample,
    }
    return findings, metrics


# ---------- browser path ----------


def find_hyperframes_dist(version: str = HYPERFRAMES_VERSION) -> Path | None:
    """Locate node_modules/hyperframes/dist for `version` in the npx cache (~/.npm/_npx/*)."""
    for pkg in sorted(
        (Path.home() / ".npm" / "_npx").glob("*/node_modules/hyperframes/package.json")
    ):
        try:
            if json.loads(pkg.read_text()).get("version") == version:
                return pkg.parent / "dist"
        except (OSError, ValueError):
            continue
    return None


def bundle_project(dist: Path, project_dir: Path) -> str:
    node = shutil.which("node")
    if not node:
        raise LegibilityError(
            "node not found on PATH (needed to run the hyperframes bundler)"
        )
    r = subprocess.run(
        [node, "--input-type=module", "-e", BUNDLE_JS, str(dist), str(project_dir)],
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    if r.returncode != 0 or not r.stdout.strip():
        raise LegibilityError(
            f"hyperframes bundle failed (exit {r.returncode}): {r.stderr.strip()[-400:]}"
        )
    return r.stdout


@contextlib.contextmanager
def serve(root: Path, bundle: bytes | None) -> Iterator[str]:
    """Serve `root` on 127.0.0.1; `bundle` (if any) is served at BUNDLE_PATH. Yields the base URL."""

    class Handler(http.server.SimpleHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 (stdlib name)
            if bundle is not None and urlsplit(self.path).path == BUNDLE_PATH:
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(bundle)))
                self.end_headers()
                self.wfile.write(bundle)
                return
            super().do_GET()

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            pass

    class Server(http.server.ThreadingHTTPServer):
        daemon_threads = True

        def handle_error(self, request: Any, client_address: Any) -> None:
            # The browser drops media downloads it no longer needs; that is not an error.
            if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError)):
                return
            super().handle_error(request, client_address)

    srv = Server(("127.0.0.1", 0), functools.partial(Handler, directory=str(root)))
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()


class BrowserError(LegibilityError):
    """An agent-browser command failed (non-zero exit or success: false)."""


class AgentBrowser:
    """One private agent-browser session (headless Chrome); each call is one CLI command.

    The session name is unique per instance, so the machine-wide default session is never touched.
    Every command but close repeats the same launch flags, so the session never relaunches with
    other ones.
    """

    def __init__(self, exe: str, browser_exe: str | None, timeout_ms: int) -> None:
        self.timeout_ms = timeout_ms
        self.session = [exe, "--session", f"legibility-{uuid.uuid4().hex[:12]}"]
        self.base = [
            *self.session,
            "--allowed-domains",
            ",".join(LOCAL_HOSTS),
            "--headed",
            "false",
            "--idle-timeout",
            BROWSER_IDLE_TIMEOUT,
            "--json",
        ]
        if browser_exe:
            self.base += ["--executable-path", browser_exe]
        self.env = {**os.environ, "AGENT_BROWSER_DEFAULT_TIMEOUT": str(timeout_ms)}

    def run(
        self, *args: str, stdin: str | None = None, launch_flags: bool = True
    ) -> Json:
        """Run one command; return its `data` object, or raise BrowserError."""
        cmd = [*self.base, *args] if launch_flags else [*self.session, "--json", *args]
        try:
            r = subprocess.run(
                cmd,
                input=stdin,
                capture_output=True,
                text=True,
                check=False,
                env=self.env,
                timeout=self.timeout_ms / 1000 + 60,
            )
        except subprocess.TimeoutExpired as e:
            raise BrowserError(f"browser error: agent-browser {args[0]} hung") from e
        try:
            out = json.loads(r.stdout)
        except ValueError:
            out = None
        if isinstance(out, dict) and out.get("success"):
            data = out.get("data")
            return data if isinstance(data, dict) else {}
        msg = out.get("error") if isinstance(out, dict) else None
        msg = str(msg or r.stderr.strip() or r.stdout.strip() or f"exit {r.returncode}")
        raise BrowserError(
            f"browser error: agent-browser {args[0]}: {msg.splitlines()[0]}"
        )

    def run_js(self, js: str, *args: Any) -> Any:
        """Evaluate the function source `js` with JSON-encoded `args`; promises are awaited."""
        call = f"({js.strip()})({', '.join(json.dumps(a) for a in args)})"
        return self.run("eval", "--stdin", stdin=call).get("result")

    def goto(self, url: str) -> None:
        self.run("open", url)
        self.run("wait", "--load", "load", "--timeout", str(self.timeout_ms))

    def close(self) -> None:
        # Without launch flags: with them, close relaunches a browser that failed to launch
        # (bad --browser path) and fails, leaving the session daemon running.
        with contextlib.suppress(LegibilityError, OSError):
            self.run("close", launch_flags=False)


def measure(
    url: str,
    samples: int,
    at: list[float] | None,
    expect_runtime: bool,
    browser_exe: str | None,
    timeout_ms: int,
) -> Json:
    """Load `url` in headless Chrome, seek to each sample time, return raw DOM measurements."""
    exe = shutil.which("agent-browser")
    if not exe:
        raise LegibilityError(
            "agent-browser not found on PATH "
            "(install: npm install -g agent-browser && agent-browser install)"
        )
    b = AgentBrowser(exe, browser_exe, timeout_ms)
    try:
        # Launch on about:blank, so the viewport is set before the first load.
        b.run("open")
        b.run("set", "viewport", str(REF_WIDTH), "1920")
        b.goto(url)
        info = b.run_js(INFO_JS)
        if not info["width"] or not info["height"]:
            raise LegibilityError(
                "no [data-composition-id][data-width][data-height] root in the composition"
            )
        if (info["width"], info["height"]) != (REF_WIDTH, 1920):
            b.run("set", "viewport", str(info["width"]), str(info["height"]))
            b.goto(url)
        if expect_runtime:
            try:
                b.run(
                    "wait",
                    "--fn",
                    "Boolean(window.__renderReady) || typeof window.__hfStartupError === 'string'",
                    "--timeout",
                    str(timeout_ms),
                )
            except BrowserError as e:
                raise LegibilityError(
                    f"hyperframes runtime not ready within {timeout_ms} ms"
                ) from e
            err = b.run_js("() => window.__hfStartupError")
            if isinstance(err, str):
                raise LegibilityError(f"hyperframes runtime startup error: {err}")
        b.run_js("() => document.fonts ? document.fonts.ready.then(() => true) : true")
        info = b.run_js(INFO_JS)
        ts = sample_times(float(info["duration"] or 0), samples, at)
        raw = []
        for t in ts:
            b.run_js(SEEK_JS, t)
            m = b.run_js(MEASURE_JS)
            raw.append({"t": t, "texts": m["texts"], "boxes": m["boxes"]})
        errors = [str(e.get("text", e)) for e in b.run("errors").get("errors", [])]
        blocked = [
            q["url"]
            for q in b.run("network", "requests").get("requests", [])
            if urlsplit(q.get("url", "")).scheme in ("http", "https", "ws", "wss")
            and urlsplit(q["url"]).hostname not in LOCAL_HOSTS
        ]
        return {
            "info": info,
            "raw": raw,
            "browser": b.run_js(VERSION_JS),
            "blocked_requests": blocked,
            "page_errors": errors,
        }
    finally:
        b.close()


def check(
    path: str,
    at: list[float] | None = None,
    samples: int = DEFAULT_SAMPLES,
    min_px: float = DEFAULT_MIN_PX,
    opacity_floor: float = DEFAULT_OPACITY_FLOOR,
    browser: str | None = None,
    hyperframes_dist: str | None = None,
    bundle: bool = True,
    timeout_ms: int = 15000,
) -> Json:
    """Run the legibility gate on a hyperframes project dir or a composition HTML file."""
    src = Path(path).expanduser().resolve()
    if samples < 1:
        raise LegibilityError("--samples must be >= 1")
    if src.is_dir():
        entry = src / "index.html"
        if not entry.is_file():
            raise LegibilityError(f"no index.html in project dir: {src}")
    elif src.is_file() and src.suffix.lower() in (".html", ".htm"):
        entry = src
    else:
        raise LegibilityError(f"not a project dir or .html file: {src}")
    html_bytes: bytes | None = None
    bundled = False
    dist_used = None
    if src.is_dir() and bundle:
        dist = (
            Path(hyperframes_dist).expanduser()
            if hyperframes_dist
            else find_hyperframes_dist()
        )
        if dist is None or not dist.is_dir():
            raise LegibilityError(
                f"hyperframes@{HYPERFRAMES_VERSION} dist not found in ~/.npm/_npx; "
                "run `npx hyperframes@0.8.183 --help` once, or pass --hyperframes-dist "
                "or --no-bundle"
            )
        html_bytes = bundle_project(dist, src).encode("utf-8")
        bundled, dist_used = True, str(dist)
    root = entry.parent
    with serve(root, html_bytes) as base:
        url = base + (BUNDLE_PATH if bundled else "/" + entry.name)
        m = measure(url, samples, at, bundled, browser, timeout_ms)
    info = m["info"]
    findings, metrics = evaluate(
        m["raw"], int(info["width"]), int(info["height"]), min_px, opacity_floor
    )
    metrics.update(
        {
            "input": str(src),
            "duration": info["duration"],
            "bundled": bundled,
            "hyperframes_dist": dist_used,
            "browser": m["browser"],
            "browser_executable": browser or DEFAULT_EXECUTABLE,
            "blocked_requests": len(m["blocked_requests"]),
            "page_errors": m["page_errors"][:5],
        }
    )
    return {
        "name": NAME,
        "passed": not findings,
        "findings": findings,
        "metrics": metrics,
    }


def _times(s: str) -> list[float]:
    try:
        out = [float(x) for x in s.split(",") if x.strip()]
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"bad --at list: {s}") from e
    if not out or any(t < 0 for t in out):
        raise argparse.ArgumentTypeError(f"--at needs non-negative seconds: {s}")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "input",
        help="hyperframes project dir (bundled) or composition .html (served raw)",
    )
    ap.add_argument("--at", type=_times, help="comma-separated sample times in seconds")
    ap.add_argument(
        "--samples",
        type=int,
        default=DEFAULT_SAMPLES,
        help="midpoint samples when --at is absent",
    )
    ap.add_argument(
        "--min-px",
        type=float,
        default=DEFAULT_MIN_PX,
        help="min rendered type px at 1080 wide",
    )
    ap.add_argument("--opacity-floor", type=float, default=DEFAULT_OPACITY_FLOOR)
    ap.add_argument(
        "--browser",
        help="Chrome/Chromium executable (default: the one agent-browser installed)",
    )
    ap.add_argument(
        "--hyperframes-dist", help="hyperframes dist/ dir holding compiler-*.js"
    )
    ap.add_argument(
        "--no-bundle",
        action="store_true",
        help="serve a project's index.html without bundling",
    )
    ap.add_argument("--timeout-ms", type=int, default=15000)
    ap.add_argument("--json", action="store_true")
    try:
        a = ap.parse_args(argv)
    except SystemExit as e:
        return 0 if e.code == 0 else 2
    try:
        res = check(
            a.input,
            at=a.at,
            samples=a.samples,
            min_px=a.min_px,
            opacity_floor=a.opacity_floor,
            browser=a.browser,
            hyperframes_dist=a.hyperframes_dist,
            bundle=not a.no_bundle,
            timeout_ms=a.timeout_ms,
        )
    except (LegibilityError, OSError, subprocess.TimeoutExpired) as e:
        print(f"legibility: error: {e}", file=sys.stderr)
        return 2
    if a.json:
        print(json.dumps(res, indent=2))
    else:
        print(
            ("PASS" if res["passed"] else "FAIL"),
            json.dumps(
                {
                    k: res["metrics"][k]
                    for k in (
                        "min_px_scaled",
                        "min_rendered_px_seen",
                        "small_type_samples",
                        "empty_lower_third_samples",
                    )
                }
            ),
        )
        for f in res["findings"]:
            print(f"  t={f['t_start']}: {f['kind']}: {f['detail']}")
    return 0 if res["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
