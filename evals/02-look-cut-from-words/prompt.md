---
description: Look-mode cut plan from the picker's real output (r2-15s fixture). Tests the plugin rules, not prediction - why the picker cut at "terms." (14.233 s, gap end minus 0.05 s snapped to 30 fps), why it rejected "off." and "agents." against the 12.5-17.5 s look window, and the lookdev workflow args with the picker JSON as args.cut.
tags: [core-path, cut, render-plan, knowledge]
runs: 3
max_turns: 15
timeout_seconds: 300
allowed_tools: [Read, Glob, Grep, Skill]
---

I want a look-mode Shorts render in style D from my narration. It is not for publishing. The narration WAV is exactly 14.3 s long; its word timings are `r2-15s.words.json`. I already ran the plugin's cut picker (`pick_cut.py --target 15 --lo 12.5 --hi 17.5` with the `--out-wav`/`--out-words`/`--out-txt` outputs). This is the JSON line it printed, complete:

```json
{"status": "OK", "cut": 14.233333, "sentenceEndIndex": 45, "sentenceEndWordId": "w45", "sentenceEndText": "terms.", "gapStart": 13.843917, "gapEnd": 14.3, "evaluated": [{"index": 17, "id": "w17", "text": "agents.", "wordStart": 4.16, "wordEnd": 5.12, "gapStart": 4.697625, "gapEnd": 5.188083, "cut": 5.133333, "status": "cut 5.133 outside 12.5-17.5 s"}, {"index": 39, "id": "w39", "text": "off.", "wordStart": 11.84, "wordEnd": 12.207007, "gapStart": 12.056729, "gapEnd": 12.2775, "cut": 12.2, "status": "cut 12.200 outside 12.5-17.5 s"}, {"index": 45, "id": "w45", "text": "terms.", "wordStart": 13.40669, "wordEnd": 14.28669, "gapStart": 13.843917, "gapEnd": 14.3, "cut": 14.233333, "status": "in window"}], "lastWordEnd": 14.233333, "transcript": "In this short, I want to walk through a failure mode that worries me more than sloppy agents. It's the rigorous agent run that does exactly what the plan says, passes every check, and still leaves the project worse off. Here's the setup in plain terms.", "wav": "/home/me/plugin-data/voiceover-shorts/cuts/r2-look-1-cut/r2-look.wav", "durationSec": 14.233333, "samples": 683200, "words": "/home/me/plugin-data/voiceover-shorts/cuts/r2-look-1-cut/r2-look.words.json", "txt": "/home/me/plugin-data/voiceover-shorts/cuts/r2-look-1-cut/r2-look.txt"}
```

Plan only: do not try to cut audio, run a workflow, or render. Tell me:

1. Which sentence end the picker cut at and how it arrived at that exact cut time from the silence gap, and why it rejected each of the other sentence ends.
2. The allowed clip-length window for a look run.
3. The exact arguments you would pass to the look-dev workflow with this picker output.
