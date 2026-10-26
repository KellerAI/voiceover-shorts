---
type: llm
focus: last_message
weight: 2
---
The user gave word timings for a 14.3 s narration whose last word ends at 14.28669 s, and asked for the timing values a style D build writes into the composition. The correct values, computed by the plugin's own build code, are: root `data-start` 0; root `data-duration` 16.4 s (written as 16.400000); end card starts at 14.38669 s (last word end plus a 0.1 s gap) by a hard cut and holds 2.0 s, with the end rounded up to a whole frame at 30 fps; narration `<audio>` clip `data-duration` equal to the narration's own length (about 14.3 s), not the full composition length; six acts with boundaries 0, 1.0, 5.16, 9.04, 10.28, 12.2867, 14.3867 s.

Pass only if every claim below holds in the final answer:

1. Root `data-start` is 0 and root `data-duration` is 16.4 s.
2. The end card starts at about 14.387 s, 0.1 s after the last word ends, and holds the rule's 2.0 s (`hold: 2.0`). Saying it is on screen for about 2.013 s because the end is rounded up to a whole 30 fps frame is also correct.
3. It explains that 16.4 comes from the end-card start plus the 2.0 s hold, rounded up to a whole 30 fps frame.
4. If it gives the narration `<audio>` clip's `data-duration`, the value is the narration length (about 14.3 s), not 16.4 s.
5. If it lists beat or act boundaries, they are the six acts above (small rounding differences are fine). Leaving them out, or saying they come from the build's phrase-boundary search, is acceptable. Listing different boundaries as exact is a failure.
6. It does not claim that a build or render was run.

Fail if the composition length is given as the narration length (14.3 s), as last word end plus 2 s (about 16.29 s), or as any other value than 16.4 s, or gives the hold as anything other than 2.0 s (rule) or about 2.013 s (on screen).
