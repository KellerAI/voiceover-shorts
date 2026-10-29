"""Tests for preflight/legibility.py (item 6).

The CLI and check() tests drive a real headless Chrome (agent-browser CLI) against HTML fixtures
written to tmp_path; nothing in the browser path is mocked. The helper tests at the bottom are browser-free
unit isolation of the pure classification code; they supplement the real-path tests.
"""

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from preflight import legibility as lg

SCRIPT = Path(__file__).resolve().parents[1] / "preflight" / "legibility.py"
CONTRACT_KEYS = {"name", "passed", "findings", "metrics"}
FINDING_KEYS = {"t_start", "t_end", "kind", "detail"}


def page(body: str, duration: float = 4.0, w: int = 1080, h: int = 1920) -> str:
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
html, body {{ width: {w}px; height: {h}px; overflow: hidden; background: #111; color: #fff;
  font-family: sans-serif; }}
#root {{ position: relative; width: {w}px; height: {h}px; overflow: hidden; }}
.abs {{ position: absolute; left: 80px; }}
</style></head><body>
<div id="root" data-composition-id="main" data-start="0" data-width="{w}" data-height="{h}"
     data-duration="{duration}">
{body}
</div></body></html>"""


# Planted negative: small type everywhere and nothing below y = 1280.
BAD = page("""
<div class="abs" style="top:200px;font-size:20px">Tiny headline nobody can read</div>
<div class="abs" style="top:600px;font-size:24px">Small body copy, still too small</div>
<div class="abs" style="top:900px;width:400px;height:200px;background:#335"></div>
""")

# Good: big type, and a solid caption card in the lower third.
GOOD = page("""
<div class="abs" style="top:300px;font-size:96px">Big headline</div>
<div class="abs" style="top:1450px;width:920px;height:220px;background:#224;font-size:56px;padding:40px">
  Readable caption</div>
""")

# Planted negative: the only lower-third content is a 22 px footer, which neither passes the size
# floor nor counts as filling the band.
FOOTER_ONLY = page("""
<div class="abs" style="top:300px;font-size:96px">Big headline</div>
<div class="abs" style="top:1800px;font-size:22px">01/06 THE SETUP</div>
""")

# Planted negative: 88 px type under a 0.25 scale transform renders at 22 px.
SCALED_DOWN = page("""
<div class="abs" style="top:300px;font-size:96px">Big headline</div>
<div class="abs" style="top:1400px;transform:scale(0.25);transform-origin:0 0;font-size:88px">Shrunk</div>
<div class="abs" style="top:1500px;font-size:64px">Lower words</div>
""")

# Good despite small text: it is faded below the opacity floor, opted out, or clipped away.
EXCLUDED_SMALL = page("""
<div class="abs" style="top:300px;font-size:96px">Big headline</div>
<div class="abs" style="top:500px;font-size:18px;opacity:0.05">faded label</div>
<div class="abs" data-legibility="ignore" style="top:700px;font-size:18px">watermark</div>
<div class="abs" style="top:900px;width:600px;height:100px;overflow:hidden">
  <div style="transform:translateY(400px);font-size:18px">clipped away</div></div>
<div class="abs" style="top:1500px;font-size:64px">Lower words</div>
""")

# A full-bleed SVG overlay is a backdrop as a whole; its shapes still count where they draw.
SVG_OVERLAY = """
<div class="abs" style="top:300px;font-size:96px">Big headline</div>
<svg style="position:absolute;left:0;top:0" width="1080" height="1920" viewBox="0 0 1080 1920">
  <rect x="{x}" y="{y}" width="600" height="120" fill="#4da6ff"/>
  <path d="M0 0 L1080 0" stroke="#fff" stroke-width="4" fill="none"/>
</svg>
"""
SVG_LOW = page(SVG_OVERLAY.format(x=120, y=1500))

# Draw-on stroke in the band: undrawn (offset == dash) must not fill; drawn (offset 0) must.
DRAW_ON = """
<div class="abs" style="top:300px;font-size:96px">Big headline</div>
<svg style="position:absolute;left:0;top:0" width="1080" height="1920" viewBox="0 0 1080 1920">
  <path d="M100 1500 L900 1500 L900 1700" stroke="#4da6ff" stroke-width="6" fill="none"
        stroke-dasharray="1000" stroke-dashoffset="{off}"/>
</svg>
"""
SVG_HIGH = page(
    SVG_OVERLAY.format(x=120, y=600)
)  # planted negative: shape is above the band

# 720x1280 canvas: the floor scales to 44 * 720 / 1080 = 29.33 px, the band starts at y = 853.
SMALL_CANVAS = page(
    """
