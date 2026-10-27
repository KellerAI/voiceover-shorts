"""Loopback fake of the ElevenLabs endpoints used by scripts/el_client.py. Tests only.

Binds 127.0.0.1 on an ephemeral port and serves in a daemon thread for the life of the
context manager. Every request is counted per (method, path) and recorded with its
lowercased headers, parsed query string and raw body. Setting `scripted_error` to
(status, body_bytes) makes every later request get that response until it is reset to None.
Setting `voice_pages` to {next_page_token or None: page} makes GET /v2/voices serve the page
keyed by the request's `next_page_token` query value (None when absent); an unknown token is a 404.
"""

from __future__ import annotations

import base64
import io
import json
import re
import threading
import wave
from collections import Counter
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

TTS_PATH_RE = re.compile(r"^/v1/text-to-speech/[^/]+/with-timestamps$")
REQUEST_ID = "req_fake_0001"

MODELS: list[dict[str, Any]] = [
    {"model_id": "eleven_multilingual_v2", "name": "Fake Multilingual", "can_do_text_to_speech": True}
]
SUBSCRIPTION: dict[str, Any] = {"tier": "free", "character_count": 0, "character_limit": 10000}
VOICES: dict[str, Any] = {"voices": [{"voice_id": "voice_fake_1", "name": "Fake Voice"}], "has_more": False}
ALIGNMENT: dict[str, Any] = {
    "characters": ["H", "i", "."],
    "character_start_times_seconds": [0.0, 0.01, 0.02],
    "character_end_times_seconds": [0.01, 0.02, 0.03],
}


def make_wav(seconds: float = 0.05, rate: int = 24000) -> bytes:
    """Return a mono 16-bit RIFF WAV of silence."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(seconds * rate))
    return buf.getvalue()


@dataclass(frozen=True)
class RecordedRequest:
    method: str
    path: str
    query: dict[str, list[str]]
    headers: dict[str, str]
    body: bytes


class FakeElevenLabs:
    def __init__(self) -> None:
        self.counts: Counter[tuple[str, str]] = Counter()
        self.requests: list[RecordedRequest] = []
        self.scripted_error: tuple[int, bytes] | None = None
        self.voice_pages: dict[str | None, dict[str, Any]] | None = None
        self._lock = threading.Lock()
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        if self._server is None:
            raise RuntimeError("FakeElevenLabs is not running")
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    @property
    def total_requests(self) -> int:
        with self._lock:
            return sum(self.counts.values())

    def __enter__(self) -> FakeElevenLabs:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                fake._handle(self, "GET")

            def do_POST(self) -> None:
                fake._handle(self, "POST")

            def log_message(self, format: str, *args: Any) -> None:
                pass

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _handle(self, h: BaseHTTPRequestHandler, method: str) -> None:
        parts = urlsplit(h.path)
        length = int(h.headers.get("Content-Length") or 0)
        body = h.rfile.read(length) if length > 0 else b""
        record = RecordedRequest(
            method=method,
            path=parts.path,
            query=parse_qs(parts.query),
            headers={k.lower(): v for k, v in h.headers.items()},
            body=body,
        )
        with self._lock:
            self.counts[(method, parts.path)] += 1
            self.requests.append(record)
            error = self.scripted_error
            pages = self.voice_pages
        if error is not None:
            self._send(h, error[0], error[1], {})
            return
        if pages is not None and method == "GET" and parts.path == "/v2/voices":
            token = record.query.get("next_page_token", [None])[0]
            page = pages.get(token)
            status = 200 if page is not None else 404
            self._send(h, status, json.dumps(page if page is not None else {"detail": "bad token"}).encode("utf-8"), {})
            return
        status, payload, extra = self._route(method, parts.path)
        self._send(h, status, json.dumps(payload).encode("utf-8"), extra)

    @staticmethod
    def _route(method: str, path: str) -> tuple[int, Any, dict[str, str]]:
        if method == "GET" and path == "/v1/models":
            return 200, MODELS, {}
        if method == "GET" and path == "/v1/user/subscription":
            return 200, SUBSCRIPTION, {}
        if method == "GET" and path == "/v2/voices":
            return 200, VOICES, {}
        if method == "POST" and TTS_PATH_RE.match(path):
            payload = {
                "audio_base64": base64.b64encode(make_wav()).decode("ascii"),
                "alignment": ALIGNMENT,
                "normalized_alignment": ALIGNMENT,
            }
            return 200, payload, {"request-id": REQUEST_ID}
        return 404, {"detail": "not found"}, {}

    @staticmethod
    def _send(h: BaseHTTPRequestHandler, status: int, body: bytes, extra: dict[str, str]) -> None:
        h.send_response(status)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(body)))
        for name, value in extra.items():
            h.send_header(name, value)
        h.end_headers()
        h.wfile.write(body)
