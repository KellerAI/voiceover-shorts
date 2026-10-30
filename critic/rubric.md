# Shorts Studio Critic Rubric

- **Used by:** the critic step in the look-dev workflow (`workflows/lookdev.js`) and the critic eval (`evals/`).
- **Calibrated on:** the owner's recorded feedback in `Shorts-Feedback.md` (path under Sources).
- **Reel role:** the reference reel anchors the 9 point only.

---

## Purpose

This rubric gives the critic fixed score anchors and a fixed list of must-fix categories.
The earlier critic had neither.
It scored every render "1-10 versus the reference reel", told the critic to start from a harsh default, and judged by eye with no QA gates.
Its scores ran 4.5 to 6 and did not match the owner's order.
This rubric drops all three of those instructions.
Score against the anchors below and against the owner's judgments, not against a mood.

## What the critic reads

The critic judges from evidence, not from builder stills alone.

1. `preflight_report.json` from `evidence/evidence.py report`: one entry per preflight check, with findings and timestamps.
2. The beat-window contact sheets in `sheets/` from the same run (4 fps over 2 s at each beat).
3. The narration transcript and word timings, to judge pacing against the voice.
4. The style's registry entry in `styles/styles.json` (content fit, font licence, publishable flag).
5. For a 9 only: the reel stills in the references directory (`reel_contact.png`, `reel_frames/`; path under Sources).
   If that directory is absent, skip the reel comparison and score at most 8.

Treat the builder's rationale and weakness list as claims to check, not as evidence.

## Score anchors

Three anchors are fixed: 5 = usable with fixes, 7 = shippable, 9 = matches the reference reel.
The other points sit between them.

| Score | Anchor | The render... |
| --- | --- | --- |
| 1–3 | Broken | cannot be watched as a short: mostly dead, silent, unreadable, or cut off. |
| 4 | Rework | has a working idea buried under one of the owner's named dislikes. |
| 5 | **Usable with fixes** | works as an idea; one or more must-fix items are open. |
| 6 | Near | has only small items left: polish, or one failed gate on an otherwise shippable render. |
| 7 | **Shippable** | passes every preflight check, has no open must-fix, and shows none of the owner's dislikes. |
| 8 | Strong | is shippable, and its best frames approach the reel. |
| 9 | **Matches the reference reel** | holds up frame for frame beside the reel's stills. |
| 10 | Beyond | exceeds the reel; award only with the owner's agreement. |

### Rules

- **Preflight cap.** Any failed preflight check caps the score at 6, because a failed gate is not shippable.
- **Broken floor.** Score 1–3 only when the render is unwatchable, for example most of its runtime is black or frozen, the narration is missing, or no text is readable at phone size.
  A render that merely fails several gates is not broken; most existing renders fail some gates (see the recurring defects below).
- **Dislikes cost more than polish.** A render showing one of the owner's named dislikes scores below an otherwise equal render whose open items are only polish.
  The named dislikes are a busy frame (G, H) and pacing too fast to read (A).
  Small timing adjustments, such as E's "small speed tweaks to the effects" (`:96`), are polish, not a dislike.
- **The reel decides 9 and above, nothing else.** Do not compare a render to the reel when choosing between 1 and 8.
  Below 9, the owner's judgments are the yardstick.
- **No default tone.** Score what the evidence shows; neither harsh nor generous is a starting point.

## Calibration exemplars

The owner's own words set the order the critic must reproduce.
The critic eval checks this order by Spearman rank agreement, so preserve the order; the owner gave no numbers.

### Ranked leaders

| Style | The owner's words | Cite |
| --- | --- | --- |
| D — Reel style | "Best so far, some alignment tweaks to match the line following in the first segment." | `:78` |
| C — Product UI | "Close second, best when there are actual UI/UX functions to show." | `:64` |
| E — UI + kinetic type | "Cleanest so far, small speed tweaks to the effects" | `:96` |
| K16 — 16-bit arcade | "16 Bit is the best arcade versions so far. They all need to have the subtitles removed though." | `:326` |

The file's own summary ranks the leaders D, then C, then E (`:21`).
K16 leads the arcade variants (K16, K32, K64, Kevo) only; the file does not rank it against D, C or E.

### Named dislikes

| Style | The owner's words | Cite | Dislike |
| --- | --- | --- | --- |
| G — Editorial print | "This one is too busy" | `:124` | busy frame |
| H — Terminal dataviz | "Too much going on but over all style might work with polish" | `:138` | busy frame |
| A — Kinetic type | "Too fast and needs polish but could be used for press release pure data videos" | `:36` | pacing too fast |

