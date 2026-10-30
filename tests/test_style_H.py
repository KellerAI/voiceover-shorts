"""Style H template (styles/H): instantiate with the S49 15 s inputs, render for real, gate the
render with every preflight (items 3-6) and the item 7 report.

Real runs only: build.py (ffmpeg + kit/sfx_bus), a sandboxed offline HyperFrames render, and the
preflight CLIs as subprocesses. Renders and scratch go under a pytest tmp dir; the S49 inputs and
the canonical dir come from SHORTS_STUDIO_DIR (tests skip without it). The render is reused within
one pytest session when index.html, the audio and the kit end card are unchanged (keyed by a hash); set STYLE_H_FORCE_RENDER=1 to force a fresh one.

Style H draws everything on one <canvas>, which the DOM-based legibility preflight cannot see
into (it reports the lower third as "indeterminate", not as a finding). Two extra checks close
that gap: the canvas records the size of every text run it draws (window.__hText, read here in
headless Chrome (agent-browser) over the full timeline at 10 fps), and the lower band of the rendered frames
is measured for ink. Each has a planted negative.
"""
import functools
import hashlib
import http.server
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PLUGIN = Path(__file__).resolve().parents[1]
STYLE = PLUGIN / "styles" / "H"
BUILD_PY = STYLE / "build.py"
PRE = PLUGIN / "preflight"
EVIDENCE = PLUGIN / "evidence" / "evidence.py"

_STUDIO_DIR = os.environ.get("SHORTS_STUDIO_DIR")
STUDIO = Path(_STUDIO_DIR) if _STUDIO_DIR else None
if STUDIO is not None:
    INPUTS = STUDIO / "_kit/lookdev/inputs"
    WAV = INPUTS / "r6-S49-cut15.wav"
    WORDS = INPUTS / "r6-S49-cut15.words.json"
    CANON = STUDIO / "_kit/lookdev/r2-15s-H"
else:
    INPUTS = WAV = WORDS = CANON = None
STUDIO_REQUIRED = (WAV, WORDS, CANON)
SANDBOX = PLUGIN / "kit" / "offline.sb"
OUT = INST = VIDEO = RESULTS = None  # scratch, bound per module by _scratch()
PORT = 4170  # preview port reserved for this item


@pytest.fixture(scope="module", autouse=True)
def _scratch(tmp_path_factory):
    global OUT, INST, VIDEO, RESULTS
    OUT = tmp_path_factory.mktemp("style-H")
    INST = OUT / "s49-cut15"
    VIDEO = INST / "look.mp4"
    RESULTS = INST / "preflight"


UNLICENSED = ("Comic Sans", "Marker Felt", "Chalkduster", "SF Pro", "SF Mono", "Papyrus", "Menlo", "Avenir")


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def run(cmd, **kw):
    return subprocess.run([str(c) for c in cmd], capture_output=True, text=True, **kw)


CANON_FILES = [] if CANON is None else [CANON / "project/index.html", CANON / "project/kit/gsap.min.js", CANON / "project/assets/narration.wav",
               CANON / "project/assets/ui_sfx.wav", CANON / "sfx_gen.py", CANON / "notes.md", CANON / "look.mp4"]


@pytest.fixture(scope="module")
def built():
    before = {str(p): sha(p) for p in CANON_FILES}
    r = run([sys.executable, BUILD_PY, "--wav", WAV, "--words", WORDS, "--out", INST, "--slug", "S49", "--json"])
    assert r.returncode == 0, r.stderr
    plan = json.loads(r.stdout)
    after = {str(p): sha(p) for p in CANON_FILES}
    return {"plan": plan, "canon_before": before, "canon_after": after}


@pytest.fixture(scope="module")
def rendered(built):
    proj = INST / "project"
    key = hashlib.sha256((sha(proj / "index.html") + sha(proj / "assets/voice_norm.wav")
                          + sha(proj / "assets/sfx_bus.wav") + sha(proj / "kit/endcard.js")).encode()).hexdigest()
    stamp = INST / "render.key"
    fresh = os.environ.get("STYLE_H_FORCE_RENDER") == "1"
    # ubs:ignore — key is a content hash, not a secret
    if fresh or not VIDEO.is_file() or not stamp.is_file() or stamp.read_text() != key:
        r = subprocess.run(
            ["sandbox-exec", "-f", str(SANDBOX), "npx", "hyperframes@0.8.183", "render",
             "project", "-w", "2", "-q", "delivery", "-o", "look.mp4"],
            cwd=INST, env={**os.environ, "npm_config_offline": "true"},
            capture_output=True, text=True, timeout=3600)
        (INST / "render.log").write_text(r.stdout + r.stderr)
        assert r.returncode == 0, (r.stdout + r.stderr)[-3000:]
        stamp.write_text(key)
    assert VIDEO.is_file()
    RESULTS.mkdir(parents=True, exist_ok=True)
    return built["plan"]


