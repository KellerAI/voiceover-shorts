# AGENTS.md

## Purpose

A look-dev system for short vertical videos built off a voiceover: 20 styles (A-T), preflight gates, a calibrated critic, evals, feedback collection, and the look-dev workflow. Packaged as a Claude Code plugin. Details are in `README.md`.

## Repo layout

- `styles/` - the 20 style definitions and the `styles.json` registry
- `preflight/` - gate modules with a `check(...)` API and a `--json` CLI
- `evidence/`, `critic/`, `evals/`, `feedback/`, `review/`
- `kit/` - shared kit (end card, SFX bus, sandbox profile, brand assets, fonts, vendored GSAP)
- `scripts/` - `pick_cut.py`, `slot_guard.py`
- `workflows/lookdev.js` - the look-dev workflow
- `skills/shorts-studio/` - the skill that drives the workflow
- `tests/` - pytest suites
- `.claude-plugin/` - plugin and marketplace manifests

## Setup

```bash
claude plugin install voiceover-shorts --marketplace KellerAI/voiceover-shorts
```

Required configuration (`SHORTS_STUDIO_DIR`, `SHORTS_SLOT_PATH`, `SHORTS_RENDER_SLOT`, `CLAUDE_PLUGIN_DATA`) is described in `README.md`.

## Build/test/lint commands

```bash
claude plugin eval . --judge-model sonnet --threshold 0.82 --no-publish   # skill evals
```

Tests are pytest suites under `tests/` (each test file sets its own `sys.path`); see `README.md`. No CI workflow or lint command is defined in the repo.

## Conventions

- Agent-authored PRs are opened from the `son-of-anton-ai` account.
- Every PR body states what changed, why, and the test evidence (the commands run and their results).
- Link the Linear issue the change belongs to.
- Never commit secrets, tokens, keys or `.env` files.
- `AGENTS.md` is the only agent-instructions file in this repo. Do not add `CLAUDE.md` or other tool-specific instruction files.
- Builds use only the bundled open-licensed fonts; the registry blocks publishing for uncleared fonts.
- The optional text-to-speech plugin lives on the `tts-addon` branch and is not part of this plugin.
