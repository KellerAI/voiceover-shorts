"""Spend-guard tests for scripts/narrate.py against the loopback fake (no real network I/O).

Every refusal test asserts the fake saw zero requests and pairs that with a positive control
showing the same setup does reach the fake. The live ElevenLabs path is NOT proven by these
tests; only a real, approved paid call is.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

_HERE = Path(__file__).resolve().parent
ROOT = _HERE.parent
for _p in (_HERE, ROOT / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import fake_elevenlabs  # noqa: E402
import narrate  # noqa: E402
import words  # noqa: E402
from el_client import ElevenLabsClient  # noqa: E402
from fake_elevenlabs import TTS_PATH_RE, FakeElevenLabs  # noqa: E402

CLI = ROOT / "scripts" / "narrate.py"
KEY = "sk_test_NARRATE_DUMMY_KEY_0123456789"
VOICE = "voice_fake_1"
MODEL = "eleven_multilingual_v2"
POST_PATH = f"/v1/text-to-speech/{VOICE}/with-timestamps"
GET_MODELS = ("GET", "/v1/models")
GET_SUB = ("GET", "/v1/user/subscription")
POST = ("POST", POST_PATH)
FULL_RUN = {GET_MODELS: 1, GET_SUB: 1, POST: 1}
SYNTH_KEYS = {
    "status", "wav", "words", "alignment", "durationSec", "charsSent",
    "modelId", "voiceId", "outputFormat", "requestId",
}
ESTIMATE_KEYS = {
    "chars", "model_id", "max_text_length", "character_cost_multiplier",
    "remaining_chars", "user_ceiling", "within_ceiling", "configured",
}


# ----------------------------------------------------------------- fakes and helpers


def alignment_for(text: str) -> dict[str, Any]:
    """An alignment that spells `text` exactly, 10 ms per character."""
    n = len(text)
    return {
        "characters": list(text),
        "character_start_times_seconds": [i * 0.01 for i in range(n)],
        "character_end_times_seconds": [(i + 1) * 0.01 for i in range(n)],
    }


def tts_payload(alignment: Any, normalized: Any = None, audio: bytes | None = None) -> dict[str, Any]:
    import base64

    raw = fake_elevenlabs.make_wav(seconds=1.0) if audio is None else audio
    return {
        "audio_base64": base64.b64encode(raw).decode("ascii"),
        "alignment": alignment,
        "normalized_alignment": alignment if normalized is None else normalized,
    }


class ScriptedFake(FakeElevenLabs):
    """The shared fake with an optional scripted POST response and subscription."""

    def __init__(
        self,
        post: tuple[int, Any] | None = None,
        subscription: dict[str, Any] | None = None,
    ) -> None:
        super().__init__()
        self.post = post
        self.subscription = subscription

    def _route(self, method: str, path: str) -> tuple[int, Any, dict[str, str]]:  # type: ignore[override]
        if method == "POST" and self.post is not None and TTS_PATH_RE.match(path):
            return self.post[0], self.post[1], {"request-id": fake_elevenlabs.REQUEST_ID}
        if method == "GET" and path == "/v1/user/subscription" and self.subscription is not None:
            return 200, self.subscription, {}
        return FakeElevenLabs._route(method, path)


@pytest.fixture(autouse=True)
def _no_proxies(monkeypatch: pytest.MonkeyPatch) -> None:
    # Keep loopback requests off any env or system proxy: no real network I/O.
    monkeypatch.setattr(urllib.request, "getproxies", dict)
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)


@pytest.fixture
def fake() -> Iterator[FakeElevenLabs]:
    with FakeElevenLabs() as server:
        yield server


def _client(fake: FakeElevenLabs) -> ElevenLabsClient:
    return ElevenLabsClient(KEY, fake.base_url, allow_loopback=True)


def _synth(fake: FakeElevenLabs, out_dir: Path, **overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "text": "Hi.",
        "name": "clip",
        "confirm_spend": True,
        "max_chars": 3,
        "user_ceiling": narrate.DEFAULT_MAX_CHARS_PER_CALL,
        "out_dir": str(out_dir),
        "voice_id": VOICE,
        "model_id": MODEL,
    }
    kwargs.update(overrides)
    return narrate.synthesize(_client(fake), **kwargs)


def _outputs(out_dir: Path, name: str = "clip") -> list[Path]:
    return [out_dir / f"{name}.alignment.json", out_dir / f"{name}.wav", out_dir / f"{name}.words.json"]


def _assert_refused_with_zero_requests(fake: FakeElevenLabs, out_dir: Path, **overrides: Any) -> None:
    with pytest.raises(narrate.Refused):
        _synth(fake, out_dir, **overrides)
    assert fake.total_requests == 0
    assert fake.counts == {}


def _assert_positive_control(fake: FakeElevenLabs, out_dir: Path, **overrides: Any) -> dict[str, Any]:
    before = dict(fake.counts)
    result = _synth(fake, out_dir, **overrides)
    delta = {k: v - before.get(k, 0) for k, v in fake.counts.items() if v - before.get(k, 0)}
    assert delta == FULL_RUN
    return result


# ----------------------------------------------------------------- pre-network refusals (0 requests)


@pytest.mark.parametrize("confirm", [False, None, "true", 1, "yes"])
def test_planted_negative_without_confirmation_fake_sees_zero_posts(
    fake: FakeElevenLabs, tmp_path: Path, confirm: object
) -> None:
    _assert_refused_with_zero_requests(fake, tmp_path, confirm_spend=confirm)
    assert not any(p.exists() for p in _outputs(tmp_path))
    # Positive control: the same call with confirm_spend=True reaches the fake.
    _assert_positive_control(fake, tmp_path)


def test_over_length_text_makes_zero_posts(fake: FakeElevenLabs, tmp_path: Path) -> None:
    text = "Hello there."
    _assert_refused_with_zero_requests(fake, tmp_path, text=text, max_chars=len(text) - 1)
    # Text longer than the ceiling, even with a matching max_chars.
    _assert_refused_with_zero_requests(fake, tmp_path, text=text, max_chars=len(text), user_ceiling=len(text) - 1)
    # max_chars above the ceiling.
    _assert_refused_with_zero_requests(fake, tmp_path, max_chars=11, user_ceiling=10)
    assert not any(p.exists() for p in _outputs(tmp_path))
    # Positive control at exactly len(text) == max_chars == ceiling ("Hi.", the fake's alignment).
    _assert_positive_control(fake, tmp_path, max_chars=3, user_ceiling=3)


@pytest.mark.parametrize("max_chars", [True, "3", 3.5, 0, -1, None])
def test_invalid_max_chars_makes_zero_requests(fake: FakeElevenLabs, tmp_path: Path, max_chars: object) -> None:
    _assert_refused_with_zero_requests(fake, tmp_path, max_chars=max_chars)


@pytest.mark.parametrize("ceiling", [0, 10001, True, "1500", 2.5])
def test_invalid_user_ceiling_makes_zero_requests(fake: FakeElevenLabs, tmp_path: Path, ceiling: object) -> None:
    _assert_refused_with_zero_requests(fake, tmp_path, user_ceiling=ceiling)


def test_integral_float_ceiling_and_max_chars_are_accepted(fake: FakeElevenLabs, tmp_path: Path) -> None:
    # plugin.json declares max_chars_per_call as a JSON number, so 1500.0 must work.
    _assert_positive_control(fake, tmp_path, max_chars=3.0, user_ceiling=1500.0)


@pytest.mark.parametrize("text", ["", "   ", "\n"])
def test_empty_text_makes_zero_requests(fake: FakeElevenLabs, tmp_path: Path, text: str) -> None:
    _assert_refused_with_zero_requests(fake, tmp_path, text=text, max_chars=max(len(text), 1))


@pytest.mark.parametrize("name", ["", "a/b", "../x", "a b", "clip\n", "ä"])
def test_bad_name_makes_zero_requests(fake: FakeElevenLabs, tmp_path: Path, name: str) -> None:
    _assert_refused_with_zero_requests(fake, tmp_path, name=name)


@pytest.mark.parametrize("output_format", ["mp3_44100_128", "wav_16000", ""])
def test_non_wav_output_format_makes_zero_requests(
    fake: FakeElevenLabs, tmp_path: Path, output_format: str
) -> None:
    _assert_refused_with_zero_requests(fake, tmp_path, output_format=output_format)


def test_empty_voice_id_makes_zero_requests(fake: FakeElevenLabs, tmp_path: Path) -> None:
    _assert_refused_with_zero_requests(fake, tmp_path, voice_id="")


@pytest.mark.parametrize("suffix", [".alignment.json", ".wav", ".words.json"])
def test_existing_output_file_makes_zero_requests_and_is_untouched(
    fake: FakeElevenLabs, tmp_path: Path, suffix: str
) -> None:
    existing = tmp_path / f"clip{suffix}"
    existing.write_bytes(b"keep me")
    _assert_refused_with_zero_requests(fake, tmp_path)
    assert existing.read_bytes() == b"keep me"
    assert [p for p in _outputs(tmp_path) if p.exists()] == [existing]
    # Positive control: a different name in the same directory runs.
    _assert_positive_control(fake, tmp_path, name="other")


def test_relative_out_dir_makes_zero_requests(fake: FakeElevenLabs) -> None:
    _assert_refused_with_zero_requests(fake, Path("relative/narration"))


def test_out_dir_inside_claude_plugin_root_makes_zero_requests(
    fake: FakeElevenLabs, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin_root = tmp_path / "plugin"
    plugin_root.mkdir()
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))
    _assert_refused_with_zero_requests(fake, plugin_root / "narration")
    assert not (plugin_root / "narration").exists()
    # Positive control: a sibling directory outside the plugin root runs.
    _assert_positive_control(fake, tmp_path / "data" / "narration")


def test_out_dir_inside_the_addon_checkout_makes_zero_requests(fake: FakeElevenLabs) -> None:
    target = ROOT / ".tmp" / "narration-guard-test"
    _assert_refused_with_zero_requests(fake, target)
    assert not target.exists()


def test_missing_ffprobe_makes_zero_requests(
    fake: FakeElevenLabs, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))
    _assert_refused_with_zero_requests(fake, tmp_path / "out")


# ----------------------------------------------------------------- free GETs, then refusal before the POST


def test_quota_too_small_makes_two_gets_and_no_post(tmp_path: Path) -> None:
    with ScriptedFake(subscription={"character_count": 9998, "character_limit": 10000}) as fake:
        with pytest.raises(narrate.Refused):
            _synth(fake, tmp_path)
        assert fake.counts == {GET_MODELS: 1, GET_SUB: 1}
    assert not any(p.exists() for p in _outputs(tmp_path))


def test_unreadable_quota_makes_no_post(tmp_path: Path) -> None:
    with ScriptedFake(subscription={"tier": "free"}) as fake:
        with pytest.raises(narrate.Refused):
            _synth(fake, tmp_path)
        assert fake.counts == {GET_MODELS: 1, GET_SUB: 1}


def test_unlisted_model_makes_no_post(fake: FakeElevenLabs, tmp_path: Path) -> None:
    with pytest.raises(narrate.Refused):
        _synth(fake, tmp_path, model_id="not_a_listed_model")
    assert POST not in fake.counts
    assert fake.counts[GET_MODELS] == 1


# ----------------------------------------------------------------- the paid call


def test_success_writes_three_files_and_returns_interface_keys(fake: FakeElevenLabs, tmp_path: Path) -> None:
    out_dir = tmp_path / "narration"  # created on demand
    result = _assert_positive_control(fake, out_dir)
    assert set(result) == SYNTH_KEYS
    alignment_path, wav_path, words_path = _outputs(out_dir)
    assert result["status"] == "OK"
    assert (result["alignment"], result["wav"], result["words"]) == tuple(
        str(p) for p in (alignment_path, wav_path, words_path)
    )
    assert result["charsSent"] == 3
    assert (result["modelId"], result["voiceId"], result["outputFormat"]) == (MODEL, VOICE, "wav_24000")
    assert result["requestId"] == fake_elevenlabs.REQUEST_ID
    assert result["durationSec"] == pytest.approx(0.05, abs=1e-3)

    rec = [r for r in fake.requests if r.method == "POST"][0]
    assert rec.query == {"output_format": ["wav_24000"]}
    assert json.loads(rec.body) == {"text": "Hi.", "model_id": MODEL}

    audio = wav_path.read_bytes()
    assert audio[:4] == b"RIFF" and audio[8:12] == b"WAVE"
    assert words.load_alignment(str(alignment_path)) == fake_elevenlabs.ALIGNMENT
    assert json.loads(words_path.read_text()) == words.alignment_to_words(fake_elevenlabs.ALIGNMENT, 0.05)


def test_wav_44100_is_passed_through_when_chosen(fake: FakeElevenLabs, tmp_path: Path) -> None:
    result = _synth(fake, tmp_path, output_format="wav_44100")
    assert result["outputFormat"] == "wav_44100"
    assert [r.query for r in fake.requests if r.method == "POST"] == [{"output_format": ["wav_44100"]}]


def test_words_are_built_from_alignment_never_normalized_alignment(tmp_path: Path) -> None:
    text = "I have 2 cats."
    normalized = alignment_for("I have two cats.")
    with ScriptedFake(post=(200, tts_payload(alignment_for(text), normalized=normalized))) as fake:
        result = _synth(fake, tmp_path, text=text, max_chars=len(text))
        assert fake.counts == FULL_RUN
    got = json.loads(Path(result["words"]).read_text())
    assert [w["text"] for w in got] == ["I", "have", "2", "cats."]


def test_post_http_error_is_not_retried_and_reported_possibly_billed(tmp_path: Path) -> None:
    with ScriptedFake(post=(500, {"detail": "boom"})) as fake:
        with pytest.raises(narrate.PossiblyBilled) as info:
            _synth(fake, tmp_path)
        assert fake.counts == FULL_RUN
    assert "billed" in str(info.value)
    assert not any(p.exists() for p in _outputs(tmp_path))


def test_unknown_outcome_is_reported_possibly_billed_without_retry(tmp_path: Path) -> None:
    calls: list[tuple[str, str]] = []

    def transport(method: str, url: str, headers: Mapping[str, str], body: bytes | None) -> tuple[int, dict[str, str], bytes]:
        calls.append((method, url))
        if method == "POST":
            raise TimeoutError(f"timed out, key {KEY}")
        if url.endswith("/v1/models"):
            return 200, {}, json.dumps(fake_elevenlabs.MODELS).encode()
        return 200, {}, json.dumps(fake_elevenlabs.SUBSCRIPTION).encode()

    client = ElevenLabsClient(KEY, "https://api.elevenlabs.io", transport=transport)
    with pytest.raises(narrate.PossiblyBilled) as info:
        narrate.synthesize(
            client, text="Hi.", name="clip", confirm_spend=True, max_chars=3, user_ceiling=1500,
            out_dir=str(tmp_path), voice_id=VOICE, model_id=MODEL,
        )
    assert [m for m, _ in calls] == ["GET", "GET", "POST"]
    message = str(info.value)
    assert "may" in message and "billed" in message
    assert KEY not in message and KEY not in repr(info.value)


def test_non_wav_audio_keeps_alignment_and_writes_no_wav(tmp_path: Path) -> None:
    payload = tts_payload(fake_elevenlabs.ALIGNMENT, audio=b"ID3\x04 not a wav file")
    with ScriptedFake(post=(200, payload)) as fake:
        with pytest.raises(narrate.PossiblyBilled):
            _synth(fake, tmp_path)
        assert fake.counts == FULL_RUN
    alignment_path, wav_path, words_path = _outputs(tmp_path)
    # Order: alignment.json is written first, so the paid alignment survives.
    assert words.load_alignment(str(alignment_path)) == fake_elevenlabs.ALIGNMENT
    assert not wav_path.exists() and not words_path.exists()


def test_truncated_alignment_keeps_alignment_and_wav_but_writes_no_words(tmp_path: Path) -> None:
    with ScriptedFake(post=(200, tts_payload(alignment_for("Hi")))) as fake:
        with pytest.raises(narrate.PossiblyBilled) as info:
            _synth(fake, tmp_path, text="Hi there.", max_chars=9)
        assert fake.counts == {GET_MODELS: 1, GET_SUB: 1, POST: 1}
    alignment_path, wav_path, words_path = _outputs(tmp_path)
    assert alignment_path.exists() and wav_path.read_bytes()[:4] == b"RIFF"
    assert not words_path.exists()
    assert "words.py" in str(info.value)


def test_null_alignment_keeps_wav_and_writes_no_words(tmp_path: Path) -> None:
    payload = tts_payload(None)
    payload["alignment"] = None
    with ScriptedFake(post=(200, payload)) as fake:
        with pytest.raises(narrate.PossiblyBilled):
            _synth(fake, tmp_path)
    alignment_path, wav_path, words_path = _outputs(tmp_path)
    assert alignment_path.exists() and wav_path.exists() and not words_path.exists()


# ----------------------------------------------------------------- estimate (free)


def test_estimate_makes_two_gets_and_no_post(fake: FakeElevenLabs) -> None:
    est = narrate.estimate(_client(fake), "Hello there.", MODEL, 1500)
    assert fake.counts == {GET_MODELS: 1, GET_SUB: 1}
    assert set(est) == ESTIMATE_KEYS
    assert est["chars"] == 12
    assert est["model_id"] == MODEL
    assert est["remaining_chars"] == 10000
    assert est["user_ceiling"] == 1500
    assert est["within_ceiling"] is True
    assert est["configured"] is True
    # The fake's model has neither field, so both are unknown rather than invented.
    assert est["max_text_length"] is None and est["character_cost_multiplier"] is None


def test_estimate_over_ceiling_is_not_within_ceiling(fake: FakeElevenLabs) -> None:
    est = narrate.estimate(_client(fake), "x" * 11, MODEL, 10)
    assert est["within_ceiling"] is False
    assert POST not in fake.counts


# ----------------------------------------------------------------- CLI (real subprocess against the fake)


def _env(key: str | None = KEY, **extra: str) -> dict[str, str]:
    # Minimal environment: no proxies (no_proxy=* bypasses env and system proxies for urllib).
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONDONTWRITEBYTECODE": "1", "no_proxy": "*"}
    if key is not None:
        env["ELEVENLABS_API_KEY"] = key
    env.update(extra)
    return env


def _run(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(CLI), *args], capture_output=True, text=True, env=env, timeout=60)


def _synth_args(fake: FakeElevenLabs, out_dir: Path, *extra: str, text: str = "Hi.") -> list[str]:
    return [
        "synthesize", "--text", text, "--name", "clip", "--voice-id", VOICE,
        "--max-chars", str(len(text)), "--out-dir", str(out_dir),
        "--api-base", fake.base_url, "--allow-loopback", *extra,
    ]


def test_cli_planted_negative_without_confirm_spend_zero_posts(fake: FakeElevenLabs, tmp_path: Path) -> None:
    proc = _run(_synth_args(fake, tmp_path), _env())
    assert proc.returncode == narrate.EXIT_NOT_SENT, proc.stderr
    assert fake.total_requests == 0
    assert "confirm" in proc.stderr
    assert not any(p.exists() for p in _outputs(tmp_path))
    # Positive control: the same command plus --confirm-spend makes exactly one POST.
    proc = _run(_synth_args(fake, tmp_path, "--confirm-spend"), _env())
    assert proc.returncode == 0, proc.stderr
    assert fake.counts == FULL_RUN
    result = json.loads(proc.stdout)
    assert set(result) == SYNTH_KEYS and result["status"] == "OK"
    assert [r.query for r in fake.requests if r.method == "POST"] == [{"output_format": ["wav_24000"]}]
    assert all(p.exists() for p in _outputs(tmp_path))


def test_cli_over_length_text_zero_posts(fake: FakeElevenLabs, tmp_path: Path) -> None:
    args = _synth_args(fake, tmp_path, "--confirm-spend", text="Hello there.")
    args[args.index("--max-chars") + 1] = "5"
    proc = _run(args, _env())
    assert proc.returncode == narrate.EXIT_NOT_SENT, proc.stderr
    assert fake.total_requests == 0
    # Over the default 1500 ceiling with a matching --max-chars.
    long_text = "a" * (narrate.DEFAULT_MAX_CHARS_PER_CALL + 1)
    proc = _run(_synth_args(fake, tmp_path, "--confirm-spend", text=long_text), _env())
    assert proc.returncode == narrate.EXIT_NOT_SENT, proc.stderr
    assert fake.total_requests == 0


@pytest.mark.parametrize("key", [None, "", "${user_config.elevenlabs_api_key}"])
def test_cli_unconfigured_key_says_run_tts_setup(fake: FakeElevenLabs, tmp_path: Path, key: str | None) -> None:
    proc = _run(_synth_args(fake, tmp_path, "--confirm-spend"), _env(key))
    assert proc.returncode == narrate.EXIT_NOT_SENT
    assert "run the tts-setup skill" in proc.stderr
    proc = _run(["estimate", "--text", "Hi.", "--api-base", fake.base_url, "--allow-loopback"], _env(key))
    assert proc.returncode == narrate.EXIT_NOT_SENT
    out = json.loads(proc.stdout)
    assert out["configured"] is False and "run the tts-setup skill" in out["message"]
    assert fake.total_requests == 0


def test_cli_estimate_is_free(fake: FakeElevenLabs) -> None:
    proc = _run(["estimate", "--text", "Hello there.", "--api-base", fake.base_url, "--allow-loopback"], _env())
    assert proc.returncode == 0, proc.stderr
    assert fake.counts == {GET_MODELS: 1, GET_SUB: 1}
    out = json.loads(proc.stdout)
    assert set(out) == ESTIMATE_KEYS
    assert out["chars"] == 12 and out["user_ceiling"] == 1500 and out["configured"] is True


def test_cli_text_file_is_sent_unchanged(fake: FakeElevenLabs, tmp_path: Path) -> None:
    text_file = tmp_path / "script.txt"
    text_file.write_text("Hi.", encoding="utf-8")
    args = _synth_args(fake, tmp_path / "out", "--confirm-spend")
    i = args.index("--text")
    args[i : i + 2] = ["--text-file", str(text_file)]
    proc = _run(args, _env())
    assert proc.returncode == 0, proc.stderr
    assert [json.loads(r.body)["text"] for r in fake.requests if r.method == "POST"] == ["Hi."]


def test_cli_out_dir_defaults_to_plugin_data_narration(fake: FakeElevenLabs, tmp_path: Path) -> None:
    args = _synth_args(fake, tmp_path, "--confirm-spend")
    i = args.index("--out-dir")
    del args[i : i + 2]
    proc = _run(args, _env(CLAUDE_PLUGIN_DATA=str(tmp_path / "data")))
    assert proc.returncode == 0, proc.stderr
    assert all(p.exists() for p in _outputs(tmp_path / "data" / "narration"))
    # Without --out-dir or CLAUDE_PLUGIN_DATA there is no default: usage error, no requests.
    before = fake.total_requests
    proc = _run(args, _env())
    assert proc.returncode == 2
    assert fake.total_requests == before


def test_cli_key_never_appears_in_output_or_files(fake: FakeElevenLabs, tmp_path: Path) -> None:
    # Success path.
    proc = _run(_synth_args(fake, tmp_path, "--confirm-spend"), _env())
    assert proc.returncode == 0, proc.stderr
    for text in (proc.stdout, proc.stderr, *(p.read_text(errors="replace") for p in _outputs(tmp_path))):
        assert KEY not in text
    # Planted negative: the server echoes the key in a 401 body on every request.
    body = json.dumps({"detail": {"status": "invalid_api_key", "message": f"Invalid key {KEY}"}}).encode()
    assert KEY.encode() in body
    fake.scripted_error = (401, body)
    for args in (
        _synth_args(fake, tmp_path / "second", "--confirm-spend"),
        ["estimate", "--text", "Hi.", "--api-base", fake.base_url, "--allow-loopback"],
    ):
        proc = _run(args, _env())
        assert proc.returncode != 0
        assert "invalid_api_key" in proc.stderr  # the error really surfaced
        assert KEY not in proc.stdout and KEY not in proc.stderr
    # Planted negative: the key inside a narrate-generated message (the out-dir path).
    leaky = tmp_path / KEY / "out"
    fake.scripted_error = None
    plugin_root = tmp_path / KEY
    proc = _run(_synth_args(fake, leaky, "--confirm-spend"), _env(CLAUDE_PLUGIN_ROOT=str(plugin_root)))
    assert proc.returncode == narrate.EXIT_NOT_SENT
    assert "***" in proc.stderr
    assert KEY not in proc.stdout and KEY not in proc.stderr
