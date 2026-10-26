---
name: shorts-studio
description: "Pick a Shorts look-dev style from the article content, cut the narration to a 12.5-17.5 s window, then run the shorts-lookdev-v2 workflow (mode look or full) and report scores, preflight results, render paths, usage and time. Also answers plan-only questions without building or rendering: data-start/data-duration values, beats, end-card timing, cut points and style choice."
when_to_use: "Use when the user wants a short-form video look or full render for an article, press release or narration, wants a style chosen for them, or wants to rate the result for the feedback loop."
model: opus
effort: medium
user-invocable: true
argument-hint: "[--mode look|full] [--styles D,E,K] [--publish] [--run-tag TAG] [--wav PATH --words PATH] [article text or path]"
allowed-tools:
  - Workflow
  - Read
  - Glob
  - Grep
  - Bash(python3 *)
---

# shorts-studio

Select a style from the article, prep the narration cut, run the look-dev workflow, and report what came back.

## Plan-only questions

This skill also answers planning and timing questions with no build or render: root `data-start`/`data-duration`, narration clip duration, beat (act) boundaries, end-card start and hold, cut points and style choice. Answer from the rule files; do not run the workflow.

- **End card:** `${CLAUDE_PLUGIN_ROOT}/kit/endcard.js`, `KAEndCard.plan()`: start = last word end + 0.1 s gap, end = max(start, last word end) + 2.0 s hold, rounded up to a whole 30 fps frame. That end is the root `data-duration`.
- **Beats and composition plan:** `build_plan()` in `${CLAUDE_PLUGIN_ROOT}/styles/<X>/build.py` (styles A, B, C, E and K have a `build.py` without `build_plan`; read their build code directly).
- **Cut points:** the module docstring of `${CLAUDE_PLUGIN_ROOT}/scripts/pick_cut.py`.
- **Style choice:** `${CLAUDE_PLUGIN_ROOT}/styles/styles.json`.

## Paths

| Name | Value |
| --- | --- |
| Plugin root | `${CLAUDE_PLUGIN_ROOT}` |
| Studio dir | `$SHORTS_STUDIO_DIR`: the shorts-studio checkout that holds `_kit/lookdev` (the original style directories and `Shorts-Feedback.md`). Required. |
| Output dir | `${CLAUDE_PLUGIN_DATA}`: all cuts, renders, ratings and calibration go here, never inside the plugin. |
| Feedback store | `${CLAUDE_PLUGIN_DATA}/feedback/ratings.jsonl` and `calibration.json`. The shipped `${CLAUDE_PLUGIN_ROOT}/feedback/` files are read-only seeds. |
| Registry | `${CLAUDE_PLUGIN_ROOT}/styles/styles.json` |
| Cut picker | `${CLAUDE_PLUGIN_ROOT}/scripts/pick_cut.py` |
| Workflow | `${CLAUDE_PLUGIN_ROOT}/workflows/lookdev.js` (meta.name `shorts-lookdev-v2`) |
| Feedback CLI | `${CLAUDE_PLUGIN_ROOT}/feedback/feedback.py` |
| Review page | `${CLAUDE_PLUGIN_ROOT}/review/build_review_page.py` |

The registry's `lookdev_root` and `feedback_file` are relative to `$SHORTS_STUDIO_DIR`. If `SHORTS_STUDIO_DIR` is unset, the review page, the critic eval and `feedback.py seed` exit 2 with an error naming the variable. Stop and ask the user to set it; do not guess a path.

## Inputs

- **Article or brief:** the text the video is about. Needed to choose a style unless the user names `--styles`.
- **Narration:** a WAV plus its `<name>.words.json` word timings. If the user has no narration, stop and say so; this skill does not synthesize voice.
- **Mode:** `look` (one 12.5-17.5 s cut, default) or `full` (the whole narration, split into segments).
- **Publishing intent:** ask once whether the output is for publishing. The answer drives the style filter below.

## Step 1: Select a style from the registry

Style choice comes from the registry's `content_fit` tags, not from taste. Skip this step when the user passed `--styles`, but still apply the publishing check.

