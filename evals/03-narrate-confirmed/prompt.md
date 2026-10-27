---
description: Confirmed spend. The user asks for a narration WAV of a 151-character script and confirms the spend for that exact text and character count up front. The narrate skill must call the free estimate first, then call synthesize exactly once with the text unchanged, confirm_spend true and max_chars between 151 and the 1500 ceiling, and report the WAV path, words.json path and duration from the synthesize result. No retry. The case-level mocks pin chars (151), the ceiling (1500) and the result (durationSec 9.84); the synthesize mock aborts the run if the text was changed.
tags: [core-path, narrate, spend-confirmed]
runs: 3
max_turns: 12
timeout_seconds: 300
allowed_tools: [Read, Glob, Grep, Skill]
---

I need a voiceover for my YouTube Short. Please narrate the script below with my ElevenLabs voice so I get the WAV and the word timings for shorts-studio. Use the text exactly as written:

> Most outages start small. A config flag flips, a cache warms too slowly, and nobody notices until the queue backs up. Here is how we caught ours early.

It is 151 characters. I confirm the spend now: yes, synthesize these 151 characters with ElevenLabs, billed to my account. This confirmation is for this exact text only, so you don't need to ask me again. Run the free estimate first, and if it says 151 characters and within my ceiling, go ahead.
