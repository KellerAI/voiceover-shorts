---
description: Should-not-fire control. A generic Python coding question with nothing to do with narration, voiceover or text-to-speech. Neither add-on skill (narrate, tts-setup) may fire and no add-on MCP tool (list_voices, estimate, synthesize) may be called. The answer is a correct median function.
tags: [no-fire, control]
runs: 3
max_turns: 5
timeout_seconds: 120
allowed_tools: [Read, Glob, Grep, Skill]
---

Write a Python function `median(values: list[float]) -> float` that returns the median of a non-empty list without changing the caller's list, and raises `ValueError` for an empty list. Show the function and two short example calls with their results: one list with an odd number of items and one with an even number. No other explanation needed.