1. Read `styles/styles.json` and read the article.
2. Pick the tags that describe the article. The tags in the registry today:

   | Tag | Styles carrying it |
   | --- | --- |
   | `press-data` | A, L, O |
   | `workflow`, `time-based` | B, M |
   | `ui` | C |
   | `symbolic` | D, N |
   | `general` | E |
   | `audit` | L |
   | `status` | O |

   Styles F, G, H, I, J, K, P, Q, R, S and T have an empty `content_fit`. Never pick them from content; they run only when the user names them. K means variant K16 in the workflow.
3. Keep styles whose `content_fit` intersects the chosen tags.
4. If the output is for publishing, drop every style where `publishable` is false or `font_license` is not `cleared`. Today that removes C, E, F, G, H, I, P and R. State which styles were dropped and why (the font names are in each style's `fonts.unlicensed`).
5. Rank the survivors by `owner_rank` (1 is best; `null` sorts last). Run the top one, or the top three if the user wants a comparison.
6. If nothing survives, fall back to D (`owner_rank` 1, publishable). Do not fall back to E for publishing: E is not publishable. Tell the user the fallback was used and that no style matched.
7. For look-only, non-publishing runs the user may use unpublishable styles. Say in the report that the render uses unlicensed fonts and must not be published. The workflow does not enforce this; the registry does.

Report the choice as a short table: style key, name, the matching tag, `publishable`.

## Step 2: Run pick_cut.py (look mode only)

The workflow does not cut audio. Run the picker first and pass its stdout JSON line to the workflow as `args.cut`.

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/pick_cut.py \
  --wav SRC.wav --words SRC.words.json --target 15 --lo 12.5 --hi 17.5 \
  --out-wav X.wav --out-words X.words.json --out-txt X.txt
```

- Put the `X.*` outputs under `${CLAUDE_PLUGIN_DATA}/cuts/<runTag>-cut/`, never inside the plugin.
- Parse the single JSON line on stdout. Expected shape: `{status:'OK', cut, wav, words, transcript, durationSec, lastWordEnd}`.
- If `status` is `FAIL`, a field is missing, or `durationSec` is outside 12.5-17.5, stop and report the picker output verbatim. The workflow rejects these before any agent runs, so retrying with the same cut wastes nothing but also fixes nothing.
- In `full` mode skip this step and omit `cut`.

## Step 3: Call the Workflow tool

Call `Workflow` with `scriptPath` set to `${CLAUDE_PLUGIN_ROOT}/workflows/lookdev.js` and these `args`.

| Arg | Rule |
| --- | --- |
| `mode` | `'look'` or `'full'`. Required. |
| `styles` | `'D'` or `['D','E','K']`. Keys A-T. Required. |
| `runTag` | `[A-Za-z0-9._-]+`. Required. Use a unique tag per run; nothing is overwritten. |
| `cut` | Look mode: the picker JSON from Step 2. Must be absent in full mode. |
| `wav`, `words` | Full mode: absolute paths of the whole narration. Look mode: optional; if given they must equal `cut.wav` and `cut.words`. |
| `transcript` | Full mode, optional. |
| `brand` | Optional absolute path to a brand JSON. |
| `ports` | Optional. Number base, `{base}`, or `{KEY: port}`. Default base 3180. |
| `pluginRoot` | Required. The value of `${CLAUDE_PLUGIN_ROOT}`, absolute. |
| `studioRoot` | Required. The value of `$SHORTS_STUDIO_DIR`, absolute. |
| `outRoot` | Required. Absolute and outside the plugin: `${CLAUDE_PLUGIN_DATA}/lookdev-v2`. |
| `parallel` | Default `false` (sequential, exact per-step tokens, one render at a time). `true` runs lockstep rounds. |
| `maxRevise` | 0-2, default 2. |
| `maxGateFixes` | 0-2, default 1. |
| `quality` | `draft`, `looks`, `delivery`, `standard` or `high`. Default `looks`. |
| `maxSegments` | 2-16, default 10 (full mode). |

Per style the workflow builds, gates (preflight), fixes up to `maxGateFixes`, critiques, then revises while the score is under 7 and revisions remain. A failed gate caps the score at 6. Worst case is 15 agent calls per style, so state the style count and mode to the user before starting a multi-style run.

## Step 4: Report the result

The workflow returns `{ok, version:2, mode, runTag, runDir, clip, plan, results, journal, usage, fail?}`. If `ok` is false, report `fail` verbatim (`Prep FAIL: ...` or `Plan FAIL: ...`) and stop.

For each entry in `results`, report:

- **Outcome:** `key`, `status` (`shipped`, `below_bar` or `error`), final `score`, `rawScore`, `gatePassed`, `revisions`, `gateFixes`, and the `scores` history per attempt.
- **Preflight:** the gate result. The full report is `<attemptDir>/evidence/preflight_report.json`; quote the failing checks, not just pass or fail.
- **Renders:** `mp4` plus `attemptDir`. Attempt directories are `a0`, `a1`, ... and none is overwritten.
- **Critic notes:** `mustFix` and `keep`.
- **Error:** the `error` string if status is `error`.

Then report cost and time from `usage`: `tokens`, `tokensByStepExact`, `pooledRounds`, `agentWallS`, `stepWallS`, `calls`. If `parallel` was true, say token counts are pooled per round rather than exact per step.

State what was not verified: scores come from the critic agent, not from a human. A `shipped` status means the score reached 7 with a passing gate; it does not mean the video is cleared to publish. Publishing also needs the style's `publishable` flag to be true.

## Step 5: Feedback loop

Human ratings calibrate the critic. Offer this after every run and do not write ratings on the user's behalf.

1. Build a review page for the finished renders. VIDEO paths are relative to the registry's `lookdev_root`:

   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT}/review/build_review_page.py build \
     --render D=<video-relative-to-lookdev_root> --out PAGE.html
   ```

   Use `STYLE/VARIANT=VIDEO` for variants, for example `K/K16=...`. The page cohort defaults to `K-variants` for K variants and `global` otherwise.
2. The user scores renders in the page and clicks "Export JSON". Then append the rows to the user store. `--store` defaults to `${CLAUDE_PLUGIN_DATA}/feedback/ratings.jsonl`; pass it only to write somewhere else (outside the plugin root).

   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT}/review/build_review_page.py submit EXPORT.json \
     --store ${CLAUDE_PLUGIN_DATA}/feedback/ratings.jsonl
   ```

3. A single rating can be added directly:

   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT}/feedback/feedback.py collect --style D --video <path> --rater <name> --score 8
   ```

   Rate K variants with `--variant K16 --cohort K-variants`, otherwise the row lands in `global` and is ranked against D, C and E.