<div class="abs" style="top:200px;font-size:{px}px">Scaled canvas headline</div>
<div class="abs" style="top:1000px;font-size:48px">Lower</div>
""",
    w=720,
    h=1280,
)


def write(tmp_path: Path, name: str, html: str) -> Path:
    p = tmp_path / name
    p.write_text(html)
    return p


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=240,
        check=False,
    )


def assert_contract(res: dict[str, Any]) -> None:
    assert set(res) == CONTRACT_KEYS
    assert res["name"] == "legibility" and isinstance(res["passed"], bool)
    assert isinstance(res["metrics"], dict)
    for f in res["findings"]:
        assert set(f) == FINDING_KEYS
        assert isinstance(f["t_start"], float) and f["t_start"] <= f["t_end"]


# ---------- real path: CLI subprocess + headless Chromium ----------


def test_cli_bad_composition_fails_both_rules(tmp_path: Path) -> None:
    r = run_cli(str(write(tmp_path, "bad.html", BAD)), "--json", "--at", "0.5,2")
    assert r.returncode == 1, r.stderr
    res = json.loads(r.stdout)
    assert_contract(res)
    assert not res["passed"]
    kinds = [f["kind"] for f in res["findings"]]
    assert kinds.count("small_type") == 2 and kinds.count("empty_lower_third") == 2
    small = next(f for f in res["findings"] if f["kind"] == "small_type")
    assert "Tiny headline" in small["detail"] and "20.0px" in small["detail"]
    m = res["metrics"]
    assert m["samples"] == [0.5, 2.0] and m["min_rendered_px_seen"] == 20
    assert m["empty_lower_third_samples"] == [0.5, 2.0] and not m["bundled"]


def test_cli_good_composition_passes(tmp_path: Path) -> None:
    r = run_cli(str(write(tmp_path, "good.html", GOOD)), "--json", "--samples", "3")
    assert r.returncode == 0, r.stderr
    res = json.loads(r.stdout)
    assert_contract(res)
    assert res["passed"] and res["findings"] == []
    m = res["metrics"]
    # midpoint samples of a 4 s composition, as hyperframes inspect does
    assert m["samples"] == [0.667, 2.0, 3.333]
    assert m["min_rendered_px_seen"] == 56 and m["text_runs_measured"] == 6
    assert all(s["lower_third"] == "filled" for s in m["per_sample"])


def test_cli_min_px_flag_moves_the_threshold(tmp_path: Path) -> None:
    good = str(write(tmp_path, "good.html", GOOD))
    r = run_cli(good, "--json", "--at", "1", "--min-px", "60")
    assert r.returncode == 1, r.stderr
    res = json.loads(r.stdout)
    # The 56 px caption is now below the floor; its solid card still fills the lower third.
    assert [f["kind"] for f in res["findings"]] == ["small_type"]
    assert "Readable caption" in res["findings"][0]["detail"]
    assert (
        res["metrics"]["min_px_scaled"] == 60
        and res["metrics"]["per_sample"][0]["lower_third"] == "filled"
    )


def test_cli_usage_and_io_errors_exit_2(tmp_path: Path) -> None:
    r = run_cli(str(tmp_path / "missing.html"), "--json")
    assert (
        r.returncode == 2
        and r.stdout == ""
        and "not a project dir or .html file" in r.stderr
    )
    (tmp_path / "proj").mkdir()
    r = run_cli(str(tmp_path / "proj"), "--json")
    assert r.returncode == 2 and "no index.html" in r.stderr
    r = run_cli(str(write(tmp_path, "good.html", GOOD)), "--at", "a,b")
    assert r.returncode == 2
    no_root = write(tmp_path, "noroot.html", "<html><body><p>hi</p></body></html>")
    r = run_cli(str(no_root), "--json")
    assert r.returncode == 2 and "data-composition-id" in r.stderr


def test_footer_only_lower_third_is_still_empty(tmp_path: Path) -> None:
    res = lg.check(str(write(tmp_path, "footer.html", FOOTER_ONLY)), at=[1.0])
    assert_contract(res)
    assert sorted(f["kind"] for f in res["findings"]) == [
        "empty_lower_third",
        "small_type",
    ]
    assert "01/06 THE SETUP" in res["findings"][0]["detail"]


def test_transform_scale_counts_toward_rendered_size(tmp_path: Path) -> None:
    res = lg.check(str(write(tmp_path, "scaled.html", SCALED_DOWN)), at=[1.0])
    assert [f["kind"] for f in res["findings"]] == ["small_type"]
    assert "'Shrunk' 22.0px (font 88.0px x0.25)" in res["findings"][0]["detail"]


def test_faded_ignored_and_clipped_text_is_excluded(tmp_path: Path) -> None:
    res = lg.check(str(write(tmp_path, "excluded.html", EXCLUDED_SMALL)), at=[1.0])
    assert res["passed"], res["findings"]
    assert res["metrics"]["min_rendered_px_seen"] == 64


def test_bundled_project_dir_follows_clip_timing(tmp_path: Path) -> None:
    """Project-dir path: hyperframes' bundler + runtime decide clip visibility over time."""
    if lg.find_hyperframes_dist() is None:
        pytest.skip(
            "hyperframes@0.8.183 not in the npx cache; bundled path not testable here"
        )
    proj = tmp_path / "proj"
    proj.mkdir()
    write(
        proj,
        "index.html",
        page("""
<div class="abs" style="top:300px;font-size:96px">Big headline</div>
<div id="cap" class="clip abs" data-start="2" data-duration="2" data-track-index="1"
     style="top:1500px;font-size:64px">Caption arrives at 2 s</div>
<script>window.__timelines = window.__timelines || {};</script>
"""),
    )
    r = run_cli(str(proj), "--json", "--at", "0.5,3")
    assert r.returncode == 1, r.stderr
    res = json.loads(r.stdout)
    assert_contract(res)
    assert isinstance(res["metrics"]["bundled"], bool) and res["metrics"]["bundled"]
    assert [(f["kind"], f["t_start"]) for f in res["findings"]] == [
        ("empty_lower_third", 0.5)
    ]
    assert res["metrics"]["per_sample"][1]["lower_third"] == "filled"


