import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "review" / "build_review_page.py"
FB = ROOT / "feedback" / "feedback.py"
RENDERS = ["D=share/round1_S61_D-reel-native.mp4", "C=share/round1_S61_C-product-ui.mp4",
           "K/K16=share/round6_S49_K16-pixel-arcade-16bit.mp4"]
NODE = shutil.which("node")
STUDIO_ENV = "SHORTS_STUDIO_DIR"


def run(script, *args, stdin=None, env=None):
    return subprocess.run([sys.executable, str(script), *map(str, args)], capture_output=True,
                          text=True, check=False, timeout=60, input=stdin, env=env)


def env_without_studio():
    return {k: v for k, v in os.environ.items() if k != STUDIO_ENV}


@pytest.fixture
def page(tmp_path):
    """The page only links the videos (file:// URIs) and never reads them, so the studio
    root here is a scratch dir: these tests check page structure and logic, not renders."""
    studio = tmp_path / "studio"
    out = tmp_path / "page.html"
    r = run(CLI, "build", "--out", out, "--title", "T", *[a for s in RENDERS for a in ("--render", s)],
            env=env_without_studio() | {STUDIO_ENV: str(studio)})
    assert r.returncode == 0, r.stderr
    srcs = re.findall(r'<video[^>]+src="([^"]+)"', out.read_text())
    lookdev = json.loads((ROOT / "styles" / "styles.json").read_text())["lookdev_root"]
    assert srcs and all(s.startswith((studio / lookdev).as_uri() + "/share/") for s in srcs), srcs
    return out


def test_build_fails_closed_without_studio_dir(tmp_path):
    """Planted negative: with $SHORTS_STUDIO_DIR unset, build exits 2 and writes no page."""
    out = tmp_path / "p.html"
    r = run(CLI, "build", "--out", out, "--render", RENDERS[0], env=env_without_studio())
    assert r.returncode == 2 and "SHORTS_STUDIO_DIR is not set" in r.stderr
    assert not out.exists()


def node_export(page_path, inputs, rater="tester"):
    """Run the page's own logic <script> under node (real page code, no DOM)."""
    html = page_path.read_text()
    logic_m = re.search(r'<script id="logic">(.*?)</script>', html, re.S)
    config_m = re.search(r'<script type="application/json" id="config">(.*?)</script>', html, re.S)
    if logic_m is None or config_m is None:
        raise AssertionError("page is missing the logic or config <script> block")
    logic, config = logic_m.group(1), config_m.group(1)
    js = (logic + f"\nconst cfg={config};console.log(JSON.stringify(buildRows(cfg,"
          f"{json.dumps(inputs)},{json.dumps(rater)},'2026-10-23T10:00:00+02:00')));")
    if NODE is None:
        raise RuntimeError("node not installed")
    r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, check=False, timeout=30)
    return r


def test_page_is_self_contained(page):
    html = page.read_text()
    assert html.count("<video") == 3 and 'id="export"' in html
    assert "http://" not in html and "https://" not in html and "<link" not in html
    assert re.search(r'<script[^>]+src=', html) is None


