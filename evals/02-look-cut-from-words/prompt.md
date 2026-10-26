---
description: Look-mode cut planning from word timings (r2-15s fixture). The plugin's picker cuts in a silence gap after a sentence-final word and keeps the clip inside 12.5-17.5 s; the result feeds the lookdev workflow as args.cut.
tags: [core-path, cut, render-plan, knowledge]
runs: 3
max_turns: 15
timeout_seconds: 300
allowed_tools: [Read, Glob, Grep, Skill]
---

I want a look-mode Shorts render in style D from my narration. It is not for publishing. The narration WAV is exactly 14.3 s long and is not attached to this chat; I will run the cut and the render myself later. The word timings (`r2-15s.words.json`) are below, complete.

Plan only: do not try to cut audio, run a workflow, or render. Tell me:

1. Which sentence end the cut picker will cut at for a look run, and which other sentence ends it rejects and why.
2. The allowed clip-length window for a look run.
3. The exact arguments you would pass to the look-dev workflow once I hand you the picker's output.

```json
[{"id":"w0","text":"In","start":0.133,"end":0.16},{"id":"w1","text":"this","start":0.24,"end":0.32},{"id":"w2","text":"short,","start":0.4,"end":0.96},{"id":"w3","text":"I","start":1.0,"end":1.04},{"id":"w4","text":"want","start":1.12,"end":1.2},{"id":"w5","text":"to","start":1.28,"end":1.36},{"id":"w6","text":"walk","start":1.44,"end":1.6},{"id":"w7","text":"through","start":1.68,"end":1.76},{"id":"w8","text":"a","start":1.84,"end":1.92},{"id":"w9","text":"failure","start":2.08,"end":2.24},{"id":"w10","text":"mode","start":2.32,"end":2.48},{"id":"w11","text":"that","start":2.56,"end":2.64},{"id":"w12","text":"worries","start":2.72,"end":2.96},{"id":"w13","text":"me","start":3.04,"end":3.28},{"id":"w14","text":"more","start":3.4,"end":3.52},{"id":"w15","text":"than","start":3.6,"end":3.68},{"id":"w16","text":"sloppy","start":3.76,"end":4.08},{"id":"w17","text":"agents.","start":4.16,"end":5.12},{"id":"w18","text":"It's","start":5.16,"end":5.28},{"id":"w19","text":"the","start":5.4,"end":5.52},{"id":"w20","text":"rigorous","start":5.6,"end":5.92},{"id":"w21","text":"agent","start":6.0,"end":6.32},{"id":"w22","text":"run","start":6.44,"end":6.56},{"id":"w23","text":"that","start":6.64,"end":6.72},{"id":"w24","text":"does","start":6.84,"end":6.96},{"id":"w25","text":"exactly","start":7.04,"end":7.6},{"id":"w26","text":"what","start":7.68,"end":7.76},{"id":"w27","text":"the","start":7.8,"end":7.84},{"id":"w28","text":"plan","start":7.92,"end":8.16},{"id":"w29","text":"says,","start":8.24,"end":8.8},{"id":"w30","text":"passes","start":9.04,"end":9.28},{"id":"w31","text":"every","start":9.36,"end":9.6},{"id":"w32","text":"check,","start":9.76,"end":10.16},{"id":"w33","text":"and","start":10.28,"end":10.4},{"id":"w34","text":"still","start":10.56,"end":10.72},{"id":"w35","text":"leaves","start":10.76,"end":10.96},{"id":"w36","text":"the","start":11.0,"end":11.04},{"id":"w37","text":"project","start":11.12,"end":11.44},{"id":"w38","text":"worse","start":11.52,"end":11.76},{"id":"w39","text":"off.","start":11.84,"end":12.207007},{"id":"w40","text":"Here's","start":12.28669,"end":12.44669},{"id":"w41","text":"the","start":12.52669,"end":12.60669},{"id":"w42","text":"setup","start":12.76669,"end":12.92669},{"id":"w43","text":"in","start":13.00669,"end":13.08669},{"id":"w44","text":"plain","start":13.20669,"end":13.32669},{"id":"w45","text":"terms.","start":13.40669,"end":14.28669}]
```
