"""Tests for workflows/lookdev.js (lookdev v2).

The workflow runs inside the Claude Code Workflow runtime, which cannot be started here. These
tests drive the real script through tests/lookdev_workflow_harness.mjs, whose agent(), parallel(),
pipeline(), budget and resume cache are LABELLED STUBS (see the header of that file). They check
the loop, re-score, gate, resume and journal logic and the prompt policy. Real execution (renders,
preflight, critic) is out of scope for these tests. The v1 reproduction test reads the v1 workflow
from SHORTS_STUDIO_DIR and skips without it.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PLUGIN = Path(__file__).resolve().parents[1]
SCRIPT = PLUGIN / "workflows" / "lookdev.js"
HARNESS = PLUGIN / "tests" / "lookdev_workflow_harness.mjs"
V1_REL = ".claude/workflows/shorts-lookdev.js"
V1 = Path(os.environ["SHORTS_STUDIO_DIR"]) / V1_REL if os.environ.get("SHORTS_STUDIO_DIR") else None
RULES = [
    "No deletions of any kind, including your own scratch copies: write to a new uniquely named path instead.",
    "No full tree copies (cp -R, rsync) into scratch; copy only the named files you need.",
    "Never silence stderr.",
]
TOKENS = {"plan": 900, "build": 1000, "segment": 600, "assemble": 50, "gate": 100, "fix": 700, "critic": 500, "revise": 800}

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is required to run the workflow harness")


@lru_cache(maxsize=None)
def run(scenario: str) -> dict:
    p = subprocess.run(["node", str(HARNESS), scenario, str(SCRIPT)], capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, f"harness {scenario} exit {p.returncode}\nstderr:\n{p.stderr}"
    return json.loads(p.stdout)


def result(scenario: str) -> dict:
    out = run(scenario)
    assert out["error"] is None, out["error"]
    return out["result"]


def style(res: dict, key: str) -> dict:
    return next(r for r in res["results"] if r["key"] == key)


def policy_violations(calls: list[dict]) -> list[str]:
    bad = []
    for c in calls:
        lines = c["prompt"].split("\n")
        if c["model"] not in ("opus", "sonnet"):
            bad.append(f"{c['label']}: model {c['model']!r}")
        if c["effort"] not in ("low", "medium", "high"):
            bad.append(f"{c['label']}: effort {c['effort']!r}")
        if lines[0] != f"Effort: {c['effort']}":
            bad.append(f"{c['label']}: first line {lines[0]!r}")
        if c["effort"] == "high" and not lines[1].startswith("EFFORT_CEILING_OVERRIDE — reason: "):
            bad.append(f"{c['label']}: high effort without EFFORT_CEILING_OVERRIDE")
        if not c["hasSchema"]:
            bad.append(f"{c['label']}: no schema")
        for rule in RULES:
            if rule not in lines:
                bad.append(f"{c['label']}: missing rule line {rule!r}")
    return bad


# ---------------------------------------------------------------- script shape and runtime rules

def test_meta_is_a_pure_literal_with_phases():
    out = run("meta")
    assert out["pure"] is True
    meta = out["meta"]
    assert meta["name"] == "shorts-lookdev-v2"
    assert [p["title"] for p in meta["phases"]] == ["Prep", "Plan", "Build", "Preflight", "Critique", "Revise"]
    assert SCRIPT.read_text().startswith("export const meta = {")


def test_meta_purity_check_rejects_an_expression():
    # planted negative for the purity check itself
    assert run("meta_impure")["pure"] is False


def test_script_uses_no_banned_runtime_apis():
    src = SCRIPT.read_text()
    for banned in ("Date.now", "Math.random", "new Date()", "require(", "child_process", "from 'fs'", "from \"fs\"", "process.", "import "):
        assert banned not in src, banned
    assert "pipeline(" not in re.sub(r"//.*", "", src), "v2 must not issue calls through pipeline() (completion-order dependent)"


def test_banned_api_shim_catches_a_date_now_call():
    # planted negative: the harness runs the script with throwing Date/Math shims, like the runtime
    out = run("banned_api")
    assert out["error"] == "banned: Date.now()"
    assert out["labels"] == []


# ---------------------------------------------------------------- prep (pick_cut runs in the caller)

@pytest.mark.parametrize("scenario,needle", [
    ("prep_missing_cut", "cut is missing: run pick_cut.py first"),
    ("prep_out_of_window", "outside the look window 12.5-17.5 s"),
    ("prep_cut_fail_status", "pick_cut.py reported FAIL"),
    ("prep_bad_style", 'unknown style key "Z"'),
    ("prep_outroot_in_plugin", "outRoot must be outside pluginRoot"),
])
def test_prep_rejects_bad_args_before_any_agent(scenario, needle):
    out = run(scenario)
    res = out["result"]
    assert res["ok"] is False and res["results"] == []
    assert needle in res["fail"], res["fail"]
    assert res["fail"].startswith("Prep FAIL: ")
    assert out["labels"] == [], "no agent may run when prep fails"
    assert any(line.startswith("Prep FAIL: ") for line in out["logs"])


# ---------------------------------------------------------------- loop and re-score

def test_revise_loop_rescores_until_shippable():
    out = run("ship_after_2")
    assert out["labels"] == [
        "build:D#a0", "gate:D#a0", "critic:D#a0",
        "revise:D#a1", "gate:D#a1", "critic:D#a1",
        "revise:D#a2", "gate:D#a2", "critic:D#a2",
    ]
    d = style(out["result"], "D")
    assert d["status"] == "shipped" and d["score"] == 8 and d["revisions"] == 2
    assert [s["raw"] for s in d["scores"]] == [5, 6, 8]
    assert d["mp4"] == "/sim/revise:D#a2.mp4" and d["attemptDir"] == "/sim/out/sim-D/a2"


def test_revise_loop_stops_after_two_revisions():
    out = run("stop_at_2")
    assert [lab.split(":")[0] for lab in out["labels"]].count("revise") == 2
    assert [lab.split(":")[0] for lab in out["labels"]].count("critic") == 3
    d = style(out["result"], "D")
    assert d["status"] == "below_bar" and d["score"] == 6 and d["revisions"] == 2


def test_score_of_seven_ships_without_revise():
    out = run("ship_first")
    assert out["labels"] == ["build:D#a0", "gate:D#a0", "critic:D#a0"]
    assert style(out["result"], "D")["status"] == "shipped"


def test_max_revise_zero_skips_revise():
    out = run("max_revise_0")
    assert out["labels"] == ["build:D#a0", "gate:D#a0", "critic:D#a0"]
    assert style(out["result"], "D")["status"] == "below_bar"


def test_failed_gate_returns_to_builder_before_critic():
    out = run("gate_fail_fix")
    assert out["labels"] == ["build:D#a0", "gate:D#a0", "fix:D#a1", "gate:D#a1", "critic:D#a1"]
    d = style(out["result"], "D")
    assert d["status"] == "shipped" and d["gateFixes"] == 1 and d["gatePassed"] is True
    fix = next(c for c in out["calls"] if c["label"] == "fix:D#a1")
    assert "dead_frames [black]: black 3.0-4.0 s" in fix["prompt"]


def test_gate_still_failing_caps_the_score_at_six_then_revises():
    out = run("gate_fail_cap")
    assert out["labels"] == [
        "build:D#a0", "gate:D#a0", "fix:D#a1", "gate:D#a1", "critic:D#a1",
        "revise:D#a2", "gate:D#a2", "critic:D#a2",
    ]
    d = style(out["result"], "D")
    assert d["scores"][0] == {"attempt": 1, "raw": 8, "score": 6, "gatePassed": False}
    assert d["scores"][1] == {"attempt": 2, "raw": 8, "score": 8, "gatePassed": True}
    assert d["status"] == "shipped"
    revise = next(c for c in out["calls"] if c["label"] == "revise:D#a2")
    assert "scored attempt a1 6/10" in revise["prompt"]
    assert "PREFLIGHT FAILURES STILL OPEN: dead_frames [black]" in revise["prompt"]


def test_gate_claiming_pass_with_a_missing_check_is_a_failure():
    # planted negative: the preflight agent says passed:true but legibility never ran
    out = run("gate_lies")
    assert out["labels"][:3] == ["build:D#a0", "gate:D#a0", "fix:D#a1"]


def test_preflight_runner_error_stops_the_style_without_a_critic():
    out = run("gate_runner_error")
    assert out["labels"] == ["build:D#a0", "gate:D#a0"]
    d = style(out["result"], "D")
    assert d["status"] == "error" and "exit 2: no such file" in d["error"]
    assert out["result"]["ok"] is False


def test_build_failure_is_reported_not_scored():
    out = run("build_fails")
    assert out["labels"] == ["build:D#a0"]
    assert style(out["result"], "D")["status"] == "error"


# ---------------------------------------------------------------- multiple styles, order, journal

def test_sequential_and_parallel_reach_the_same_outcome():
    seq = result("multi_sequential")
    par = result("multi_parallel")
    for res in (seq, par):
        assert [(r["key"], r["status"], r["score"], r["revisions"]) for r in res["results"]] == [
            ("D", "shipped", 7, 0), ("E", "shipped", 7, 1), ("K", "below_bar", 6, 2)]
    # sequential: one style at a time, so D finishes before E starts
    labels = run("multi_sequential")["labels"]
    assert labels.index("critic:D#a0") < labels.index("build:E#a0")
    # parallel: lockstep rounds in style order
    assert run("multi_parallel")["labels"][:3] == ["build:D#a0", "build:E#a0", "build:K#a0"]


def test_call_order_does_not_depend_on_completion_order():
    out = run("order_invariance")
    assert out["a"] == out["b"], "issued key sequence changed with completion timing"
    assert out["labelsA"] == out["labelsB"]


def test_prompts_are_deterministic():
    out = run("deterministic_prompts")
    # build, gate (fail), fix, gate, critic 5, revise, gate, critic 8
    assert out["same"] is True and out["n"] == 8


def test_journal_records_exact_tokens_and_time_for_sequential_steps():
    out = run("ship_after_2")
    res = out["result"]
    j = res["journal"]
    assert [r["label"] for r in j] == out["labels"]
    for r in j:
        for f in ("seq", "round", "style", "step", "attempt", "label", "model", "effort", "t0", "t1", "wallS", "tokens", "tokensMode", "pooledCalls", "cachedLikely", "resumed", "outcome"):
            assert f in r, f
        assert r["tokensMode"] == "exact" and r["tokens"] == TOKENS[r["step"]]
        assert r["wallS"] == 10 and r["t1"] - r["t0"] == 10
        assert r["outcome"] == "ok" and r["cachedLikely"] is False
    assert res["usage"]["tokens"] == sum(r["tokens"] for r in j)
    assert res["usage"]["tokensByStepExact"] == {"build": 1000, "gate": 300, "critic": 1500, "revise": 1600}
    assert res["usage"]["stepWallS"] == 90
    journal_logs = [json.loads(line[len("journal "):]) for line in out["logs"] if line.startswith("journal ")]
    assert journal_logs == j
    assert any(line.startswith("usage ") for line in out["logs"])


def test_journal_labels_parallel_tokens_as_pooled():
    res = result("multi_parallel")
    first_round = [r for r in res["journal"] if r["round"] == 1]
    assert [r["label"] for r in first_round] == ["build:D#a0", "build:E#a0", "build:K#a0"]
    assert all(r["tokensMode"] == "pooled" and r["pooledCalls"] == 3 and r["tokens"] == 3000 for r in first_round)
    # once only K is left, its steps are exact again
    tail = [r for r in res["journal"] if r["label"] == "critic:K#a2"]
    assert tail and tail[0]["tokensMode"] == "exact"


# ---------------------------------------------------------------- resume

def test_resume_after_kill_reruns_only_unfinished_steps_sequential():
    out = run("resume_sequential")
    done1 = out["completedInRun1"]
    assert done1 == ["build:D#a0", "gate:D#a0", "critic:D#a0", "revise:D#a1", "gate:D#a1", "critic:D#a1"]
    assert out["run2"]["cached"] == done1
    assert set(out["run2"]["works"]).isdisjoint(done1), "a completed step was redone on resume"
    assert out["run2"]["works"] == ["revise:D#a2", "gate:D#a2", "critic:D#a2"]
    assert out["run2"]["keys"][: len(out["run1"]["keys"])] == out["run1"]["keys"]
    assert style(out["run2"]["result"], "D")["status"] == "shipped"


def test_resume_mid_parallel_round_uses_receipts_not_rework():
    out = run("resume_parallel")
    done1 = out["completedInRun1"]
    assert "revise:E#a1" in done1 and "revise:K#a1" in done1 and "revise:D#a1" not in done1
    assert set(out["run2"]["works"]).isdisjoint(done1), "a completed step was redone on resume"
    assert out["run2"]["receiptHits"] == ["revise:E#a1", "revise:K#a1"]
    assert [r["status"] for r in out["run2"]["result"]["results"]] == ["shipped"] * 3
    resumed = [r["label"] for r in out["run2"]["result"]["journal"] if r["resumed"]]
    assert resumed == ["revise:E#a1", "revise:K#a1"]


def test_receipt_keys_are_distinct_per_step():
    every = []
    for sc in ("ship_after_2", "multi_parallel", "full_mode"):
        keys = []
        for c in run(sc)["calls"]:
            m = re.search(r"^RESUME RECEIPT: \S+\.([0-9a-f]{16})\.json$", c["prompt"], re.M)
            assert m, c["label"]
            keys.append(m.group(1))
        assert len(set(keys)) == len(keys), sc  # within one run every step has its own receipt
        every += keys
    distinct = set(every)
    assert len({k[-4:] for k in distinct}) > len(distinct) // 2, "hash lane collapsed"


def test_without_receipts_the_mid_round_gap_reworks_steps():
    # planted negative: shows the cache-prefix gap the receipts close
    out = run("resume_parallel_no_receipts")
    redone = sorted(set(out["run2"]["works"]) & set(out["completedInRun1"]))
    assert redone == ["revise:E#a1", "revise:K#a1"]


@pytest.mark.studio(V1_REL)
def test_v1_resume_failure_is_reproduced_by_the_harness():
    # planted reproduction of an observed v1 resume failure: critics cached, revise:R completed but re-ran
    assert V1 is not None and V1.exists()
    out = run("v1_repro")
    assert "revise:R" in out["completedInRun1"]
    assert out["run2"]["cached"] == ["prep:sim", "build:C", "build:D", "build:E", "build:R",
                                     "critic:C", "critic:D", "critic:E", "critic:R"]
    assert "revise:R" in out["run2"]["works"]


# ---------------------------------------------------------------- prompt policy

POLICY_SCENARIOS = ["ship_after_2", "gate_fail_cap", "multi_parallel", "full_mode"]


@pytest.mark.parametrize("scenario", POLICY_SCENARIOS)
def test_every_call_follows_the_model_effort_and_rule_policy(scenario):
    calls = run(scenario)["calls"]
    assert calls
    assert policy_violations(calls) == []


def test_policy_check_flags_an_altered_rule_line():
    # planted negative: one of the three verbatim lines reworded
    out = run("rules_mutated")
    assert any("missing rule line 'Never silence stderr.'" in v for v in policy_violations(out["calls"]))


def test_roles_use_the_required_models():
    roles = {}
    for sc in POLICY_SCENARIOS:
        for c in run(sc)["calls"]:
            roles.setdefault(c["label"].split(":")[0], set()).add((c["model"], c["effort"]))
    assert roles["critic"] == {("opus", "medium")}
    assert roles["revise"] == {("opus", "medium")}
    assert roles["plan"] == {("opus", "high")}
    assert roles["gate"] == {("sonnet", "low")}
    assert roles["fix"] == {("opus", "medium")}
    assert roles["build"] == {("sonnet", "medium")}


def test_critic_prompt_points_at_rubric_and_evidence():
    out = run("gate_fail_cap")
    critic = next(c for c in out["calls"] if c["label"] == "critic:D#a1")
    rubric = PLUGIN / "critic" / "rubric.md"
    assert rubric.exists()
    assert f"Read {rubric} in full" in critic["prompt"]
    assert "/sim/gate:D#a1/preflight_report.json" in critic["prompt"]
    assert "every PNG listed in /sim/gate:D#a1/sheets.json" in critic["prompt"]
    assert "You did NOT build it." in critic["prompt"]
    assert "did fix:D#a1" not in critic["prompt"], "the critic must not see the builder's claims"
    revise = next(c for c in out["calls"] if c["label"] == "revise:D#a2")
    assert "did fix:D#a1" in revise["prompt"]  # control: the notes exist and reach the builder side


def _gate_argv(prompt: str) -> list[str]:
    line = next(x for x in prompt.split("\n") if x.startswith("python3 "))
    return shlex.split(line)


def test_gate_command_runs_all_four_preflight_clis():
    out = run("ship_first")
    gate = next(c for c in out["calls"] if c["label"] == "gate:D#a0")
    argv = _gate_argv(gate["prompt"])
    assert argv[:5] == ["python3", str(PLUGIN / "evidence" / "evidence.py"), "report", "/sim/build:D#a0.mp4", "--out"]
    assert argv[5] == "/sim/out/sim-D/a0/evidence"
    assert argv[6:8] == ["--words", "/sim/in/S49-cut15.words.json"]
    runs = [argv[i + 1] for i, a in enumerate(argv) if a == "--run"]
    parsed = [shlex.split(r) for r in runs]
    assert parsed == [
        ["dead_frames", "{video}"],
        ["endcard", "{video}", "/sim/in/S49-cut15.words.json"],
        ["loudness", "{video}", "--voice", "/sim/build:D#a0/voice_norm.wav", "--sfx", "/sim/build:D#a0/sfx_bus.wav"],
        ["legibility", "/sim/build:D#a0/project"],
    ]
    for name in ("dead_frames", "endcard", "loudness", "legibility"):
        assert (PLUGIN / "preflight" / f"{name}.py").exists()


def test_build_prompt_names_template_ports_and_render_rules():
    out = run("multi_parallel")
    k = next(c for c in out["calls"] if c["label"] == "build:K#a0")
    p = k["prompt"]
    assert f"{PLUGIN}/styles/K/build.py --wav /sim/in/S49-cut15.wav --words /sim/in/S49-cut15.words.json --out /sim/out/sim-K/a0/build --brand /sim/brand.json" in p
    assert "use port 3340" in p  # base 3300 + index 2 * 20
    assert "-w 2" in p and "Never -w auto" in p and "never /dev/null" in p
    assert "RESUME RECEIPT: /sim/out/sim-K/receipts/a0.build." in p
    for key in "ABCDEFGHIJKLMNOPQRST":
        assert (PLUGIN / "styles" / key / "build.py").exists(), key


# ---------------------------------------------------------------- full mode

def test_full_mode_plans_once_then_builds_segments():
    out = run("full_mode")
    assert out["labels"] == [
        "plan:simfull", "segment:D#a0.s0", "segment:D#a0.s1", "segment:D#a0.s2", "assemble:D#a0",
        "gate:D#a0", "critic:D#a0", "revise:D#a1", "gate:D#a1", "critic:D#a1",
    ]
    plan = out["calls"][0]
    assert plan["model"] == "opus" and plan["effort"] == "high"
    seg2 = next(c for c in out["calls"] if c["label"] == "segment:D#a0.s2")
    assert "This is the final segment: keep its end card." in seg2["prompt"]
    seg0 = next(c for c in out["calls"] if c["label"] == "segment:D#a0.s0")
    assert "trim the render to exactly 20 s" in seg0["prompt"]
    gate = next(c for c in out["calls"] if c["label"] == "gate:D#a0")
    runs = [shlex.split(r) for r in [a for i, a in enumerate(_gate_argv(gate["prompt"])) if i and _gate_argv(gate["prompt"])[i - 1] == "--run"]]
    assert [r[0] for r in runs].count("legibility") == 3
    assert ["loudness", "{video}"] in runs
    revise = next(c for c in out["calls"] if c["label"] == "revise:D#a1")
    assert "SEGMENTS (full mode): #0 0-20 s /sim/seg0.mp4" in revise["prompt"]
    res = out["result"]
    assert res["plan"]["segments"][2]["t_end"] == 60 and style(res, "D")["status"] == "shipped"


def test_full_mode_rejects_a_plan_with_a_gap():
    out = run("full_bad_plan")
    assert out["labels"] == ["plan:simfull"]
    assert out["result"]["ok"] is False
    assert out["result"]["fail"] == "Plan FAIL: segment 1 starts at 25, expected 20"