def preflight(name, args, out_name=None):
    r = run([sys.executable, PRE / f"{name}.py", *args, "--json"], timeout=1800)
    (RESULTS / f"{out_name or name}.json").write_text(r.stdout)
    assert r.returncode in (0, 1), f"{name} exit {r.returncode}: {r.stderr}"
    d = json.loads(r.stdout)
    assert d["name"] == name
    assert (r.returncode == 0) == d["passed"]
    return d


# ---------------------------------------------------------------- build / instantiation

def test_build_leaves_canonical_dir_untouched(built):
    assert built["canon_before"] == built["canon_after"]


def test_build_plan_shape(built):
    p = built["plan"]
    words = json.loads(WORDS.read_text())
    lwe = max(w["end"] for w in words)
    assert len(p["bounds"]) == 7 and p["bounds"][0] == 0.0
    assert all(b - a >= 0.9 for a, b in zip(p["bounds"], p["bounds"][1:]))
    assert abs(p["bounds"][6] - p["endcard"]["start"]) < 1e-6
    assert p["endcard"]["start"] >= lwe - 0.05
    assert p["duration"] - max(p["endcard"]["start"], lwe) >= 1.5
    assert len(p["acts"]) == 6 and all(a["hl"] and a["hl"][0] for a in p["acts"])
    starts = {round(w["start"], 4) for w in words}
    for a in p["acts"]:  # every headline word lands inside its act
        for row in a["hl"]:
            for _, t, _ in row:
                assert a["start"] <= t <= a["end"] - 0.15 + 1e-6
    # the trace spans are spoken words of the trace act, placed on their speech
    tasks = [s for s in p["spans"] if s["depth"] == 1]
    assert tasks and all(round(s["t"], 4) in starts and p["bounds"][2] <= s["t"] < p["bounds"][3] for s in tasks)
    assert all(0 <= s["x0"] and s["x0"] + s["w"] <= 1.0 + 1e-6 for s in p["spans"])
    assert p["audio"]["bus_passed"] is True


def test_instantiated_html_has_no_placeholders_and_uses_kit(built):
    html = (INST / "project/index.html").read_text()
    for tok in ("__H_DURATION__", "__H_VO_DURATION__", "__H_DATA__", "__H_TITLE__"):
        assert tok not in html
    dur = f'data-duration="{built["plan"]["duration"]:.6f}"'
    assert html.count(dur) == 2  # root + sfx <audio>; the voice slot is the narration's length
    assert 'id="vo" src="assets/voice_norm.wav" data-start="0" data-duration="15.0' in html
    for bad in UNLICENSED:  # D4 (plus the canonical's other system faces): bundled OFL only
        assert bad not in html, bad
    assert "local(" not in html
    assert sha(INST / "project/kit/endcard.js") == sha(PLUGIN / "kit/endcard.js")
    for f in ("LilitaOne.woff2", "ComicShanns.woff2", "Virgil.woff2"):
        assert sha(INST / "project/assets/fonts" / f) == sha(PLUGIN / "kit/fonts" / f)


def test_build_rejects_words_past_the_narration(tmp_path):
    """Planted negative: a word that ends after --duration must fail with exit 2."""
    words = json.loads(WORDS.read_text())
    r = run([sys.executable, BUILD_PY, "--wav", WAV, "--words", WORDS, "--out", tmp_path / "x",
             "--duration", str(max(w["end"] for w in words) - 1.0)])
    assert r.returncode == 2
    assert "after the narration" in r.stderr


def test_build_rejects_missing_wav_and_bad_content(tmp_path):
    r = run([sys.executable, BUILD_PY, "--wav", tmp_path / "nope.wav", "--words", WORDS, "--out", tmp_path / "x"])
    assert r.returncode == 2 and "wav not found" in r.stderr
    cj = tmp_path / "c.json"
    cj.write_text(json.dumps({"bogus": 1}))
    r = run([sys.executable, BUILD_PY, "--wav", WAV, "--words", WORDS, "--out", tmp_path / "y", "--content", cj])
    assert r.returncode == 2 and "unknown keys" in r.stderr


