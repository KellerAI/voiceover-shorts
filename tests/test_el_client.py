"""Tests for scripts/el_client.py against the loopback fake (no real network I/O).

The live ElevenLabs path is NOT proven by these tests; only a real call is.
"""

from __future__ import annotations

import io
import json
import sys
import urllib.request
import wave
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
for _p in (_HERE, _HERE.parent / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import fake_elevenlabs  # noqa: E402
from el_client import (  # noqa: E402
    ALLOWED_API_BASES,
    MAX_ERROR_BODY_CHARS,
    ElevenLabsClient,
    ElevenLabsError,
)
from fake_elevenlabs import FakeElevenLabs  # noqa: E402

KEY = "sk_test_DUMMY"
VOICE = "voice_fake_1"
MODEL = "eleven_multilingual_v2"


@pytest.fixture(autouse=True)
def _no_proxies(monkeypatch: pytest.MonkeyPatch) -> None:
    # Keep loopback requests off any env or system proxy: no real network I/O.
    monkeypatch.setattr(urllib.request, "getproxies", dict)


@pytest.fixture
def fake() -> Iterator[FakeElevenLabs]:
    with FakeElevenLabs() as server:
        yield server


def _client(fake: FakeElevenLabs) -> ElevenLabsClient:
    return ElevenLabsClient(KEY, fake.base_url, allow_loopback=True)


def _no_io(method: str, url: str, headers: Mapping[str, str], body: bytes | None) -> tuple[int, dict[str, str], bytes]:
    raise AssertionError(f"transport must not be called: {method} {url}")


class _Recorder:
    """Injected transport that returns one scripted response and records calls."""

    def __init__(self, status: int, headers: dict[str, str], body: bytes) -> None:
        self.response = (status, headers, body)
        self.calls: list[tuple[str, str, dict[str, str], bytes | None]] = []

    def __call__(
        self, method: str, url: str, headers: Mapping[str, str], body: bytes | None
    ) -> tuple[int, dict[str, str], bytes]:
        self.calls.append((method, url, dict(headers), body))
        return self.response


# ----------------------------------------------------------------- allowlist


@pytest.mark.parametrize("base", sorted(ALLOWED_API_BASES))
def test_allowlisted_bases_are_accepted(base: str) -> None:
    ElevenLabsClient(KEY, base, transport=_no_io)
    ElevenLabsClient(KEY, base + "/", transport=_no_io)


def test_allowlist_is_the_documented_server_list() -> None:
    assert ALLOWED_API_BASES == {
        "https://api.elevenlabs.io",
        "https://api.us.elevenlabs.io",
        "https://api.eu.residency.elevenlabs.io",
        "https://api.in.residency.elevenlabs.io",
        "https://api.sg.residency.elevenlabs.io",
    }


@pytest.mark.parametrize(
    "base",
    [
        "https://evil.example.com",
        "http://api.elevenlabs.io",
        "https://api.elevenlabs.io.evil.example.com",
        "https://api.elevenlabs.io/v1",
        "https://user@api.elevenlabs.io",
        "https://api.elevenlabs.io:8443",
        "",
        "http://localhost:8080",
        "https://127.0.0.1:8080",
        "http://127.0.0.1",
        "http://127.0.0.1:8080/v1",
        "http://127.0.0.2:8080",
    ],
)
def test_rejected_bases_raise_value_error_even_with_loopback_flag(base: str) -> None:
    for flag in (False, True):
        with pytest.raises(ValueError):
            ElevenLabsClient(KEY, base, transport=_no_io, allow_loopback=flag)


def test_loopback_rejected_unless_flag_set(fake: FakeElevenLabs) -> None:
    with pytest.raises(ValueError):
        ElevenLabsClient(KEY, fake.base_url)
    assert fake.total_requests == 0
    _client(fake).list_models()
    assert fake.total_requests == 1


def test_planted_negative_non_allowlisted_base_makes_zero_requests(fake: FakeElevenLabs) -> None:
    # Both bases really reach the fake if the client allowed them.
    port = fake.base_url.rsplit(":", 1)[1]
    for base, flag in ((fake.base_url, False), (f"http://localhost:{port}", True)):
        with pytest.raises(ValueError):
            ElevenLabsClient(KEY, base, allow_loopback=flag).list_models()
    assert fake.total_requests == 0
    # Positive control: the counter does see a request from an allowed client.
    _client(fake).subscription()
    assert fake.counts == {("GET", "/v1/user/subscription"): 1}


def test_empty_api_key_rejected() -> None:
    with pytest.raises(ValueError):
        ElevenLabsClient("", "https://api.elevenlabs.io", transport=_no_io)


# ----------------------------------------------------------------- GET endpoints


def test_api_key_header_is_sent(fake: FakeElevenLabs) -> None:
    assert _client(fake).list_models() == fake_elevenlabs.MODELS
    assert fake.counts == {("GET", "/v1/models"): 1}
    assert fake.requests[-1].headers["xi-api-key"] == KEY


def test_subscription_and_voices_paths(fake: FakeElevenLabs) -> None:
    client = _client(fake)
    assert client.subscription() == fake_elevenlabs.SUBSCRIPTION
    assert client.list_voices() == fake_elevenlabs.VOICES
    assert fake.counts == {("GET", "/v1/user/subscription"): 1, ("GET", "/v2/voices"): 1}
    assert all(r.headers["xi-api-key"] == KEY for r in fake.requests)


def _voices(prefix: str, n: int) -> list[dict[str, str]]:
    return [{"voice_id": f"{prefix}_{i}", "name": f"{prefix} {i}"} for i in range(n)]


def test_list_voices_follows_next_page_token(fake: FakeElevenLabs) -> None:
    page1, page2 = _voices("a", 10), _voices("b", 22)
    fake.voice_pages = {
        None: {"voices": page1, "has_more": True, "total_count": 32, "next_page_token": "tok2"},
        "tok2": {"voices": page2, "has_more": False, "total_count": 32, "next_page_token": None},
    }
    doc = _client(fake).list_voices()
    assert [v["voice_id"] for v in doc["voices"]] == [v["voice_id"] for v in page1 + page2]
    assert doc["has_more"] is False
    assert fake.counts == {("GET", "/v2/voices"): 2}
    first, second = fake.requests
    assert "next_page_token" not in first.query
    assert second.query["next_page_token"] == ["tok2"]
    assert all(r.query["page_size"] == ["100"] for r in fake.requests)
    assert all(r.headers["xi-api-key"] == KEY for r in fake.requests)


def test_list_voices_stops_on_repeated_token(fake: FakeElevenLabs) -> None:
    fake.voice_pages = {
        None: {"voices": _voices("a", 1), "has_more": True, "next_page_token": "loop"},
        "loop": {"voices": _voices("b", 1), "has_more": True, "next_page_token": "loop"},
    }
    with pytest.raises(ElevenLabsError) as info:
        _client(fake).list_voices()
    assert "repeated" in info.value.message
    assert fake.counts == {("GET", "/v2/voices"): 2}


def test_list_voices_page_cap_bounds_requests(fake: FakeElevenLabs) -> None:
    import el_client

    cap = el_client.MAX_VOICE_PAGES
    fake.voice_pages = {None: {"voices": [], "has_more": True, "next_page_token": "t1"}}
    for i in range(1, cap + 5):
        fake.voice_pages[f"t{i}"] = {"voices": [], "has_more": True, "next_page_token": f"t{i + 1}"}
    with pytest.raises(ElevenLabsError) as info:
        _client(fake).list_voices()
    assert str(cap) in info.value.message
    assert fake.counts == {("GET", "/v2/voices"): cap}


def test_list_voices_has_more_without_token_is_an_error(fake: FakeElevenLabs) -> None:
    fake.voice_pages = {None: {"voices": _voices("a", 1), "has_more": True, "next_page_token": None}}
    with pytest.raises(ElevenLabsError):
        _client(fake).list_voices()
    assert fake.counts == {("GET", "/v2/voices"): 1}


# ----------------------------------------------------------------- with-timestamps


def test_output_format_is_query_param_not_body(fake: FakeElevenLabs) -> None:
    _client(fake).tts_with_timestamps(VOICE, "Hi.", MODEL, "wav_24000")
    rec = fake.requests[-1]
    assert rec.method == "POST"
    assert rec.path == f"/v1/text-to-speech/{VOICE}/with-timestamps"
    assert rec.query == {"output_format": ["wav_24000"]}
    assert json.loads(rec.body) == {"text": "Hi.", "model_id": MODEL}
    assert rec.headers["xi-api-key"] == KEY
    assert rec.headers["content-type"] == "application/json"
    assert fake.counts == {("POST", rec.path): 1}


def test_audio_base64_decodes_to_riff_wav(fake: FakeElevenLabs) -> None:
    result = _client(fake).tts_with_timestamps(VOICE, "Hi.", MODEL, "wav_24000")
    assert result.audio_bytes.startswith(b"RIFF")
    assert result.audio_bytes[8:12] == b"WAVE"
    with wave.open(io.BytesIO(result.audio_bytes), "rb") as w:
        assert (w.getframerate(), w.getnchannels()) == (24000, 1)
    assert result.alignment == fake_elevenlabs.ALIGNMENT
    assert result.normalized_alignment == fake_elevenlabs.ALIGNMENT


def test_request_id_header_is_captured(fake: FakeElevenLabs) -> None:
    result = _client(fake).tts_with_timestamps(VOICE, "Hi.", MODEL, "wav_24000")
    assert result.request_id == fake_elevenlabs.REQUEST_ID


def test_injected_transport_is_used_and_missing_request_id_is_none() -> None:
    body = json.dumps({"audio_base64": "UklGRg==", "alignment": None}).encode()
    rec = _Recorder(200, {"Content-Type": "application/json"}, body)
    client = ElevenLabsClient(KEY, "https://api.elevenlabs.io", transport=rec)
    result = client.tts_with_timestamps("v/1", "Hi.", MODEL, "wav_24000")
    assert result.audio_bytes == b"RIFF"
    assert result.request_id is None
    assert result.alignment is None and result.normalized_alignment is None
    [(method, url, headers, sent)] = rec.calls
    assert method == "POST"
    assert url == "https://api.elevenlabs.io/v1/text-to-speech/v%2F1/with-timestamps?output_format=wav_24000"
    assert headers["xi-api-key"] == KEY
    assert sent is not None and json.loads(sent) == {"text": "Hi.", "model_id": MODEL}


def test_empty_voice_id_rejected_before_io() -> None:
    client = ElevenLabsClient(KEY, "https://api.elevenlabs.io", transport=_no_io)
    with pytest.raises(ValueError):
        client.tts_with_timestamps("", "Hi.", MODEL, "wav_24000")


@pytest.mark.parametrize(
    "body",
    [b"not json", b"[]", b'{"alignment": null}', b'{"audio_base64": "@@not base64@@"}'],
)
def test_bad_success_body_raises(body: bytes) -> None:
    client = ElevenLabsClient(KEY, "https://api.elevenlabs.io", transport=_Recorder(200, {}, body))
    with pytest.raises(ElevenLabsError) as info:
        client.tts_with_timestamps(VOICE, "Hi.", MODEL, "wav_24000")
    assert info.value.status == 200


# ----------------------------------------------------------------- errors and redaction


def test_planted_negative_401_echoing_key_is_redacted(fake: FakeElevenLabs) -> None:
    body = json.dumps({"detail": {"status": "invalid_api_key", "message": f"Invalid key {KEY}"}}).encode()
    fake.scripted_error = (401, body)
    assert KEY.encode() in body  # the negative is really planted
    with pytest.raises(ElevenLabsError) as info:
        _client(fake).list_models()
    err = info.value
    assert err.status == 401
    assert "invalid_api_key" in str(err)
    assert "***" in str(err)
    for text in (str(err), repr(err), err.message, repr(err.args)):
        assert KEY not in text
    assert fake.total_requests == 1


def test_key_straddling_truncation_limit_is_not_partly_leaked(fake: FakeElevenLabs) -> None:
    fake.scripted_error = (403, b"x" * (MAX_ERROR_BODY_CHARS - 5) + KEY.encode())
    with pytest.raises(ElevenLabsError) as info:
        _client(fake).subscription()
    assert "sk_te" not in str(info.value)
    assert "sk_te" not in repr(info.value)


def test_error_status_is_not_retried(fake: FakeElevenLabs) -> None:
    fake.scripted_error = (500, b'{"detail": "boom"}')
    with pytest.raises(ElevenLabsError) as info:
        _client(fake).tts_with_timestamps(VOICE, "Hi.", MODEL, "wav_24000")
    assert info.value.status == 500
    assert fake.counts == {("POST", f"/v1/text-to-speech/{VOICE}/with-timestamps"): 1}


def test_transport_failure_is_redacted_with_unknown_status() -> None:
    calls: list[str] = []

    def broken(method: str, url: str, headers: Mapping[str, str], body: bytes | None) -> tuple[int, dict[str, str], bytes]:
        calls.append(url)
        raise OSError(f"connection reset, key was {KEY}")

    client = ElevenLabsClient(KEY, "https://api.elevenlabs.io", transport=broken)
    with pytest.raises(ElevenLabsError) as info:
        client.list_voices()
    err = info.value
    assert err.status is None
    assert KEY not in str(err) and KEY not in repr(err)
    assert err.__cause__ is None and err.__suppress_context__
    assert len(calls) == 1


def test_client_repr_hides_key() -> None:
    client = ElevenLabsClient(KEY, "https://api.elevenlabs.io", transport=_no_io)
    assert KEY not in repr(client)
    assert "***" in repr(client)
