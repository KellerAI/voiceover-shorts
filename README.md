# voiceover-shorts-tts

- **Version:** 0.1.0
- **License:** see `LICENSE`; trademarks and service terms are in `NOTICE`

---

voiceover-shorts-tts turns text into a narration WAV and a matching `<name>.words.json` for the voiceover-shorts
plugin. It calls the ElevenLabs text-to-speech API with your own ElevenLabs API key. Each paid request uses
credits from your ElevenLabs account; KellerAI does not provide, resell or proxy the service.

## Requirements

You need `python3` and `ffprobe` on your `PATH`. The MCP server uses only the Python standard library, and
`ffprobe` checks each WAV after it is written.

## Install

Install the add-on from the repository marketplace.

```bash
/plugin install voiceover-shorts-tts --marketplace KellerAI/voiceover-shorts
```

From a shell: `claude plugin install voiceover-shorts-tts --marketplace KellerAI/voiceover-shorts`.

## Setup

The `tts-setup` skill walks you through creating an API key and entering it as the plugin's sensitive
`elevenlabs_api_key` option. It also covers `voice_id`, `model_id` (default `eleven_multilingual_v2`),
`output_format` (`wav_24000`, or `wav_44100`, which may need a Pro account) and `api_base`.

### ElevenLabs account

You need an ElevenLabs account. Either link below works.

- Affiliate link: <https://try.elevenlabs.io/vbrajywhn5js>\
  Paid link: KellerAI is an independent affiliate of ElevenLabs and may earn a commission.
- Direct sign-up: https://elevenlabs.io/app/sign-up

## Usage

The `narrate` skill runs one narration in four steps.

1. It calls `estimate` on your text and shows the character count, your remaining quota and your ceiling.
2. It waits for your explicit confirmation of the spend.
3. It makes one `synthesize` call, which writes the alignment, the WAV and the `words.json`.
4. It hands the WAV and `words.json` to the `shorts-studio` skill in voiceover-shorts.

## Spend controls

The `synthesize` tool refuses to spend unless every check passes, and then makes a single request.

- `confirm_spend` must be `true`.
- `max_chars` must be at least the text length and at most your ceiling, `max_chars_per_call` (default 1500).
- If a check fails or an output file already exists, it returns an error and makes zero HTTP requests.
- Two free requests come first: the model list and your subscription quota.
- The paid step is exactly one POST, with no retries. An unknown outcome is reported as possibly billed.

## Data and network

The add-on talks only to the ElevenLabs host you pick in `api_base`:

- `https://api.elevenlabs.io` (default)
- `https://api.us.elevenlabs.io`
- `https://api.eu.residency.elevenlabs.io`
- `https://api.in.residency.elevenlabs.io`
- `https://api.sg.residency.elevenlabs.io`

| Method | Path | Paid |
| --- | --- | --- |
| GET | `/v1/models` | No |
| GET | `/v1/user/subscription` | No |
| GET | `/v2/voices` | No |
| POST | `/v1/text-to-speech/{voice_id}/with-timestamps` | Yes |

- **Credential:** one ElevenLabs API key, stored as a sensitive plugin option and passed only to the MCP server.
- **Sent to ElevenLabs:** the narration text, `voice_id` and `model_id`.
- **Written locally:** `${CLAUDE_PLUGIN_DATA}/narration/<name>.alignment.json`, `<name>.wav` and
  `<name>.words.json`, in that order. Existing files are never overwritten.
- **Telemetry:** none.

You can connect ElevenLabs' own hosted MCP yourself; this add-on doesn't use it because it returns no word timings.

## Plugin components

The add-on ships two skills and one MCP server.

| Component | Included | Detail |
| --- | --- | --- |
| Skills | Yes | `tts-setup` (account and key setup) and `narrate` (estimate, confirm, synthesize) |
| MCP servers | Yes | One stdio server, `elevenlabs`, in Python with only the standard library |
| Hooks | No | None |
| `bin/` | No | None |

## License

MIT with a no-training rider: the code and docs may not be used as AI or ML training data. See `LICENSE` for the
full text. "ElevenLabs" is a trademark of ElevenLabs, Inc.; this add-on is not affiliated with or endorsed by
ElevenLabs, Inc. (see `NOTICE`). You are responsible for having the rights to the voice you choose.