@pytest.mark.studio("_kit/lookdev/inputs/r6-S49.wav", "_kit/lookdev/inputs/r6-S49.words.json")
def test_template_is_script_agnostic(tmp_path):
    """A different narration (the full S49 words, first ~20 s) builds a different plan."""
    words = [w for w in json.loads((INPUTS / "r6-S49.words.json").read_text()) if w["end"] <= 20.0]
    wj = tmp_path / "w.json"
    wj.write_text(json.dumps(words))
    r = run([sys.executable, BUILD_PY, "--wav", INPUTS / "r6-S49.wav", "--words", wj, "--out", tmp_path / "o",
             "--duration", "20.0", "--json"])
    assert r.returncode == 0, r.stderr
    p = json.loads(r.stdout)
    assert p["lastWordEnd"] == round(max(w["end"] for w in words), 4)
    assert p["bounds"][6] == p["endcard"]["start"]
    assert p["acts"][5]["lines"] != ["BORROWS FIX", "BANKING."]  # not the S49 cut's close
    pr = run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
              tmp_path / "o/project/assets/voice_norm.wav"])
    assert abs(float(pr.stdout) - min(20.0, p["endcard"]["start"])) < 0.02
    assert f'data-duration="{p["duration"]:.6f}"' in (tmp_path / "o/project/index.html").read_text()


# ---------------------------------------------------------------- canvas-side checks (DOM preflight is blind here)

def _serve(directory):
    class _QuietHandler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            return None

    handler = functools.partial(_QuietHandler, directory=str(directory))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


SCAN_JS = """async ([dur, step]) => {
  const tl = window.__timelines.main; const out = [];
  for (let t = 0; t <= dur + 1e-6; t += step) {
    window.__hText.min = Infinity; window.__hText.small = []; window.__hText.count = 0;
    tl.seek(Math.min(t, dur - 1e-3), false);
    out.push({t: Math.round(t * 100) / 100, min: window.__hText.min, count: window.__hText.count, small: window.__hText.small});
  }
  return out;
}"""


def canvas_text_scan(project):
    """Scan the canvas text record over the timeline in headless Chrome via agent-browser."""
    import shutil

    from preflight.legibility import AGENT_BROWSER_VERSION, AgentBrowser
    exe = shutil.which("agent-browser")
    assert exe, (
        f"agent-browser not found on PATH (npm install -g agent-browser@{AGENT_BROWSER_VERSION} "
        "&& agent-browser install, which downloads Chrome for Testing)"
    )
    srv = _serve(project)
    b = AgentBrowser(exe, None, 20000)  # private session; non-local hosts blocked (--allowed-domains)
    try:
        b.run("open")
        b.run("set", "viewport", "1080", "1920")
        b.goto(f"http://127.0.0.1:{PORT}/index.html")
        b.run("wait", "--fn", "!!(window.__timelines && window.__timelines.main)", "--timeout", "20000")
        dur = float(b.run_js("() => parseFloat(document.getElementById('root').getAttribute('data-duration'))"))
        rows = b.run_js(SCAN_JS, [dur, 0.1])
        fonts = b.run_js("() => [...document.fonts].filter(f => f.status === 'loaded').map(f => f.family)")
        errors = [str(e.get("text", e)) for e in b.run("errors").get("errors", [])]
    finally:
        b.close()
        srv.shutdown()
        srv.server_close()
    return rows, errors, fonts


def test_canvas_text_is_at_least_44px(built):
    rows, errors, fonts = canvas_text_scan(INST / "project")
    assert errors == []
    assert {'"KS Shanns"', '"KS Lilita"', '"KS Virgil"'} <= set(fonts) or {"KS Shanns", "KS Lilita", "KS Virgil"} <= set(fonts)
    drawn = [r for r in rows if r["count"] > 0]
    assert len(drawn) >= len(rows) - 1  # text is drawn on every sampled frame
    small = [r for r in rows if r["small"]]
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "canvas_text.json").write_text(json.dumps(
        {"samples": len(rows), "min_px": min(r["min"] for r in drawn), "small_samples": small[:5]}))
    assert small == [], small[:3]
    assert min(r["min"] for r in drawn) >= 44.0 - 0.01  # float noise of the transform determinant


