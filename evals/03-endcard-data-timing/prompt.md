---
description: Composition timing derived from word timings (r2-15s fixture) for a style D build. The plugin's end-card rule (0.1 s gap after the last word, 2.0 s hold, end rounded up to a 30 fps frame) fixes the root data-duration at 16.4 s and the end-card cut at 14.38669 s.
tags: [core-path, beats, data-timing, knowledge]
runs: 3
max_turns: 20
timeout_seconds: 420
allowed_tools: [Read, Glob, Grep, Skill]
---

I'm about to build a style D Shorts composition from this narration. It is a look run, not for publishing. The narration WAV is exactly 14.3 s long and is not attached here. Its word timings (`r2-15s.words.json`) are below, complete.

Before I build, tell me the exact timing values the style D build will write into the HyperFrames composition for this narration:

- the root composition element's `data-start` and `data-duration`,
- when the end card starts and how long it holds,
- the `data-duration` of the narration `<audio>` clip,
- the beat (act) boundaries, if you can get them exactly.

Use the plugin's own end-card rule and build logic, not estimates. Do not build or render anything; just report the values and how each one follows from the word timings.

```json
[{"id":"w0","text":"In","start":0.133,"end":0.16},{"id":"w1","text":"this","start":0.24,"end":0.32},{"id":"w2","text":"short,","start":0.4,"end":0.96},{"id":"w3","text":"I","start":1.0,"end":1.04},{"id":"w4","text":"want","start":1.12,"end":1.2},{"id":"w5","text":"to","start":1.28,"end":1.36},{"id":"w6","text":"walk","start":1.44,"end":1.6},{"id":"w7","text":"through","start":1.68,"end":1.76},{"id":"w8","text":"a","start":1.84,"end":1.92},{"id":"w9","text":"failure","start":2.08,"end":2.24},{"id":"w10","text":"mode","start":2.32,"end":2.48},{"id":"w11","text":"that","start":2.56,"end":2.64},{"id":"w12","text":"worries","start":2.72,"end":2.96},{"id":"w13","text":"me","start":3.04,"end":3.28},{"id":"w14","text":"more","start":3.4,"end":3.52},{"id":"w15","text":"than","start":3.6,"end":3.68},{"id":"w16","text":"sloppy","start":3.76,"end":4.08},{"id":"w17","text":"agents.","start":4.16,"end":5.12},{"id":"w18","text":"It's","start":5.16,"end":5.28},{"id":"w19","text":"the","start":5.4,"end":5.52},{"id":"w20","text":"rigorous","start":5.6,"end":5.92},{"id":"w21","text":"agent","start":6.0,"end":6.32},{"id":"w22","text":"run","start":6.44,"end":6.56},{"id":"w23","text":"that","start":6.64,"end":6.72},{"id":"w24","text":"does","start":6.84,"end":6.96},{"id":"w25","text":"exactly","start":7.04,"end":7.6},{"id":"w26","text":"what","start":7.68,"end":7.76},{"id":"w27","text":"the","start":7.8,"end":7.84},{"id":"w28","text":"plan","start":7.92,"end":8.16},{"id":"w29","text":"says,","start":8.24,"end":8.8},{"id":"w30","text":"passes","start":9.04,"end":9.28},{"id":"w31","text":"every","start":9.36,"end":9.6},{"id":"w32","text":"check,","start":9.76,"end":10.16},{"id":"w33","text":"and","start":10.28,"end":10.4},{"id":"w34","text":"still","start":10.56,"end":10.72},{"id":"w35","text":"leaves","start":10.76,"end":10.96},{"id":"w36","text":"the","start":11.0,"end":11.04},{"id":"w37","text":"project","start":11.12,"end":11.44},{"id":"w38","text":"worse","start":11.52,"end":11.76},{"id":"w39","text":"off.","start":11.84,"end":12.207007},{"id":"w40","text":"Here's","start":12.28669,"end":12.44669},{"id":"w41","text":"the","start":12.52669,"end":12.60669},{"id":"w42","text":"setup","start":12.76669,"end":12.92669},{"id":"w43","text":"in","start":13.00669,"end":13.08669},{"id":"w44","text":"plain","start":13.20669,"end":13.32669},{"id":"w45","text":"terms.","start":13.40669,"end":14.28669}]
```
