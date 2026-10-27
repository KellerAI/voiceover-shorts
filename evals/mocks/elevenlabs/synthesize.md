---
type: fixed
expect:
  text: string
  name: "/^[A-Za-z0-9._-]+$/"
  confirm_spend: true
  max_chars: number
---

{"status": "OK", "wav": "/Users/eval-user/.claude/plugins/data/voiceover-shorts-tts-voiceover-shorts/narration/{{input.name}}.wav", "words": "/Users/eval-user/.claude/plugins/data/voiceover-shorts-tts-voiceover-shorts/narration/{{input.name}}.words.json", "alignment": "/Users/eval-user/.claude/plugins/data/voiceover-shorts-tts-voiceover-shorts/narration/{{input.name}}.alignment.json", "durationSec": 2.871, "charsSent": {{input.max_chars}}, "modelId": "eleven_multilingual_v2", "voiceId": "evalVoice0000000000001", "outputFormat": "wav_24000", "requestId": "eval-request-0001"}