def test_canvas_text_scan_catches_small_type(built, tmp_path):
    """Planted negative: a copy of the instance with the HUD label back at the canonical 26 px
    must be flagged by the scan (through the template's own txt() record)."""
    import shutil
    proj = tmp_path / "project"
    shutil.copytree(INST / "project", proj)
    html = (proj / "index.html").read_text()
    needle = 'txt(fitTxt(CO.mark, 44, MONO, 700, 420), 130, 104, 44, MONO, 700, C.ink, "left", 3);'
    assert needle in html
    (proj / "index.html").write_text(html.replace(needle, needle.replace("130, 104, 44,", "130, 104, 26,")))
    rows, errors, _ = canvas_text_scan(proj)
    assert errors == []
    assert any(s[0] == "KELLERAI" and s[1] == 26 for r in rows for s in r["small"])


def band_ink(video, times, top=1280, luma=40):
    """Fraction of lower-band pixels (y >= top) brighter than `luma`, per sample time."""
    import numpy as np
    out = []
    for t in times:
        r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", str(video), "-frames:v", "1",
                            "-vf", f"crop=1080:{1920 - top}:0:{top},format=gray", "-f", "rawvideo", "-"],
                           capture_output=True, timeout=120)
        assert r.returncode == 0, r.stderr.decode()[-500:]
        a = np.frombuffer(r.stdout, dtype=np.uint8)
        out.append(float((a > luma).mean()))
    return out


def test_lower_band_has_ink_on_every_sample(rendered):
    """Every content frame before the end card (the kit end card is a flat brand card, gated by
    preflight/endcard.py instead) has ink in the lower third."""
    times = [round(0.25 + i * 0.5, 2) for i in range(int((rendered["endcard"]["start"] - 0.3) / 0.5))]
    ink = band_ink(VIDEO, times)
    (RESULTS / "lower_band.json").write_text(json.dumps(dict(zip(map(str, times), ink))))
    assert min(ink) >= 0.01, [(t, v) for t, v in zip(times, ink) if v < 0.01]


def test_lower_band_check_catches_an_empty_band(tmp_path):
    """Planted negative: a frame that is the style's background colour everywhere scores ~0."""
    v = tmp_path / "bg.mp4"
    r = run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=0x040507:s=1080x1920:d=1:r=30", "-pix_fmt", "yuv420p", v])
    assert r.returncode == 0, r.stderr
    assert band_ink(v, [0.5])[0] < 0.01


# ---------------------------------------------------------------- render + preflight

def test_dead_frames(rendered):
    d = preflight("dead_frames", [VIDEO])
    assert d["passed"], d["findings"]


def test_endcard_scene_method(rendered):
    d = preflight("endcard", [VIDEO, WORDS])
    assert d["passed"], d["findings"]
    assert abs(d["metrics"]["end_card_start"] - rendered["endcard"]["start"]) <= 0.1


def test_endcard_explicit_start(rendered):
    d = preflight("endcard", [VIDEO, WORDS, "--end-card-start", rendered["endcard"]["start"]], "endcard_explicit")
    assert d["passed"], d["findings"]


def test_loudness_delivered_mix(rendered):
    d = preflight("loudness", [VIDEO], "loudness_mix")
    assert d["passed"], d["findings"]


def test_loudness_stems(rendered):
    a = INST / "project/assets"
    d = preflight("loudness", [VIDEO, "--voice", a / "voice_norm.wav", "--sfx", a / "sfx_bus.wav"], "loudness_stems")
    assert d["passed"], d["findings"]
    assert isinstance(d["metrics"].get("sfx_under_voice_db"), float)


def test_legibility(rendered):
    d = preflight("legibility", [INST / "project", "--samples", "51"])
    assert d["passed"], d["findings"]
    assert d["metrics"]["page_errors"] == []


def test_evidence_report_all_checks(rendered):
    out = INST / "evidence"
    a = INST / "project/assets"
    r = run([sys.executable, EVIDENCE, "report", VIDEO, "--out", out, "--words", WORDS,
             "--run", "dead_frames {video}",
             "--run", f"endcard {{video}} {WORDS}",
             "--run", f"loudness {{video}} --voice {a / 'voice_norm.wav'} --sfx {a / 'sfx_bus.wav'}",
             "--run", f"legibility {INST / 'project'}"], timeout=3600)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-3000:]
    rep = json.loads((out / "preflight_report.json").read_text())
    (RESULTS / "evidence_summary.json").write_text(json.dumps(
        {"passed": rep["passed"], "checks": [{"name": c["name"], "passed": c["passed"]} for c in rep["checks"]]}))
    assert rep["passed"] is True
    assert sorted(c["name"] for c in rep["checks"]) == ["dead_frames", "endcard", "legibility", "loudness"]
    assert all(c["passed"] for c in rep["checks"])
    assert rep["sheets"]
