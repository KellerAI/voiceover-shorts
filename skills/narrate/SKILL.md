---
name: narrate
description: "Turn exact narration text into a WAV and word timings (words.json) through the user's own ElevenLabs key, after a free cost estimate and an explicit spend confirmation, then hand off to shorts-studio. Use when the user asks to voice, narrate, read aloud or make a voiceover from text. Do not use when the user only wants a script written or edited."
user-invocable: true
argument-hint: "[narration text or path]"
allowed-tools:
  - Read
  - mcp__plugin_voiceover-shorts-tts_elevenlabs__estimate
  - mcp__plugin_voiceover-shorts-tts_elevenlabs__synthesize
  - mcp__plugin_voiceover-shorts-tts_elevenlabs__list_voices
---

# narrate

Make a narration WAV plus `words.json` from text. Every `synthesize` call is billed to the user, so follow these steps in order.

## Step 1: Get the exact text

Get the exact narration text from the user, inline or as a file path. Do not write, rewrite or trim it yourself. If the user wants a script written first, write it as plain text and stop; narrating it is a separate request.

## Step 2: Estimate (free)

Call `estimate` with the exact `text`.

- If `configured` is `false`, tell the user to run the tts-setup skill and stop.
- Otherwise report: `chars`, `character_cost_multiplier`, `remaining_chars` and `user_ceiling`.
- If `within_ceiling` is `false` or `chars` exceeds `user_ceiling`, say so and stop. Suggest shorter text or a higher `max_chars_per_call` setting.

## Step 3: Confirm the spend

Ask: "Synthesize these <chars> characters with ElevenLabs? This is billed to your account." Proceed only on a clear yes in this conversation for this exact text. A yes given earlier, or for different text, does not count. If the text changes, go back to Step 2.

## Step 4: Synthesize once

Call `synthesize` exactly once with:

| Arg | Value |
| --- | --- |
| `text` | The confirmed text, unchanged |
| `name` | A short safe name matching `[A-Za-z0-9._-]+`, unique per run |
| `confirm_spend` | `true` |
| `max_chars` | The confirmed character count: at least `len(text)`, never above `user_ceiling` |

Omit `out_dir` unless the user gives an absolute path; the default is `${CLAUDE_PLUGIN_DATA}/narration/`.

## Step 5: Never retry

Do not call `synthesize` again in this run. On any error, timeout or unknown outcome, tell the user the request may already have been billed and that they should check their ElevenLabs history before trying again. A new attempt needs a new Step 2 and Step 3.

## Step 6: Report and hand off

Report `wav`, `words`, `durationSec`, `charsSent` and `voiceId`. Then hand off to the shorts-studio skill with the two paths:

```text
--wav <wav> --words <words>
```
