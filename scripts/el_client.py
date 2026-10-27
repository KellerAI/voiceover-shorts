"""Minimal ElevenLabs HTTP client (standard library only) for voiceover-shorts-tts.

API facts used here, checked against the docs on 2026-10-26:

- https://elevenlabs.io/docs/api-reference/text-to-speech/convert-with-timestamps
  POST /v1/text-to-speech/{voice_id}/with-timestamps. `output_format` is a query
  parameter (its enum includes wav_24000 and wav_44100). The JSON body carries `text`
  and `model_id`. A 200 body has `audio_base64` (required), plus `alignment` and
  `normalized_alignment` (nullable; each holds characters, character_start_times_seconds
  and character_end_times_seconds). The page's "Servers" list is ALLOWED_API_BASES.
- https://elevenlabs.io/docs/api-reference/authentication
  every request sends the key in an `xi-api-key` header.
- GET /v1/models (https://elevenlabs.io/docs/api-reference/models/list),
  GET /v1/user/subscription (https://elevenlabs.io/docs/api-reference/user/subscription/get),
  GET /v2/voices (https://elevenlabs.io/docs/api-reference/voices/search).

UNVERIFIED: the convert-with-timestamps page does not mention a `request-id` response
header. It is captured when present, as the add-on spec asks, and is None otherwise.

Each method makes exactly one request, with no retries. Every ElevenLabsError message
has the API key replaced by "***".
"""

from __future__ import annotations

import base64
import binascii
import http.client
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

ALLOWED_API_BASES = frozenset(
    {
        "https://api.elevenlabs.io",
        "https://api.us.elevenlabs.io",
        "https://api.eu.residency.elevenlabs.io",
        "https://api.in.residency.elevenlabs.io",
        "https://api.sg.residency.elevenlabs.io",
    }
)
LOOPBACK_BASE_RE = re.compile(r"^http://127\.0\.0\.1:\d{1,5}$")
DEFAULT_TIMEOUT_S = 120.0
MAX_ERROR_BODY_CHARS = 500
REDACTED = "***"

# transport(method, url, headers, body) -> (status, response headers, response body)
Transport = Callable[[str, str, Mapping[str, str], bytes | None], tuple[int, Mapping[str, str], bytes]]


class ElevenLabsError(Exception):
    """A failed ElevenLabs call. `status` is None when no HTTP status was received
    (network failure or timeout); for a POST that means the outcome is unknown."""

    def __init__(self, status: int | None, message: str) -> None:
        super().__init__(status, message)
        self.status = status
        self.message = message

    def __str__(self) -> str:
        return f"ElevenLabs error (status {self.status}): {self.message}"


@dataclass(frozen=True)
class TimestampedSpeech:
    audio_bytes: bytes
    alignment: dict[str, Any] | None
    normalized_alignment: dict[str, Any] | None
    request_id: str | None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # urllib forwards custom headers (the API key) on redirects; refuse to follow them.
    def redirect_request(self, req: Any, fp: Any, code: Any, msg: Any, headers: Any, newurl: Any) -> None:
        return None


def urllib_transport(
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: bytes | None,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> tuple[int, dict[str, str], bytes]:
    """Default transport. HTTP error statuses are returned, not raised."""
    request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except urllib.error.HTTPError as exc:
        try:
            data = exc.read()
        finally:
            exc.close()
        items = exc.headers.items() if exc.headers is not None else []
        return exc.code, {k.lower(): v for k, v in items}, data


class ElevenLabsClient:
    def __init__(
        self,
        api_key: str,
        api_base: str,
        transport: Transport | None = None,
        allow_loopback: bool = False,
    ) -> None:
        if not isinstance(api_key, str) or not api_key:
            raise ValueError("api_key must be a non-empty string")
        base = api_base.rstrip("/") if isinstance(api_base, str) else ""
        allowed = base in ALLOWED_API_BASES or (allow_loopback and LOOPBACK_BASE_RE.match(base) is not None)
        if not allowed:
            shown = str(api_base).replace(api_key, REDACTED)
            raise ValueError(f"api_base {shown!r} is not an allowed ElevenLabs API host")
        self._key = api_key
        self._base = base
        self._transport: Transport = transport if transport is not None else urllib_transport

    def __repr__(self) -> str:
        return f"ElevenLabsClient(api_base={self._base!r}, api_key={REDACTED!r})"

    def list_models(self) -> Any:
        return self._get_json("/v1/models")

    def subscription(self) -> Any:
        return self._get_json("/v1/user/subscription")

    def list_voices(self) -> Any:
        return self._get_json("/v2/voices")

    def tts_with_timestamps(self, voice_id: str, text: str, model_id: str, output_format: str) -> TimestampedSpeech:
        if not voice_id:
            raise ValueError("voice_id must be non-empty")
        path = f"/v1/text-to-speech/{urllib.parse.quote(voice_id, safe='')}/with-timestamps"
        status, headers, data = self._request(
            "POST", path, query={"output_format": output_format}, payload={"text": text, "model_id": model_id}
        )
        doc = self._decode_json("POST", path, status, data)
        if not isinstance(doc, dict) or not isinstance(doc.get("audio_base64"), str):
            raise ElevenLabsError(status, f"POST {path}: response has no audio_base64 string")
        try:
            audio = base64.b64decode(doc["audio_base64"], validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ElevenLabsError(status, self._redact(f"POST {path}: audio_base64 is not valid base64: {exc}")) from None
        return TimestampedSpeech(
            audio_bytes=audio,
            alignment=doc.get("alignment"),
            normalized_alignment=doc.get("normalized_alignment"),
            request_id=headers.get("request-id"),
        )

    # ------------------------------------------------------------------ internals

    def _redact(self, text: str) -> str:
        return text.replace(self._key, REDACTED)

    def _get_json(self, path: str) -> Any:
        status, _, data = self._request("GET", path)
        return self._decode_json("GET", path, status, data)

    def _decode_json(self, method: str, path: str, status: int, data: bytes) -> Any:
        try:
            return json.loads(data)
        except ValueError as exc:  # JSONDecodeError and UnicodeDecodeError
            raise ElevenLabsError(status, self._redact(f"{method} {path}: response is not JSON: {exc}")) from None

    def _request(
        self,
        method: str,
        path: str,
        query: Mapping[str, str] | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        url = self._base + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        headers = {"xi-api-key": self._key, "Accept": "application/json"}
        body = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        try:
            status, resp_headers, data = self._transport(method, url, headers, body)
        except (OSError, http.client.HTTPException, ValueError) as exc:
            message = f"{method} {path}: request failed, outcome unknown: {type(exc).__name__}: {exc}"
            raise ElevenLabsError(None, self._redact(message)) from None
        if not 200 <= status < 300:
            # Redact before truncating so a key straddling the cut cannot leak in part.
            text = self._redact(data.decode("utf-8", "replace"))[:MAX_ERROR_BODY_CHARS]
            raise ElevenLabsError(status, f"{method} {path}: HTTP {status}: {text}")
        return status, {k.lower(): v for k, v in resp_headers.items()}, data
