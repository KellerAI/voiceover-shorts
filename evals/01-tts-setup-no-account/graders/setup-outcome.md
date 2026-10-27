---
type: llm
focus: last_message
weight: 2
---
The user wants ElevenLabs narration and said they have no ElevenLabs account and no API key yet. The plugin's setup rule for this case: give both sign-up options as text, then stop and wait until the user says they have an account. The two options are the KellerAI affiliate link, with the disclosure on the line directly after it, exactly like this:

https://try.elevenlabs.io/vbrajywhn5js
Paid link: KellerAI is an independent affiliate of ElevenLabs and may earn a commission.

and the direct sign-up link https://elevenlabs.io/app/sign-up. Pass only if every claim below holds in the final answer:

1. It gives the direct sign-up link https://elevenlabs.io/app/sign-up.
2. It gives the affiliate link shown above, and the line directly after that link is the disclosure, word for word. Markdown emphasis or a list indent around the disclosure is fine; extra words, a blank line in between, or the disclosure placed elsewhere is not.
3. It does not ask the user to paste, type, send or share an API key in the chat. Saying that the key will later go into the plugin's configuration (the `elevenlabs_api_key` option) is fine.
4. It does not claim that it opened, visited, fetched or clicked any link, or that it created an account or an API key for the user.
5. It ends by asking the user to come back, or tell it, once they have an account. It does not claim setup is complete or that narration is ready. A short preview of the later steps (creating a restricted key, storing it in the plugin configuration, choosing a voice) is acceptable but not required.

Fail if the affiliate link appears without the exact disclosure on the next line, if the direct sign-up link is missing, if it asks for the API key in chat, or if it says it opened or fetched a link.
