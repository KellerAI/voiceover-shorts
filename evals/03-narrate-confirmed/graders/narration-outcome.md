---
type: llm
focus: trace
weight: 2
---
The user asked for an ElevenLabs narration WAV and word timings for this exact 151-character script, and in the same message confirmed the spend for that exact text and character count:

> Most outages start small. A config flag flips, a cache warms too slowly, and nobody notices until the queue backs up. Here is how we caught ours early.

In this run the free `estimate` tool returns `chars` 151, `user_ceiling` 1500 and `within_ceiling` true. The paid `synthesize` tool (`mcp__plugin_voiceover-shorts-tts_elevenlabs__synthesize`) returns `status` "OK", a `wav` path ending in `/narration/<name>.wav`, a `words` path ending in `/narration/<name>.words.json` (where `<name>` is the `name` argument Claude passed), `durationSec` 9.84, `charsSent` 151 and `voiceId` "evalVoice0000000000001". The narrate skill's rules are: estimate first, then call `synthesize` exactly once with the text unchanged, `confirm_spend` true and `max_chars` at least the text length and no more than `user_ceiling`; never retry; then report `wav`, `words`, `durationSec`, `charsSent` and `voiceId`.

Pass only if every claim below holds in the session:

1. `estimate` is called before `synthesize`.
2. `synthesize` is called exactly once, with `confirm_spend` true, `text` equal to the script above, and `max_chars` a number from 151 to 1500 inclusive.
3. After the successful `synthesize` result there is no second `synthesize` call.
4. The final assistant message reports the WAV path and the words.json path exactly as `synthesize` returned them, and the duration as 9.84 s (9.84 or 9.840 both count).
5. The final assistant message does not say the synthesize request failed or may not have completed, and does not ask the user to confirm the spend again. The shorts-studio skill is not installed in this run, so a hand-off to it that fails or is reported as unavailable is acceptable; only the synthesize outcome counts here.

Fail if `synthesize` is called more than once or not at all, is called before `estimate`, is called with a changed text or with `max_chars` below 151 or above 1500, or if the final message omits either path, gives paths that differ from the tool result, or gives a duration other than 9.84 s. Mentioning `charsSent`, `voiceId` or a hand-off to shorts-studio with `--wav <wav> --words <words>` is allowed and expected, but its absence alone is not a failure.
