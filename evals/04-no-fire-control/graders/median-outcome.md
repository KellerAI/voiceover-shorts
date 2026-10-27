---
type: llm
focus: last_message
weight: 2
---
The user asked for a Python function `median(values: list[float]) -> float` that returns the median of a non-empty list without changing the caller's list, raises `ValueError` for an empty list, plus two example calls with their results (one odd-length list, one even-length list).

Pass only if every claim below holds in the final answer:

1. It gives a Python function named `median` that returns the middle value of the sorted values for an odd-length list and the mean of the two middle values for an even-length list.
2. It does not sort or otherwise change the caller's list in place (using `sorted(values)` or sorting a copy is correct; calling `values.sort()` on the argument is not).
3. It raises `ValueError` when the list is empty.
4. It shows one odd-length and one even-length example call, and each stated result is the correct median for that list.
5. It contains no narration or text-to-speech content: it does not mention ElevenLabs, voices, narration, WAV files, word timings, words.json, character counts or spend, and does not offer to narrate anything.

Fail if the function returns a wrong median for either list length, mutates the input list, does not raise `ValueError` on an empty list, an example result is wrong, or the answer brings up narration, text-to-speech or ElevenLabs.