def test_build_rejects_unknown_style_and_variant(tmp_path):
    for spec in ("ZZ=share/x.mp4", "K/nope=share/x.mp4", "noequals"):
        r = run(CLI, "build", "--out", tmp_path / "p.html", "--render", spec)
        assert r.returncode == 2 and "build_review_page:" in r.stderr, spec
        assert not (tmp_path / "p.html").exists()


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_exported_json_accepted_by_feedback_ingest(page, tmp_path):
    inputs = [{"score": "6", "liked": "clean, pacing", "disliked": "", "text": "good"},
              {"score": "", "liked": "", "disliked": "", "text": ""},
              {"score": "5.5", "liked": "", "disliked": "captions", "text": ""}]
    r = node_export(page, inputs)
    assert r.returncode == 0, r.stderr
    rows = json.loads(r.stdout)
    assert [x["style_key"] for x in rows] == ["D", "K"] and rows[1]["variant"] == "K16"
    export = tmp_path / "export.json"
    export.write_text(r.stdout)
    store = tmp_path / "ratings.jsonl"
    s = run(CLI, "submit", export, "--store", store)
    assert s.returncode == 0, s.stderr
    assert len(store.read_text().splitlines()) == 2
    # piping through stdin works too
    store2 = tmp_path / "r2.jsonl"
    assert run(CLI, "submit", "-", "--store", store2, stdin=r.stdout).returncode == 0
    # the collect path reads the store (validates every line) and ingest ranks it
    cal = tmp_path / "cal.json"
    i = run(FB, "ingest", "--store", store, "--out", cal)
    assert i.returncode == 0, i.stderr
    data = json.loads(cal.read_text())
    assert data["rows_read"] == 2
    assert data["cohorts"]["global"]["order"] == ["D"]
    assert data["cohorts"]["K-variants"]["order"] == ["K16"]
    assert data["cohorts"]["global"]["subjects"]["D"]["ratings"][0]["rater"] == "tester"
    assert data["cohorts"]["global"]["subjects"]["D"]["ratings"][0]["score"] == 6


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_page_logic_refuses_bad_input(page):
    assert node_export(page, [{"score": "11"}]).returncode != 0   # planted: out of range
    assert node_export(page, [{"score": "5"}], rater=" ").returncode != 0  # planted: no rater
    assert node_export(page, [{}, {}, {}]).returncode != 0         # nothing rated


GOOD_ROW = {"style_key": "D", "variant": None, "cohort": "global", "video": "share/a.mp4",
            "rater": "t", "score": 6, "score_origin": "rater", "liked": [], "disliked": [],
            "text": "", "ts": "2026-10-23T10:00:00+02:00", "source": None}


def test_submit_default_store_is_plugin_data_not_seed(tmp_path, monkeypatch):
    """submit without --store writes to $CLAUDE_PLUGIN_DATA/feedback/ratings.jsonl (seeded from
    the shipped seed first) and leaves the shipped seed untouched; with the variable unset it
    exits 2 and writes nothing (planted negative)."""
    seed = ROOT / "feedback" / "ratings.jsonl"
    seed_before = seed.read_bytes()
    export = tmp_path / "export.json"
    export.write_text(json.dumps([GOOD_ROW]))
    data = tmp_path / "data"
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
    r = run(CLI, "submit", export)  # negative: no --store, no CLAUDE_PLUGIN_DATA
    assert r.returncode == 2 and "CLAUDE_PLUGIN_DATA is not set" in r.stderr
    assert not data.exists() and seed.read_bytes() == seed_before
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(data))
    r = run(CLI, "submit", export)
    assert r.returncode == 0, r.stderr
    store = data / "feedback" / "ratings.jsonl"
    assert json.loads(r.stdout)["store"] == str(store)
    lines = store.read_text().splitlines()
    assert lines[-1] == json.dumps(GOOD_ROW, sort_keys=True, ensure_ascii=False)
    assert "\n".join(lines[:-1]) + "\n" == seed_before.decode()  # seed copied in, then one row appended
    assert seed.read_bytes() == seed_before


def test_submit_rejects_missing_required_field(tmp_path):
    """Planted negative: a row missing `rater` is rejected, nothing is appended."""
    good = {"style_key": "D", "variant": None, "cohort": "global", "video": "share/a.mp4",
            "rater": "t", "score": 6, "score_origin": "rater", "liked": [], "disliked": [],
            "text": "", "ts": "2026-10-23T10:00:00+02:00", "source": None}
    store = tmp_path / "ratings.jsonl"
    ok = tmp_path / "ok.json"
    ok.write_text(json.dumps([good]))
    assert run(CLI, "submit", ok, "--store", store).returncode == 0  # positive control
    before = store.read_text()
    bad = {k: v for k, v in good.items() if k != "rater"}
    badf = tmp_path / "bad.json"
    badf.write_text(json.dumps([good, bad]))
    r = run(CLI, "submit", badf, "--store", store)
    assert r.returncode == 2 and "missing field(s): rater" in r.stderr
    assert store.read_text() == before  # all or nothing: the good row in the same file not appended
    # and the feedback CLI itself refuses a store line missing the field
    corrupt = tmp_path / "corrupt.jsonl"
    corrupt.write_text(json.dumps(bad) + "\n")
    assert run(FB, "ingest", "--store", corrupt, "--out", tmp_path / "c.json").returncode == 2
