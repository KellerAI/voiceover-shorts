# voiceover-shorts

- **Version:** 0.2.0
- **License:** see `LICENSE`; third-party components are listed in `NOTICE`

---

voiceover-shorts is a look-dev system for short vertical videos built off a voiceover:
20 styles (A-T), preflight gates, a calibrated critic, evals, feedback collection,
and the look-dev workflow that builds, gates, scores and revises renders.

## Install

```bash
/plugin install voiceover-shorts --marketplace KellerAI/voiceover-shorts
```

From a shell the equivalent is `claude plugin install voiceover-shorts --marketplace KellerAI/voiceover-shorts`.
`claude plugin install --help` shows `--marketplace <source>` taking `owner/repo`, a git or https URL, or a path, with a bare plugin name as the argument.

## Layout

| Path | Purpose |
| --- | --- |
| `styles/` | The 20 style definitions (A-T) and the `styles.json` registry |
| `preflight/` | Gate modules with a `check(...)` API and a `--json` CLI (see below) |
| `evidence/` | Evidence capture and binding for renders and checks |
| `critic/` | The calibrated critic rubric |
| `evals/` | Evals for preflight gates and the critic |
| `feedback/` | Feedback collection and the calibration set |
| `review/` | Review page tooling |
| `kit/` | Shared kit: end card, SFX bus, sandbox profile, brand assets, fonts, vendored GSAP |
| `scripts/` | `pick_cut.py` (cut selection) and `slot_guard.py` (render-slot check) |
| `workflows/` | The look-dev workflow (`lookdev.js`) |
| `skills/` | The skill that drives the workflow |
| `tests/` | pytest suites; each test file sets its own `sys.path` |

Each `preflight/` gate exposes `check(...)` returning name, passed, findings, and metrics.
Its `--json` CLI exits 0 on pass, 1 on fail, and 2 on a usage or IO error.

## Configuration

The look-dev workflow and the slot guard take their locations from arguments and environment variables.

| Variable | Used by | Meaning |
| --- | --- | --- |
| `SHORTS_STUDIO_DIR` | `workflows/lookdev.js` (as `args.studioRoot`), `feedback/feedback.py`, `evals/eval_critic.py`, `critic/rubric.md` | The studio directory that holds look-dev and critic data: the feedback file at `_kit/lookdev/share/Review/Shorts-Feedback.md`, the registry's `lookdev_root`, and the optional reference reel at `_kit/lookdev/inputs/references/`. Required, no default; unset fails closed. |
| `SHORTS_SLOT_PATH` | `scripts/slot_guard.py` | Path of the render-slot JSON file. No default. Unset or empty means no slot is held, so real renders and render tests skip. The file is only read, never written. |
| `SHORTS_RENDER_SLOT` | `scripts/slot_guard.py` | Holder name to match against the slot file when `--holder` is not given. Unset means not held. |

`SHORTS_STUDIO_DIR` fails closed: `feedback.py` exits 2 when a command needs it and it is unset
(`feedback/feedback.py:117-132`), and `eval_critic.py` raises an error (`evals/eval_critic.py:167`).

The Workflow runtime gives the script no environment, so the caller passes resolved paths as arguments.
`workflows/lookdev.js:200-207` requires all three and throws when any is missing:

- `pluginRoot`: the plugin directory, `${CLAUDE_PLUGIN_ROOT}`.
- `studioRoot`: the value of `SHORTS_STUDIO_DIR`.
- `outRoot`: an absolute output directory outside the plugin, such as `${CLAUDE_PLUGIN_DATA}/lookdev-v2`.

User ratings and calibration live under `${CLAUDE_PLUGIN_DATA}/feedback/` (`ratings.jsonl`, append-only, and `calibration.json`).
The first write there copies in the shipped `feedback/` files, which are read-only seeds (`feedback/feedback.py:203-219`).
Writes (`collect`, `seed`, `ingest`) fail closed with exit 2 when `CLAUDE_PLUGIN_DATA` is unset and no `--store` or `--out` is given, and they refuse any target inside the plugin root (`feedback/feedback.py:165-184`).
Pass `--store PATH` (and `--out PATH` for `ingest`) to `feedback.py` to keep them elsewhere.
Reads use the user file when it exists, else the shipped seed.

## Plugin components

The plugin ships one skill, `skills/shorts-studio/`, which triggers on intent and drives the workflow.

| Component | Included | Reason |
| --- | --- | --- |
| Skills | Yes | `shorts-studio` runs the cut, the look-dev workflow and feedback collection. |
| Agents | No | The workflow and the eval script run the critic; a separate agent would duplicate it. |
| Commands | No | The skill triggers on intent. |
| Hooks | No | Preflight checks are explicit pipeline steps, not tool-event hooks. [OpenAI's plugin directory](https://developers.openai.com/codex/submit-plugins) also rejects packages with lifecycle hooks. |
| `bin/` | No | It would block claude.ai and Cowork installs ([manifest reference](https://code.claude.com/docs/en/plugins/manifest-reference.md)). |
| MCP servers | No | None needed. |

## Narration input

This plugin does not generate voice.
It needs a WAV plus a matching `<name>.words.json` (schema: `tests/fixtures/r2-15s.words.json`).
A separate, optional plugin, `voiceover-shorts-tts`, can produce both from text through a third-party text-to-speech API.
It lives on this repository's `tts-addon` branch and is not part of this plugin.

## Evals

The `evals/` directory holds four skill eval cases. Run them without publishing:

```bash
claude plugin eval . --judge-model sonnet --threshold 0.82 --no-publish
```

LLM-judged graders use a 2-of-3 vote, and 0.82 tolerates one judge flake per case while still failing any case where a grader fails every run.

| Case | Checks |
| --- | --- |
| `01-style-pick-ui-publish` | Style pick for a UI piece, the font-clearance publish filter and fallback |
| `02-look-cut-from-words` | Cut selection from word timings and the look-mode workflow args |
| `03-endcard-data-timing` | End-card start and root `data-duration` derived from the narration |
| `04-no-fire-press-blurb` | The skill does not fire for an unrelated press blurb |

Results go to `evals/results/`, which `.gitignore` excludes.
`evals/eval_critic.py` separately scores the critic against the calibration set in `feedback/calibration.json`.

## License

MIT with a no-training rider: the code and docs may not be used as AI or ML training data.
Everything else MIT allows stays permitted, including running the plugin with AI agents and evaluating it.
See `LICENSE` for the full text and `NOTICE` for third-party components.

## Third-party components

GSAP is vendored at `kit/vendor/gsap.min.js` and redistributed unmodified under the
GSAP Standard License, with its license header kept intact.
The bundled fonts keep their own licenses.
`NOTICE` lists every third-party component, its copyright and its license.

## Fonts

Builds use only bundled open-licensed fonts:
Virgil and Lilita One (OFL 1.1) and Comic Shanns (MIT, see `kit/fonts/LICENSE-ComicShanns.txt`).
Unlicensed fonts (Comic Sans, Marker Felt, Chalkduster, SF Pro, SF Mono) are swapped out.
The registry blocks publishing for uncleared fonts.
