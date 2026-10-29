# Privacy

This policy covers the two plugins in this marketplace: the core `voiceover-shorts` plugin and the optional
`voiceover-shorts-tts` add-on.

KellerAI runs no servers for either plugin and receives no data from them. Neither plugin has telemetry or
analytics.

## voiceover-shorts (core)

The core plugin works only on your machine and makes no calls to the internet.

- The legibility preflight serves pages from `127.0.0.1` to a headless browser and aborts requests to any
  other host.
- Ratings and calibration are written under `${CLAUDE_PLUGIN_DATA}/feedback/` (`ratings.jsonl`,
  `calibration.json`). Cuts and renders go to output directories you choose, outside the plugin.
- None of this data leaves your machine unless you send it somewhere yourself.

Tools you install separately, such as HyperFrames and agent-browser, have their own terms.

## voiceover-shorts-tts (add-on)

The add-on sends your narration text to ElevenLabs, using your own ElevenLabs account, to make a WAV file
and word timings.

### What is sent to ElevenLabs

| Request | Data sent |
|---------|-----------|
| `POST /v1/text-to-speech/{voice_id}/with-timestamps` (paid) | Narration text, voice ID, model ID, output format |
| `GET /v1/models`, `GET /v1/user/subscription` (free) | None beyond the API key |
| `GET /v2/voices` (free) | Page size and page token |

Every request carries your API key in the `xi-api-key` header. Requests go only to the ElevenLabs API host
you pick (`api.elevenlabs.io` or one of its regional hosts). Redirects are not followed, so the key is never
forwarded to another host.

### Your API key

You enter the key in the plugin's settings, where Claude Code keeps it as a sensitive value. Claude Code
passes it to the add-on's local MCP server as an environment variable. The add-on does not write the key to
disk and replaces it with `***` in its output and error messages.

### What is written locally

Each narration writes three files, by default to `${CLAUDE_PLUGIN_DATA}/narration/`:

- `<name>.wav`: the audio returned by ElevenLabs.
- `<name>.alignment.json`: the character timings, plus the voice ID, model ID, output format and request
  ID.
- `<name>.words.json`: word timings built from the alignment.

Existing files are never overwritten, and the add-on refuses to write inside the plugin directory.

### ElevenLabs

ElevenLabs processes the text you send under its own terms. See the
[ElevenLabs privacy policy](https://elevenlabs.io/privacy-policy).

## Contact

Questions or concerns: open an issue at <https://github.com/KellerAI/voiceover-shorts/issues>.
