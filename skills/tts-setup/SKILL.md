---
name: tts-setup
description: "Walk the user through ElevenLabs setup for voiceover-shorts-tts: getting an account, creating a restricted API key, storing it in the plugin configuration, choosing a voice_id, and running a free end-to-end check. Never spends credits."
when_to_use: "Use when the user wants to set up ElevenLabs, add or fix an ElevenLabs API key, pick a narration voice, or set up narration/text-to-speech, or when an add-on tool says to run the tts-setup skill."
model: opus
effort: low
user-invocable: true
allowed-tools:
  - Read
  - mcp__plugin_voiceover-shorts-tts_elevenlabs__list_voices
  - mcp__plugin_voiceover-shorts-tts_elevenlabs__estimate
---

# ElevenLabs setup

Follow these steps in order. Stop and wait for the user wherever a step says so.

## Hard rules

- Never fetch, open, navigate to, or pass the affiliate link to any tool: not WebFetch, not Bash, not Claude in Chrome.
  The ElevenLabs affiliate terms prohibit "bots, scripts, or other automated methods". The user clicks it themselves.
- Never ask for the API key in chat. It belongs in the plugin configuration only.
- Never call `synthesize` from this skill. Every tool used here is free.

## 1. Ask about an account

Ask: "Do you already have an ElevenLabs account?"

## 2. No account: give both sign-up options, then stop

Reply with both links as text, exactly like this:

```text
Sign up through KellerAI's link:
https://try.elevenlabs.io/vbrajywhn5js
Paid link: KellerAI is an independent affiliate of ElevenLabs and may earn a commission.

Or sign up directly:
https://elevenlabs.io/app/sign-up
```

Then stop and wait until the user says they have an account.

## 3. Has an account: create a restricted key

1. Offer optional Claude in Chrome help. Use it only after the user has signed in to ElevenLabs themselves, and only
   to navigate to the API keys page. The user creates the key; never fill in or read the key yourself.
2. Recommend a restricted key: text-to-speech access, plus read access to voices, models, and user. Set a credit quota.

## 4. Store the key in the plugin configuration

Tell the user to paste the key into the `elevenlabs_api_key` option of the voiceover-shorts-tts plugin configuration
(a sensitive option, kept in secure storage). Not into chat.

If the user pastes a key into chat, tell them to revoke that key in ElevenLabs now and create a new one. Do not use
or repeat the pasted key.

## 5. Choose a voice

Call `list_voices`. Show the names, categories, and `voice_id` values. Have the user set the `voice_id` option in the
plugin configuration. Remind them they are responsible for having the rights to the voice they choose.

If a tool reports `configured:false` or tells you to run tts-setup, the key is not set yet: return to step 4.

## 6. Check end to end

Call `estimate` with a sample sentence, such as "This is a test of my narration voice." It is free. Report `chars`,
`model_id`, `remaining_chars`, and `within_ceiling`. A result with `configured:true` means setup is complete.

## Mac CLI fallback

Outside the MCP server, `scripts/narrate.py` reads the key from the `ELEVENLABS_API_KEY` environment variable.
