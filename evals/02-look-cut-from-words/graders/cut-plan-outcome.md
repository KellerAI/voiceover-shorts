---
type: llm
focus: last_message
weight: 2
---
The user gave the real JSON output of the plugin's cut picker (`pick_cut.py --target 15 --lo 12.5 --hi 17.5`) for a 14.3 s narration and asked for an explanation of the cut, the look window, and the look-dev workflow arguments. In that JSON the picker chose "terms." (word w45, wordEnd 14.28669) with `cut` 14.233333, inside the silence gap 13.843917-14.3 s (the gap runs to the end of the WAV). It rejected "off." (cut 12.2, status "cut 12.200 outside 12.5-17.5 s") and "agents." (cut 5.133333, status "cut 5.133 outside 12.5-17.5 s"). The plugin rule: the cut is the last 30 fps frame boundary at or before the gap end minus 0.05 s (14.3 - 0.05 = 14.25, floored to the frame grid = 14.2333), never earlier than the gap start plus 0.1 s, and it must fall in the window. Pass only if every claim below holds in the final answer:

1. It says the cut is at the "terms." sentence end at 14.233 s (14.233, 14.2333 or 14.233333 all count), taken from the picker output, so the clip keeps the whole narration through "terms.".
2. It explains how that number follows from the silence gap, not from the word timing: it names the gap end (14.3 s, or the end of the trailing silence), the 0.05 s end margin, and the snap down to a 30 fps frame boundary. Saying only "the picker chose it" or "it is in a silence gap" is not enough.
3. It rejects "off." because its cut (12.2 s) is below the 12.5 s lower bound, and rejects "agents." because its cut (about 5.13 s) is far below the window.
4. It gives the look window as 12.5 to 17.5 s.
5. Its workflow arguments include mode "look"; styles "D" (key `styles`, as a string or a one-element list); a run tag (`runTag`); the given picker JSON object as the `cut` argument (the whole object, or an unmistakable reference to that exact JSON line, not a bare number); and the required path arguments `pluginRoot`, `studioRoot` and `outRoot` (placeholders such as `${CLAUDE_PLUGIN_ROOT}`, `$SHORTS_STUDIO_DIR` and `${CLAUDE_PLUGIN_DATA}/lookdev-v2` are fine). If it also passes `wav` or `words`, they must equal `cut.wav` and `cut.words`.
6. It does not claim that audio was cut, a workflow was run, or a video was rendered by the assistant.

Fail if it states that the cut is at any time other than 14.233 s (for example, claims the cut is at the word end 14.287 s, or recomputes a different value); mentioning 14.287 s as the "terms." word end while giving 14.233 s as the cut is fine. Also fail if it picks "off." or any mid-sentence word, gives a window other than 12.5 to 17.5 s, passes `cut` as a number or omits it, or offers full-mode arguments (wav/words paths without a cut) as the look-mode plan.
