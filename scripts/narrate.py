#!/usr/bin/env python3
"""narrate.py - spend-guarded ElevenLabs narration: text -> WAV + alignment + core words.json.

Library (used by the CLI below and meant for server/tts_mcp.py):
  estimate(client, text, model_id, user_ceiling) -> dict    two free GETs, never a POST
  synthesize(client, *, text, name, confirm_spend, max_chars, user_ceiling, out_dir, voice_id,
             model_id, output_format) -> dict                  at most one paid POST

Spend guard (synthesize), in order:
  1. No network: confirm_spend is exactly True; text is not blank; max_chars and user_ceiling are
     whole numbers with 1 <= user_ceiling <= 10000 (plugin.json max_chars_per_call bounds) and
     len(text) <= max_chars <= user_ceiling; name matches [A-Za-z0-9._-]+; voice_id is set;
     output_format is wav_24000 or wav_44100; out_dir is absolute and outside both this add-on's
     checkout and CLAUDE_PLUGIN_ROOT; none of the three output files exists; ffprobe is on PATH.
     Any failure raises Refused and no HTTP request is made.
  2. Two free GETs: /v1/models (model listed with can_do_text_to_speech) and /v1/user/subscription
     (len(text) <= character_limit - character_count). Any failure raises Refused; no POST is made.
  3. Exactly one POST /v1/text-to-speech/{voice_id}/with-timestamps?output_format=<wav_*>, no
     retries, no conversion. Any failure from here on raises PossiblyBilled.
  4. Files, each opened with mode "x" (never overwritten), in this order:
     <name>.alignment.json (the response minus audio), <name>.wav (bytes must start RIFF....WAVE;
     then checked with ffprobe, which also gives the duration), <name>.words.json (built from
     `alignment`, never `normalized_alignment`, and only when the alignment spells the sent text).

Field names: character_limit, character_count, can_do_text_to_speech and
model_rates.character_cost_multiplier follow kellerai-concept-shorts/tools/tts_generate.py:13,468,886
(which cites https://elevenlabs.io/docs/api-reference/models/list). UNVERIFIED:
maximum_text_length_per_request (reported as max_text_length and enforced only when present).

CLI (key from ELEVENLABS_API_KEY only; it is never printed):
  narrate.py estimate   (--text T | --text-file F) [--model-id M] [--max-chars-per-call N]
  narrate.py synthesize (--text T | --text-file F) --name N --voice-id V --max-chars N
                        [--confirm-spend] [--out-dir ABS] [--model-id M] [--output-format F]
                        [--max-chars-per-call N]
  Both take [--api-base URL] [--allow-loopback]. --out-dir defaults to $CLAUDE_PLUGIN_DATA/narration.
Exit: 0 done; 1 the POST was sent and something failed (may be billed); 2 usage error;
3 nothing was sent to the paid endpoint (refused, unconfigured, or a free GET failed).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import words  # noqa: E402
from el_client import REDACTED, ElevenLabsClient, ElevenLabsError  # noqa: E402

ADDON_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_API_BASE = "https://api.elevenlabs.io"
DEFAULT_MODEL_ID = "eleven_multilingual_v2"
DEFAULT_OUTPUT_FORMAT = "wav_24000"
OUTPUT_FORMATS = ("wav_24000", "wav_44100")
DEFAULT_MAX_CHARS_PER_CALL = 1500
CEILING_MIN, CEILING_MAX = 1, 10000
NAME_RE = re.compile(r"[A-Za-z0-9._-]+")
FFPROBE_TIMEOUT_S = 30
TTS_SETUP_HINT = "ElevenLabs API key is not set: run the tts-setup skill"
CHECK_HISTORY = "check your ElevenLabs history before trying again"

EXIT_OK = 0
EXIT_POSSIBLY_BILLED = 1
EXIT_USAGE = 2
EXIT_NOT_SENT = 3


class NarrateError(Exception):
    pass


class Refused(NarrateError):
    """Nothing was sent to the paid endpoint."""


class PossiblyBilled(NarrateError):
    """The paid POST was sent; the request may have been billed."""


def is_placeholder(value: str | None) -> bool:
    """True for an unset or empty value, or one still holding an unsubstituted ${...} placeholder."""
    return not value or not str(value).strip() or str(value).strip().startswith("${")


def key_configured(key: str | None) -> bool:
    """False for an unset key, an empty one, or an unsubstituted ${...} placeholder."""
    return not is_placeholder(key)


def _whole_number(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value)
    return None


def _ceiling(user_ceiling: object) -> int:
    ceiling = _whole_number(user_ceiling)
    if ceiling is None or not CEILING_MIN <= ceiling <= CEILING_MAX:
        raise Refused(f"user ceiling must be a whole number from {CEILING_MIN} to {CEILING_MAX} (got {user_ceiling!r})")
    return ceiling


def _is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _find_model(models: object, model_id: str) -> dict[str, Any]:
    if not isinstance(models, list):
        raise Refused("GET /v1/models did not return a list")
    for model in models:
        if isinstance(model, dict) and model.get("model_id") == model_id:
            if model.get("can_do_text_to_speech") is not True:
                raise Refused(f"model {model_id!r} is listed but cannot do text to speech")
            return model
    raise Refused(f"model {model_id!r} is not in GET /v1/models")


def _remaining(subscription: object) -> int | None:
    if not isinstance(subscription, dict):
        return None
    limit = _whole_number(subscription.get("character_limit"))
    count = _whole_number(subscription.get("character_count"))
    if limit is None or count is None:
        return None
    return limit - count


def estimate(client: ElevenLabsClient, text: str, model_id: str, user_ceiling: object) -> dict[str, Any]:
    """Free: GET /v1/models then GET /v1/user/subscription. Raises Refused on any failure."""
    ceiling = _ceiling(user_ceiling)
    try:
        model = _find_model(client.list_models(), model_id)
        subscription = client.subscription()
    except ElevenLabsError as exc:
        raise Refused(f"free check failed, nothing was billed: {exc}") from None
    rates = model.get("model_rates")
    multiplier = rates.get("character_cost_multiplier") if isinstance(rates, dict) else None
    max_len = _whole_number(model.get("maximum_text_length_per_request"))
    chars = len(text)
    return {
        "chars": chars,
        "model_id": model_id,
        "max_text_length": max_len,
        "character_cost_multiplier": multiplier,
        "remaining_chars": _remaining(subscription),
        "user_ceiling": ceiling,
        "within_ceiling": chars <= ceiling and (max_len is None or chars <= max_len),
        "configured": True,
    }


def _output_paths(out_dir: str, name: str, plugin_root: str | None) -> tuple[Path, Path, Path]:
    if not isinstance(name, str) or NAME_RE.fullmatch(name) is None:
        raise Refused(f"name must match [A-Za-z0-9._-]+ (got {name!r})")
    directory = Path(out_dir)
    if not directory.is_absolute():
        raise Refused(f"out_dir must be an absolute path (got {out_dir!r})")
    resolved = directory.resolve()
    roots = [ADDON_ROOT] + ([Path(plugin_root).resolve()] if plugin_root else [])
    for root in roots:
        if _is_within(resolved, root):
            raise Refused(f"out_dir {out_dir} is inside the plugin directory {root}; choose a data directory")
    paths = (
        directory / f"{name}.alignment.json",
        directory / f"{name}.wav",
        directory / f"{name}.words.json",
    )
    for path in paths:
        if os.path.lexists(path):
            raise Refused(f"{path} already exists; refusing to overwrite")
    return paths


def _probe_wav(ffprobe: str, path: Path) -> float:
    """ffprobe the written WAV; return its duration in seconds. Raises ValueError."""
    cmd = [ffprobe, "-v", "error", "-show_entries", "format=format_name,duration", "-of", "json", str(path)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=FFPROBE_TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError(f"ffprobe could not run: {exc}") from None
    if proc.returncode != 0:
        raise ValueError(f"ffprobe rejected the file: {proc.stderr.strip()[:300]}")
    try:
        fmt = json.loads(proc.stdout)["format"]
        duration = float(fmt["duration"])
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError(f"ffprobe output has no duration: {exc}") from None
    if "wav" not in str(fmt.get("format_name", "")).split(","):
        raise ValueError(f"ffprobe format is {fmt.get('format_name')!r}, not wav")
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError(f"ffprobe duration is {duration!r}")
    return duration


def synthesize(
    client: ElevenLabsClient,
    *,
    text: str,
    name: str,
    confirm_spend: object,
    max_chars: object,
    user_ceiling: object,
    out_dir: str,
    voice_id: str,
    model_id: str = DEFAULT_MODEL_ID,
    output_format: str = DEFAULT_OUTPUT_FORMAT,
    plugin_root: str | None = None,
) -> dict[str, Any]:
    """One paid ElevenLabs call behind the spend guard (module docstring). plugin_root defaults
    to $CLAUDE_PLUGIN_ROOT. Raises Refused (nothing sent) or PossiblyBilled (POST sent)."""
    # 1. Local checks: no network.
    if confirm_spend is not True:
        raise Refused("confirm_spend must be true; nothing was sent")
    if not isinstance(text, str) or not text.strip():
        raise Refused("text is empty")
    ceiling = _ceiling(user_ceiling)
    limit = _whole_number(max_chars)
    if limit is None or limit < 1:
        raise Refused(f"max_chars must be a whole number >= 1 (got {max_chars!r})")
    chars = len(text)
    if not chars <= limit <= ceiling:
        raise Refused(f"need len(text) <= max_chars <= ceiling, got {chars} <= {limit} <= {ceiling}")
    if not isinstance(voice_id, str) or not voice_id:
        raise Refused("voice_id is not set; run the tts-setup skill")
    if output_format not in OUTPUT_FORMATS:
        raise Refused(f"output_format must be one of {', '.join(OUTPUT_FORMATS)} (got {output_format!r})")
    if plugin_root is None:
        env_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
        plugin_root = None if is_placeholder(env_root) else env_root  # ADDON_ROOT is always checked
    alignment_path, wav_path, words_path = _output_paths(out_dir, name, plugin_root)
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        raise Refused("ffprobe is not on PATH; install ffmpeg first")
    try:
        Path(out_dir).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise Refused(f"cannot create out_dir: {exc}") from None

    # 2. Two free GETs.
    est = estimate(client, text, model_id, ceiling)
    if not est["within_ceiling"]:
        raise Refused(f"{chars} characters exceed the model's max_text_length {est['max_text_length']}")
    remaining = est["remaining_chars"]
    if remaining is None:
        raise Refused("could not read the remaining character quota from GET /v1/user/subscription")
    if chars > remaining:
        raise Refused(f"{chars} characters exceed the remaining quota of {remaining}")

    # 3. Exactly one POST.
    try:
        speech = client.tts_with_timestamps(voice_id, text, model_id, output_format)
    except (ElevenLabsError, ValueError) as exc:
        raise PossiblyBilled(f"the paid request failed and may have been billed; {CHECK_HISTORY}: {exc}") from None

    # 4. alignment.json, then wav, then words.json.
    written: list[str] = []

    def billed(problem: str) -> PossiblyBilled:
        kept = ", ".join(written) if written else "nothing"
        return PossiblyBilled(f"the request was billed but {problem}; kept: {kept}")

    record = {
        "alignment": speech.alignment,
        "normalized_alignment": speech.normalized_alignment,
        "request_id": speech.request_id,
        "voice_id": voice_id,
        "model_id": model_id,
        "output_format": output_format,
    }
    try:
        with open(alignment_path, "x", encoding="utf-8") as f:
            json.dump(record, f, indent=1, ensure_ascii=False)
            f.write("\n")
        written.append(str(alignment_path))
        audio = speech.audio_bytes
        if audio[:4] != b"RIFF" or audio[8:12] != b"WAVE":
            raise billed(f"the audio is not a RIFF/WAVE file (starts {audio[:12]!r})")
        with open(wav_path, "xb") as fb:
            fb.write(audio)
        written.append(str(wav_path))
    except OSError as exc:
        raise billed(f"writing the output failed: {exc}") from None
    try:
        duration = _probe_wav(ffprobe, wav_path)
    except ValueError as exc:
        raise billed(f"the WAV failed verification: {exc}") from None
    rebuild = f"rebuild with: words.py --from-alignment {alignment_path} --wav-duration {duration} --out {words_path}"
    try:
        if speech.alignment is None:
            raise billed("the response has no alignment, so no words.json was written")
        if not words.covers_text(speech.alignment, text):
            raise billed(f"the alignment does not spell the sent text, so no words.json was written; {rebuild}")
        result_words = words.alignment_to_words(speech.alignment, duration)
        with open(words_path, "x", encoding="utf-8") as f:
            json.dump(result_words, f, indent=1, ensure_ascii=False)
            f.write("\n")
    except (OSError, ValueError) as exc:
        raise billed(f"words.json could not be written ({exc}); {rebuild}") from None
    return {
        "status": "OK",
        "wav": str(wav_path),
        "words": str(words_path),
        "alignment": str(alignment_path),
        "durationSec": round(duration, 6),
        "charsSent": chars,
        "modelId": model_id,
        "voiceId": voice_id,
        "outputFormat": output_format,
        "requestId": speech.request_id,
    }


# ----------------------------------------------------------------------------- CLI


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Spend-guarded ElevenLabs narration (key from ELEVENLABS_API_KEY).")
    sub = ap.add_subparsers(dest="command", required=True)
    common = argparse.ArgumentParser(add_help=False)
    src = common.add_mutually_exclusive_group(required=True)
    src.add_argument("--text", help="exact narration text")
    src.add_argument("--text-file", help="UTF-8 file holding the exact narration text (sent unchanged)")
    common.add_argument("--model-id", default=DEFAULT_MODEL_ID)
    common.add_argument("--max-chars-per-call", type=int, default=DEFAULT_MAX_CHARS_PER_CALL,
                        help=f"your ceiling, {CEILING_MIN}-{CEILING_MAX} (default {DEFAULT_MAX_CHARS_PER_CALL})")
    common.add_argument("--api-base", default=DEFAULT_API_BASE, help="ElevenLabs API host")
    common.add_argument("--allow-loopback", action="store_true",
                        help="also accept http://127.0.0.1:PORT as --api-base (local fake server, tests only)")
    sub.add_parser("estimate", parents=[common], help="free: character count, quota and ceiling check")
    syn = sub.add_parser("synthesize", parents=[common], help="paid: exactly one ElevenLabs request")
    syn.add_argument("--name", required=True, help="output base name, [A-Za-z0-9._-]+")
    syn.add_argument("--voice-id", required=True)
    syn.add_argument("--max-chars", type=int, required=True, help="confirmed character count for this call")
    syn.add_argument("--confirm-spend", action="store_true", help="required: confirms this call may be billed")
    syn.add_argument("--out-dir", help="absolute output directory (default $CLAUDE_PLUGIN_DATA/narration)")
    syn.add_argument("--output-format", choices=OUTPUT_FORMATS, default=DEFAULT_OUTPUT_FORMAT)
    return ap


def _run(a: argparse.Namespace, key: str | None, ap: argparse.ArgumentParser) -> tuple[int, dict[str, Any]]:
    if a.text_file is not None:
        with open(a.text_file, encoding="utf-8") as f:
            text = f.read()
    else:
        text = a.text
    if a.command == "estimate":
        if key is None or not key_configured(key):
            return EXIT_NOT_SENT, {"configured": False, "chars": len(text), "message": TTS_SETUP_HINT}
        client = ElevenLabsClient(key, a.api_base, allow_loopback=a.allow_loopback)
        return EXIT_OK, estimate(client, text, a.model_id, a.max_chars_per_call)
    out_dir: str | None = a.out_dir
    if out_dir is None:
        data = os.environ.get("CLAUDE_PLUGIN_DATA")
        if data is None or is_placeholder(data):
            ap.error("--out-dir is required when CLAUDE_PLUGIN_DATA is not set")
        out_dir = os.path.join(data, "narration")
    if key is None or not key_configured(key):
        raise Refused(TTS_SETUP_HINT)
    client = ElevenLabsClient(key, a.api_base, allow_loopback=a.allow_loopback)
    result = synthesize(
        client, text=text, name=a.name, confirm_spend=a.confirm_spend, max_chars=a.max_chars,
        user_ceiling=a.max_chars_per_call, out_dir=out_dir, voice_id=a.voice_id,
        model_id=a.model_id, output_format=a.output_format,
    )
    return EXIT_OK, result


def main(argv: list[str] | None = None) -> int:
    ap = _parser()
    a = ap.parse_args(argv)
    key = os.environ.get("ELEVENLABS_API_KEY")

    def redact(s: str) -> str:
        return s.replace(key, REDACTED) if key is not None and key_configured(key) else s

    try:
        code, payload = _run(a, key, ap)
    except Refused as exc:
        sys.stderr.write(redact(f"narrate.py: not sent: {exc}\n"))
        return EXIT_NOT_SENT
    except PossiblyBilled as exc:
        sys.stderr.write(redact(f"narrate.py: {exc}\n"))
        return EXIT_POSSIBLY_BILLED
    except (OSError, ValueError) as exc:  # unreadable --text-file, rejected --api-base: before any request
        sys.stderr.write(redact(f"narrate.py: not sent: {exc}\n"))
        return EXIT_NOT_SENT
    except Exception as exc:  # last resort so no traceback can carry the key; outcome unknown
        sys.stderr.write(redact(f"narrate.py: unexpected {type(exc).__name__}: {exc}; may have been billed; {CHECK_HISTORY}\n"))
        return EXIT_POSSIBLY_BILLED
    sys.stdout.write(redact(json.dumps(payload, ensure_ascii=False)) + "\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