The summary states the theme directly: "Clean beats busy" (`:17`).
Every leader except C comes with a requested fix (`:78`, `:96`, `:326`), and no line calls any render ready to ship.
So no exemplar reaches 7; the leaders sit in the "usable with fixes" band, ordered D above C above E.
That band placement is this rubric's reading of the file, not a number the owner gave.

### Where the earlier critic went wrong

It scored G 6.5 (`:120`) above E at 6 (`:92`).
The owner called G "too busy" (`:124`) and E the "Cleanest so far" (`:96`).
A calibrated critic puts E above G.

### What is not calibration

- "Director notes" blocks are Claude's notes, not the owner's; use them as defect examples only.
- Feedback blocks that hold only the reviewer-name placeholder are empty (for example `:337`, `:347`, `:357`).
- Round 3 has no recorded feedback (`:153`), and I through T are pending review (for example `:212`, `:401`).

New ratings arrive through the feedback store (`feedback/feedback.py`).
It appends user ratings to `${CLAUDE_PLUGIN_DATA}/feedback/ratings.jsonl` and rebuilds `${CLAUDE_PLUGIN_DATA}/feedback/calibration.json`; the shipped `feedback/ratings.jsonl` and `feedback/calibration.json` are read-only seeds.
When they do, the calibration set the eval reads changes; this table records the state on 2026-10-22 (`:3`).

## Must-fix categories

Each must-fix item names one category slug, a time range when it has one, and a concrete visual fix.

### Recurring defects (gated by preflight)

The "Seen in" counts come from a review of 12 look-dev renders.
Each has a preflight module; a failing module is a must-fix and triggers the preflight cap.

| Slug | Seen in | Preflight module | Fails when |
| --- | --- | --- | --- |
| `dead_frames` | 12/12 | `preflight/dead_frames.py` | a black or frozen span lasts at least 0.5 s (`min_dur`). |
| `small_type` | 11/12 | `preflight/legibility.py`, kind `small_type` | rendered text is under 44 px at 1080 px width (`legibility.py:54-55`). |
| `end_card` | 10/12 | `preflight/endcard.py` | the card holds under 1.5 s after the last word, cuts to black, or covers the last word. |
| `empty_lower_frame` | 9/12 | `preflight/legibility.py`, kind `empty_lower_third` | no legible text, media or solid block fills y 1280–1920. |
| `loudness` | 8/12 | `preflight/loudness.py` | voice is outside -14 ±1 LUFS, true peak exceeds -1.5 dBTP, or SFX peaks less than 12 dB below the voice peak (`loudness.py:23-26`). |

### Judged by the critic

No preflight module covers these, so the critic must catch them from the contact sheets and the transcript.

| Slug | What to look for | Source |
| --- | --- | --- |
| `busy_frame` | More than one idea or decorative device competing in a frame. | Owner `:124`, `:138`; theme `:17` |
| `pacing` | Words or effects too fast to read; visuals running ahead of or behind the voice. | Owner `:36`, `:96` |
| `alignment` | Type or labels drifting off the element they follow. | Owner `:78` |
| `captions` | Subtitles tracking the narration when the brief does not ask for them. | Owner `:326`; round 7 brief `:391` |
| `content_fit` | Style used for content it does not suit (C without real UI, for example). | Owner `:64`; themes `:19` |
| `safe_area_crop` | Type or jokes cropped at the frame edge. | Director notes `:180`, `:196` |
| `brand` | Missing or token K tile and "Keller AI"; off-palette colours; the old gradient logo. | Brand kit `kit/endcard.js`, `kit/brand/` |
| `font_licence` | An uncleared font (Comic Sans, Marker Felt, Chalkduster, SF Pro, SF Mono) in a render meant to publish. | `styles.json` `font_license`; README "Fonts" |

A `busy_frame` item, or a `pacing` item where words move too fast to read, is one of the owner's named dislikes.
It costs more than a polish item (see Rules).

## Output

The critic returns the shape the workflow schema expects (`CRIT` in `workflows/lookdev.js`): `score`, `mustFix`, `keep`.

- `score`: a number from 1 to 10 that obeys the anchors and the rules above.
- `mustFix`: strings, each starting with a category slug, for example `busy_frame 4.2-6.0 s: cut the halftone layer, keep the headline`.
- `keep`: strengths worth protecting in the revision.

## Sources

`SHORTS_STUDIO_DIR` is the studio directory the plugin is configured with (see README).

- The owner's feedback: `$SHORTS_STUDIO_DIR/_kit/lookdev/share/Review/Shorts-Feedback.md` (every bare `:N` cite in this file points here; other cites name their file).
- Preflight modules, evidence tool and registry: `preflight/`, `evidence/evidence.py` and `styles/styles.json` under this plugin.
- Reference reel: `$SHORTS_STUDIO_DIR/_kit/lookdev/inputs/references/` (optional; without it no render scores 9 or above).