4. Rebuild the calibration set with `python3 ${CLAUDE_PLUGIN_ROOT}/feedback/feedback.py ingest`. It writes `${CLAUDE_PLUGIN_DATA}/feedback/calibration.json` (`cohorts["global"]["order"]` and `["ranks"]`). The critic eval reads the user's `${CLAUDE_PLUGIN_DATA}/feedback/calibration.json` when it exists, else the shipped seed `feedback/calibration.json`; pass `--calibration PATH` to override.

Store: `${CLAUDE_PLUGIN_DATA}/feedback/ratings.jsonl`, append-only. The first write (`collect`, `ingest`, or `submit` to that path) copies the shipped seeds `${CLAUDE_PLUGIN_ROOT}/feedback/ratings.jsonl` and `calibration.json` into `${CLAUDE_PLUGIN_DATA}/feedback/`; the shipped files are read-only and never written. If `CLAUDE_PLUGIN_DATA` is unset, `collect`, `ingest` and `seed` exit 2 unless given an explicit `--store` (and `--out` for `ingest`) outside the plugin root. Stop and ask the user where ratings should go; do not point them at the plugin. A malformed row makes the whole store unreadable (exit 2), so never edit it by hand.

## Rules

- Do not delete files. Every run writes to a new `runTag`.
- Do not write into the plugin root (`${CLAUDE_PLUGIN_ROOT}`) or the original style directories. Renders and cuts go under `outRoot`; ratings and calibration go under `${CLAUDE_PLUGIN_DATA}/feedback/`.
- Never claim a render exists without reading its path from the workflow result.
- A failed workflow, a `below_bar` style or a failed gate is reported as such. Do not retry with a weaker setting to get a green result.