def test_full_bleed_svg_overlay_counts_by_shape(tmp_path: Path) -> None:
    low = lg.check(str(write(tmp_path, "svg_low.html", SVG_LOW)), at=[1.0])
    assert low["passed"], low["findings"]
    high = lg.check(str(write(tmp_path, "svg_high.html", SVG_HIGH)), at=[1.0])
    assert [f["kind"] for f in high["findings"]] == ["empty_lower_third"]
    undrawn = lg.check(
        str(write(tmp_path, "undrawn.html", page(DRAW_ON.format(off=1000)))), at=[1.0]
    )
    assert [f["kind"] for f in undrawn["findings"]] == ["empty_lower_third"]
    drawn = lg.check(
        str(write(tmp_path, "drawn.html", page(DRAW_ON.format(off=0)))), at=[1.0]
    )
    assert drawn["passed"], drawn["findings"]


def test_non_1080_canvas_scales_threshold(tmp_path: Path) -> None:
    ok = lg.check(
        str(write(tmp_path, "c32.html", SMALL_CANVAS.replace("{px}", "32"))), at=[1.0]
    )
    assert ok["passed"], ok["findings"]
    m = ok["metrics"]
    assert (m["width"], m["height"], m["min_px_scaled"]) == (720, 1280, 29.33)
    bad = lg.check(
        str(write(tmp_path, "c26.html", SMALL_CANVAS.replace("{px}", "26"))), at=[1.0]
    )
    assert [f["kind"] for f in bad["findings"]] == ["small_type"]
    assert "26.0px" in bad["findings"][0]["detail"]


# ---------- unit isolation: pure helpers, no browser ----------


def test_scaled_min_px() -> None:
    assert lg.scaled_min_px(44, 1080) == 44
    assert lg.scaled_min_px(44, 720) == pytest.approx(29.333, abs=1e-3)


def test_sample_times() -> None:
    assert lg.sample_times(10.0, 4, None) == [1.25, 3.75, 6.25, 8.75]
    assert lg.sample_times(10.0, 4, [3, 1, 3]) == [1.0, 3.0]
    assert lg.sample_times(0.0, 9, None) == [0.0]


def test_band_area_and_full_bleed() -> None:
    assert lg.band_area([0, 0, 1080, 1280], 1080, 1920) == 0  # stops exactly at 2H/3
    assert lg.band_area([0, 1270, 100, 20], 1080, 1920) == 1000
    assert lg.is_full_bleed([-10, -10, 1100, 1940], 1080, 1920)
    assert not lg.is_full_bleed([0, 0, 1080, 1500], 1080, 1920)


def _txt(px: float, box: list[float], ink: float = 1.0) -> dict[str, Any]:
    return {
        "text": "x",
        "font_px": px,
        "scale": 1.0,
        "rendered_px": px,
        "ink": ink,
        "box": box,
    }


def test_classify_sample_statuses() -> None:
    big_low = _txt(60, [0, 1400, 500, 80])
    c = lg.classify_sample({"texts": [big_low], "boxes": []}, 1080, 1920, 44, 0.2)
    assert c["lower_third"] == "filled" and c["small"] == []
    small_low = _txt(20, [0, 1400, 500, 80])
    c = lg.classify_sample({"texts": [small_low], "boxes": []}, 1080, 1920, 44, 0.2)
    assert c["lower_third"] == "empty" and len(c["small"]) == 1
    faded = _txt(20, [0, 100, 500, 80], ink=0.1)
    c = lg.classify_sample({"texts": [faded], "boxes": []}, 1080, 1920, 44, 0.2)
    assert c["small"] == [] and c["texts"] == []
    canvas = {"tag": "canvas", "kind": "raster", "ink": 1.0, "box": [0, 0, 1080, 1920]}
    c = lg.classify_sample({"texts": [], "boxes": [canvas]}, 1080, 1920, 44, 0.2)
    assert c["lower_third"] == "indeterminate"
    card = {"tag": "div", "kind": "block", "ink": 1.0, "box": [80, 1500, 900, 200]}
    c = lg.classify_sample({"texts": [], "boxes": [card]}, 1080, 1920, 44, 0.2)
    assert c["lower_third"] == "filled"
