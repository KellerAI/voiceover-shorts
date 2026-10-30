"""Tests for server/tts_mcp.py, driven over real stdio (a subprocess speaking JSON-RPC lines).

Every test talks to the loopback fake in tests/fake_elevenlabs.py; nothing here reaches the real
ElevenLabs API, so the live path is NOT proven by these tests.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import fake_elevenlabs  # noqa: E402
from fake_elevenlabs import REQUEST_ID, FakeElevenLabs  # noqa: E402

SERVER = ROOT / "server" / "tts_mcp.py"
KEY = "sk_test_DUMMY_KEY_0123456789abcdef"
VOICE = "voice_fake_1"
MODEL = "eleven_multilingual_v2"
MODERN = "2026-07-28"
LEGACY = "2025-11-25"
SETUP_HINT = "run the tts-setup skill"
TIMEOUT_S = 15.0
FFPROBE = shutil.which("ffprobe")
# The server's PATH: the system default plus ffprobe's directory (narrate.py verifies WAVs with it).
SERVER_PATH = os.pathsep.join(([os.path.dirname(FFPROBE)] if FFPROBE else []) + [os.defpath])
needs_ffprobe = pytest.mark.skipif(FFPROBE is None, reason="ffprobe is not installed; synthesize refuses without it")

META = {
    "io.modelcontextprotocol/protocolVersion": MODERN,
    "io.modelcontextprotocol/clientCapabilities": {},
    "io.modelcontextprotocol/clientInfo": {"name": "pytest", "version": "0"},
}

TOOL_NAMES = ["list_voices", "estimate", "synthesize"]
ESTIMATE_KEYS = {
    "chars",
    "model_id",
    "max_text_length",
    "character_cost_multiplier",
    "remaining_chars",
    "user_ceiling",
    "within_ceiling",
    "configured",
}
SYNTH_KEYS = {
    "status",
    "wav",
    "words",
    "alignment",
    "durationSec",
    "charsSent",
    "modelId",
    "voiceId",
    "outputFormat",
    "requestId",
}
MODELS_WITH_LIMITS = [
    {
        "model_id": MODEL,
        "name": "Fake Multilingual",
        "can_do_text_to_speech": True,
        "maximum_text_length_per_request": 10000,
        "model_rates": {"character_cost_multiplier": 1.0},
    }
]
NORMALIZED = {
    "characters": ["B", "y", "e", "."],
    "character_start_times_seconds": [0.0, 0.01, 0.02, 0.03],
    "character_end_times_seconds": [0.01, 0.02, 0.03, 0.04],
}


class DistinctNormalizedFake(FakeElevenLabs):
    """Same fake, but the POST's normalized_alignment spells different words than alignment."""

    @staticmethod
    def _route(method: str, path: str) -> tuple[int, Any, dict[str, str]]:
        status, payload, extra = FakeElevenLabs._route(method, path)
        if method == "POST" and status == 200:
            payload = dict(payload, normalized_alignment=NORMALIZED)
        return status, payload, extra


class McpProc:
    """The server as a subprocess; one JSON-RPC message per line on stdin/stdout."""

    def __init__(self, env: dict[str, str]) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, "-I", "-B", str(SERVER)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=str(ROOT),
        )
        self.lines: queue.Queue[bytes] = queue.Queue()
        self.raw_out: list[bytes] = []
        self._next_id = 0
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self.raw_out.append(line)
            self.lines.put(line)
        self.lines.put(b"")  # EOF sentinel: a dead server fails recv() at once, not after TIMEOUT_S

    def send(self, message: dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(message).encode("utf-8") + b"\n")
        self.proc.stdin.flush()

    def recv(self) -> dict[str, Any]:
        line = self.lines.get(timeout=TIMEOUT_S)
        if not line:
            self.lines.put(b"")
            raise AssertionError(f"server closed stdout (exit {self.proc.poll()})")
        assert line.endswith(b"\n") and b"\n" not in line[:-1]
        message: dict[str, Any] = json.loads(line)
        return message

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._next_id += 1
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": self._next_id, "method": method}
        if params is not None:
            message["params"] = params
        self.send(message)
        reply = self.recv()
        assert reply.get("jsonrpc") == "2.0"
        assert reply.get("id") == self._next_id
        return reply

    def close(self) -> tuple[int, bytes, bytes]:
        """EOF on stdin, then wait; a server that will not exit is killed and reaped (and fails the test)."""
        assert self.proc.stdin is not None
        try:
            self.proc.stdin.close()
        except BrokenPipeError:
            pass
        try:
            code = self.proc.wait(timeout=TIMEOUT_S)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=TIMEOUT_S)
            code = -9
        self._reader.join(timeout=TIMEOUT_S)
        assert self.proc.stderr is not None
        err = self.proc.stderr.read()
        self.proc.stderr.close()
        assert self.proc.stdout is not None
        self.proc.stdout.close()
        return code, b"".join(self.raw_out), err


# ------------------------------------------------------------------ fixtures


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeElevenLabs]:
    monkeypatch.setattr(fake_elevenlabs, "MODELS", MODELS_WITH_LIMITS)
    with DistinctNormalizedFake() as server:
        yield server


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    d = tmp_path / "plugin-data"
    d.mkdir()
    return d


def server_env(fake: FakeElevenLabs, data_dir: Path, **overrides: str | None) -> dict[str, str]:
    """A from-scratch environment: nothing (no real key, no proxy) is inherited from the shell."""
    env: dict[str, str | None] = {
        "PATH": SERVER_PATH,
        "no_proxy": "*",
        "NO_PROXY": "*",
        "ELEVENLABS_API_KEY": KEY,
        "VOS_TTS_VOICE_ID": VOICE,
        "VOS_TTS_MODEL_ID": MODEL,
        "VOS_TTS_OUTPUT_FORMAT": "wav_24000",
        "VOS_TTS_MAX_CHARS_PER_CALL": "1500",
        "VOS_TTS_API_BASE": fake.base_url,
        "VOS_TTS_TEST_ALLOW_LOOPBACK": "1",
        "CLAUDE_PLUGIN_DATA": str(data_dir),
        "CLAUDE_PLUGIN_ROOT": str(ROOT),
    }
    env.update(overrides)
    return {k: v for k, v in env.items() if v is not None}


@pytest.fixture
def run_server() -> Iterator[Any]:
    """Start servers; on teardown assert clean EOF exit and that the key never reached stdout/stderr."""
    procs: list[McpProc] = []

    def start(env: dict[str, str]) -> McpProc:
        p = McpProc(env)
        procs.append(p)
        return p

    yield start
    closed = [p.close() for p in procs]  # every server is reaped before any assertion can fail
    for code, out, err in closed:
        assert code == 0, f"server exit {code}; stderr: {err.decode('utf-8', 'replace')[:2000]}"
        assert KEY.encode() not in out, "API key leaked to stdout"
        assert KEY.encode() not in err, "API key leaked to stderr"
        for line in out.splitlines():
            json.loads(line)  # stdout carries only JSON-RPC messages


def legacy_session(proc: McpProc) -> dict[str, Any]:
    reply = proc.request(
        "initialize",
        {"protocolVersion": LEGACY, "capabilities": {}, "clientInfo": {"name": "pytest", "version": "0"}},
    )
    proc.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    return reply


def modern(proc: McpProc, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    p = dict(params or {})
    p["_meta"] = META
    return proc.request(method, p)


def call(proc: McpProc, name: str, arguments: dict[str, Any], era: str = "modern") -> dict[str, Any]:
    params = {"name": name, "arguments": arguments}
    reply = modern(proc, "tools/call", params) if era == "modern" else proc.request("tools/call", params)
    assert "result" in reply, reply
    result: dict[str, Any] = reply["result"]
    return result


def text_of(result: dict[str, Any]) -> str:
    return "\n".join(c["text"] for c in result["content"] if c.get("type") == "text")


def ok_payload(result: dict[str, Any]) -> dict[str, Any]:
    assert result.get("isError") is not True, text_of(result)
    payload: dict[str, Any] = result["structuredContent"]
    assert json.loads(result["content"][0]["text"]) == payload
    return payload


_OMIT = object()


def synth_args(text: str = "Hi.", **overrides: Any) -> dict[str, Any]:
    args: dict[str, Any] = {"text": text, "name": "take1", "confirm_spend": True, "max_chars": len(text)}
    args.update(overrides)
    return {k: v for k, v in args.items() if v is not _OMIT}



# ------------------------------------------------------------- handshake eras


def test_legacy_initialize_handshake(fake: FakeElevenLabs, data_dir: Path, run_server: Any) -> None:
    proc = run_server(server_env(fake, data_dir))
    reply = legacy_session(proc)
    result = reply["result"]
    assert result["protocolVersion"] == LEGACY
    assert "tools" in result["capabilities"]
    assert result["serverInfo"]["name"]
    assert result["serverInfo"]["version"]
    # notifications/initialized got no reply: the next reply is the ping's (id checked in request()).
    assert proc.request("ping")["result"] == {}
    listed = proc.request("tools/list")["result"]
    assert [t["name"] for t in listed["tools"]] == TOOL_NAMES


def test_legacy_initialize_unknown_version_gets_latest_legacy(
    fake: FakeElevenLabs, data_dir: Path, run_server: Any
) -> None:
    proc = run_server(server_env(fake, data_dir))
    reply = proc.request("initialize", {"protocolVersion": "2024-01-01", "capabilities": {}, "clientInfo": {}})
    assert reply["result"]["protocolVersion"] == LEGACY


@pytest.mark.parametrize("version", ["2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"])
def test_legacy_initialize_echoes_supported_versions(
    version: str, fake: FakeElevenLabs, data_dir: Path, run_server: Any
) -> None:
    proc = run_server(server_env(fake, data_dir))
    reply = proc.request("initialize", {"protocolVersion": version, "capabilities": {}, "clientInfo": {}})
    assert reply["result"]["protocolVersion"] == version


def test_modern_server_discover(fake: FakeElevenLabs, data_dir: Path, run_server: Any) -> None:
    proc = run_server(server_env(fake, data_dir))
    result = modern(proc, "server/discover")["result"]
    assert result["resultType"] == "complete"
    assert result["supportedVersions"] == [MODERN]
    assert "tools" in result["capabilities"]
    assert result["_meta"]["io.modelcontextprotocol/serverInfo"]["name"]
    assert isinstance(result["ttlMs"], int) and result["ttlMs"] >= 0
    assert result["cacheScope"] in ("public", "private")


def test_modern_unsupported_version_is_error_32022(fake: FakeElevenLabs, data_dir: Path, run_server: Any) -> None:
    proc = run_server(server_env(fake, data_dir))
    meta = dict(META, **{"io.modelcontextprotocol/protocolVersion": "1900-01-01"})
    reply = proc.request("server/discover", {"_meta": meta})
    assert reply["error"]["code"] == -32022
    assert reply["error"]["data"] == {"supported": [MODERN], "requested": "1900-01-01"}


def test_modern_request_missing_client_capabilities_is_invalid_params(
    fake: FakeElevenLabs, data_dir: Path, run_server: Any
) -> None:
    proc = run_server(server_env(fake, data_dir))
    reply = proc.request("tools/list", {"_meta": {"io.modelcontextprotocol/protocolVersion": MODERN}})
    assert reply["error"]["code"] == -32602


def test_request_without_meta_or_initialize_is_rejected(fake: FakeElevenLabs, data_dir: Path, run_server: Any) -> None:
    proc = run_server(server_env(fake, data_dir))
    reply = proc.request("tools/list")
    assert reply["error"]["code"] == -32602
    assert fake.total_requests == 0


def test_both_eras_on_one_process(fake: FakeElevenLabs, data_dir: Path, run_server: Any) -> None:
    proc = run_server(server_env(fake, data_dir))
    legacy_session(proc)
    legacy = proc.request("tools/list")["result"]
    assert "resultType" not in legacy
    current = modern(proc, "tools/list")["result"]
    assert current["resultType"] == "complete"
    assert [t["name"] for t in current["tools"]] == TOOL_NAMES


def test_unknown_method_and_malformed_line(fake: FakeElevenLabs, data_dir: Path, run_server: Any) -> None:
    proc = run_server(server_env(fake, data_dir))
    assert modern(proc, "resources/list")["error"]["code"] == -32601
    assert proc.proc.stdin is not None
    proc.proc.stdin.write(b"{not json\n")
    proc.proc.stdin.flush()
    reply = proc.recv()
    assert reply["error"]["code"] == -32700 and reply["id"] is None


def _old_python() -> str | None:
    """An installed python3 older than 3.10 (macOS ships 3.9 at /usr/bin/python3), if any."""
    exe = shutil.which("python3", path=os.defpath)
    if exe is None:
        return None
    probe = subprocess.run([exe, "-c", "import sys; print(sys.version_info >= (3, 10))"],
                           capture_output=True, text=True, timeout=TIMEOUT_S, check=False)
    return exe if probe.stdout.strip() == "False" else None


OLD_PYTHON = _old_python()


@pytest.mark.skipif(OLD_PYTHON is None, reason="no python3 older than 3.10 on the default PATH")
def test_old_python_exits_with_a_clear_message() -> None:
    assert OLD_PYTHON is not None
    proc = subprocess.run([OLD_PYTHON, "-I", "-B", str(SERVER)], input=b"", capture_output=True,
                          env={"PATH": os.defpath}, timeout=TIMEOUT_S, check=False)
    assert proc.returncode == 1
    assert b"needs Python 3.10 or newer" in proc.stderr
    assert proc.stdout == b""


# ------------------------------------------------------------------ tools/list


@pytest.mark.parametrize("era", ["legacy", "modern"])
def test_tools_list_matches_interface(era: str, fake: FakeElevenLabs, data_dir: Path, run_server: Any) -> None:
    proc = run_server(server_env(fake, data_dir))
    if era == "legacy":
        legacy_session(proc)
        result = proc.request("tools/list")["result"]
    else:
        result = modern(proc, "tools/list")["result"]
        assert result["resultType"] == "complete"
        assert isinstance(result["ttlMs"], int) and result["ttlMs"] >= 0
        assert result["cacheScope"] == "public"
    tools = {t["name"]: t for t in result["tools"]}
    assert [t["name"] for t in result["tools"]] == TOOL_NAMES

    lv = tools["list_voices"]["inputSchema"]
    assert lv == {"type": "object", "properties": {}, "additionalProperties": False}

    est = tools["estimate"]["inputSchema"]
    assert est["type"] == "object" and est["additionalProperties"] is False
    assert set(est["properties"]) == {"text"} and est["required"] == ["text"]
    assert est["properties"]["text"]["type"] == "string"

    syn = tools["synthesize"]["inputSchema"]
    assert syn["type"] == "object" and syn["additionalProperties"] is False
    assert set(syn["properties"]) == {"text", "name", "confirm_spend", "max_chars", "out_dir"}
    assert syn["required"] == ["text", "name", "confirm_spend", "max_chars"]
    assert syn["properties"]["text"]["type"] == "string"
    assert syn["properties"]["name"] == {
        "type": "string",
        "pattern": "^[A-Za-z0-9._-]+$",
        "description": syn["properties"]["name"]["description"],
    }
    assert syn["properties"]["confirm_spend"]["type"] == "boolean"
    assert syn["properties"]["max_chars"]["type"] == "integer"
    assert syn["properties"]["out_dir"]["type"] == "string"
    assert "billed" in tools["synthesize"]["description"].lower()
    assert fake.total_requests == 0


def test_unknown_tool_is_protocol_error(fake: FakeElevenLabs, data_dir: Path, run_server: Any) -> None:
    proc = run_server(server_env(fake, data_dir))
    reply = modern(proc, "tools/call", {"name": "speak", "arguments": {}})
    assert reply["error"]["code"] == -32602
    assert fake.total_requests == 0


# ---------------------------------------------------------- list_voices, estimate


@pytest.mark.parametrize("era", ["legacy", "modern"])
def test_list_voices_shape(era: str, fake: FakeElevenLabs, data_dir: Path, run_server: Any) -> None:
    proc = run_server(server_env(fake, data_dir))
    if era == "legacy":
        legacy_session(proc)
    payload = ok_payload(call(proc, "list_voices", {}, era))
    assert payload == {"voices": [{"voice_id": VOICE, "name": "Fake Voice", "category": None}]}
    assert fake.counts[("GET", "/v2/voices")] == 1
    assert fake.total_requests == 1
    assert fake.requests[0].headers["xi-api-key"] == KEY


def test_list_voices_returns_every_page(fake: FakeElevenLabs, data_dir: Path, run_server: Any) -> None:
    page1 = [{"voice_id": f"a{i}", "name": f"A {i}", "category": "premade"} for i in range(10)]
    page2 = [{"voice_id": f"b{i}", "name": f"B {i}", "category": "cloned"} for i in range(22)]
    fake.voice_pages = {
        None: {"voices": page1, "has_more": True, "total_count": 32, "next_page_token": "tok2"},
        "tok2": {"voices": page2, "has_more": False, "total_count": 32, "next_page_token": None},
    }
    proc = run_server(server_env(fake, data_dir))
    payload = ok_payload(call(proc, "list_voices", {}, "modern"))
    assert payload == {"voices": page1 + page2}
    assert fake.counts == {("GET", "/v2/voices"): 2}


def test_estimate_shape_and_two_free_gets(fake: FakeElevenLabs, data_dir: Path, run_server: Any) -> None:
    proc = run_server(server_env(fake, data_dir))
    payload = ok_payload(call(proc, "estimate", {"text": "Hello there."}))
    assert set(payload) == ESTIMATE_KEYS
    assert payload == {
        "chars": 12,
        "model_id": MODEL,
        "max_text_length": 10000,
        "character_cost_multiplier": 1.0,
        "remaining_chars": 10000,
        "user_ceiling": 1500,
        "within_ceiling": True,
        "configured": True,
    }
    assert fake.counts[("GET", "/v1/models")] == 1
    assert fake.counts[("GET", "/v1/user/subscription")] == 1
    assert fake.total_requests == 2


def test_estimate_over_user_ceiling(fake: FakeElevenLabs, data_dir: Path, run_server: Any) -> None:
    proc = run_server(server_env(fake, data_dir, VOS_TTS_MAX_CHARS_PER_CALL="5"))
    payload = ok_payload(call(proc, "estimate", {"text": "Hello there."}))
    assert payload["user_ceiling"] == 5
    assert payload["within_ceiling"] is False


@pytest.mark.parametrize("key", ["", "${user_config.elevenlabs_api_key}", None])
def test_unconfigured_key(key: str | None, fake: FakeElevenLabs, data_dir: Path, run_server: Any) -> None:
    proc = run_server(server_env(fake, data_dir, ELEVENLABS_API_KEY=key))
    est = call(proc, "estimate", {"text": "Hi."})
    assert est.get("isError") is not True
    assert est["structuredContent"]["configured"] is False
    assert set(est["structuredContent"]) == ESTIMATE_KEYS
    assert SETUP_HINT in text_of(est)
    for name, args in (("list_voices", {}), ("synthesize", synth_args())):
        result = call(proc, name, args)
        assert result["isError"] is True
        assert SETUP_HINT in text_of(result)
    assert fake.total_requests == 0
    assert not (data_dir / "narration").exists() or not any((data_dir / "narration").iterdir())


# ------------------------------------------------------------------ synthesize


@pytest.mark.parametrize("confirm", [_OMIT, False, "true", 1, None])
def test_unconfirmed_synthesize_makes_zero_requests(
    confirm: Any, fake: FakeElevenLabs, data_dir: Path, run_server: Any
) -> None:
    proc = run_server(server_env(fake, data_dir))
    result = call(proc, "synthesize", synth_args(confirm_spend=confirm))
    assert result["isError"] is True
    assert "confirm_spend" in text_of(result)
    assert fake.total_requests == 0
    assert fake.counts[("POST", f"/v1/text-to-speech/{VOICE}/with-timestamps")] == 0
    narration = data_dir / "narration"
    assert not narration.exists() or not any(narration.iterdir())


@pytest.mark.parametrize("max_chars", [2, 1501, True, "3", 3.5, _OMIT])
def test_invalid_max_chars_makes_zero_requests(
    max_chars: Any, fake: FakeElevenLabs, data_dir: Path, run_server: Any
) -> None:
    proc = run_server(server_env(fake, data_dir))
    result = call(proc, "synthesize", synth_args("Hi.", max_chars=max_chars))
    assert result["isError"] is True
    assert "max_chars" in text_of(result)
    assert fake.total_requests == 0


@pytest.mark.parametrize(
    "overrides",
    [
        {"name": "../escape"},
        {"name": "a b"},
        {"name": ""},
        {"text": "   "},
        {"out_dir": "relative/dir"},
        {"out_dir": str(ROOT / "narration")},
        {"extra": 1},
    ],
)
def test_invalid_arguments_make_zero_requests(
    overrides: dict[str, Any], fake: FakeElevenLabs, data_dir: Path, run_server: Any
) -> None:
    proc = run_server(server_env(fake, data_dir))
    args = synth_args(**overrides)
    if "text" in overrides:
        args["max_chars"] = max(1, len(args["text"]))
    result = call(proc, "synthesize", args)
    assert result["isError"] is True
    assert fake.total_requests == 0
    assert not (ROOT / "narration").exists()


@pytest.mark.parametrize("suffix", [".wav", ".words.json", ".alignment.json"])
def test_existing_output_file_makes_zero_requests(
    suffix: str, fake: FakeElevenLabs, data_dir: Path, run_server: Any
) -> None:
    out = data_dir / "narration"
    out.mkdir()
    existing = out / f"take1{suffix}"
    existing.write_text("keep me", encoding="utf-8")
    proc = run_server(server_env(fake, data_dir))
    result = call(proc, "synthesize", synth_args())
    assert result["isError"] is True
    assert "exists" in text_of(result)
    assert fake.total_requests == 0
    assert existing.read_text(encoding="utf-8") == "keep me"


def test_missing_voice_id_makes_zero_requests(fake: FakeElevenLabs, data_dir: Path, run_server: Any) -> None:
    proc = run_server(server_env(fake, data_dir, VOS_TTS_VOICE_ID="${user_config.voice_id}"))
    result = call(proc, "synthesize", synth_args())
    assert result["isError"] is True
    assert SETUP_HINT in text_of(result)
    assert fake.total_requests == 0


def test_loopback_base_without_test_flag_is_refused(fake: FakeElevenLabs, data_dir: Path, run_server: Any) -> None:
    proc = run_server(server_env(fake, data_dir, VOS_TTS_TEST_ALLOW_LOOPBACK=None))
    for name, args in (("list_voices", {}), ("estimate", {"text": "Hi."}), ("synthesize", synth_args())):
        result = call(proc, name, args)
        assert result["isError"] is True, name
        assert "api_base" in text_of(result)
    assert fake.total_requests == 0


@needs_ffprobe
@pytest.mark.parametrize("era", ["legacy", "modern"])
def test_confirmed_synthesize_makes_exactly_one_post(
    era: str, fake: FakeElevenLabs, data_dir: Path, run_server: Any
) -> None:
    """Planted positive for the zero-POST tests: the same counter sees the one paid request."""
    proc = run_server(server_env(fake, data_dir))
    if era == "legacy":
        legacy_session(proc)
    payload = ok_payload(call(proc, "synthesize", synth_args("Hi."), era))
    assert set(payload) == SYNTH_KEYS

    post_path = f"/v1/text-to-speech/{VOICE}/with-timestamps"
    assert fake.counts[("POST", post_path)] == 1
    assert fake.counts[("GET", "/v1/models")] == 1
    assert fake.counts[("GET", "/v1/user/subscription")] == 1
    assert fake.total_requests == 3
    post = next(r for r in fake.requests if r.method == "POST")
    assert post.query == {"output_format": ["wav_24000"]}
    assert json.loads(post.body) == {"text": "Hi.", "model_id": MODEL}

    out = (data_dir / "narration").resolve()
    assert payload["status"] == "OK"
    assert payload["wav"] == str(out / "take1.wav")
    assert payload["words"] == str(out / "take1.words.json")
    assert payload["alignment"] == str(out / "take1.alignment.json")
    assert payload["charsSent"] == 3
    assert payload["modelId"] == MODEL
    assert payload["voiceId"] == VOICE
    assert payload["outputFormat"] == "wav_24000"
    assert payload["requestId"] == REQUEST_ID
    assert payload["durationSec"] == pytest.approx(0.05, abs=1e-3)

    wav = Path(payload["wav"]).read_bytes()
    assert wav[:4] == b"RIFF" and wav[8:12] == b"WAVE"
    words = json.loads(Path(payload["words"]).read_text(encoding="utf-8"))
    # From `alignment` ("Hi."), never `normalized_alignment` ("Bye.").
    assert words == [{"id": "w0", "text": "Hi.", "start": 0.0, "end": 0.03}]
    saved = json.loads(Path(payload["alignment"]).read_text(encoding="utf-8"))
    assert saved["alignment"] == fake_elevenlabs.ALIGNMENT
    assert "audio_base64" not in saved


@needs_ffprobe
def test_synthesize_explicit_out_dir(fake: FakeElevenLabs, data_dir: Path, tmp_path: Path, run_server: Any) -> None:
    out = tmp_path / "chosen"
    proc = run_server(server_env(fake, data_dir))
    payload = ok_payload(call(proc, "synthesize", synth_args(out_dir=str(out))))
    assert Path(payload["wav"]).parent == out.resolve()
    assert fake.counts[("POST", f"/v1/text-to-speech/{VOICE}/with-timestamps")] == 1


@needs_ffprobe
def test_second_synthesize_same_name_is_refused_without_requests(
    fake: FakeElevenLabs, data_dir: Path, run_server: Any
) -> None:
    proc = run_server(server_env(fake, data_dir))
    ok_payload(call(proc, "synthesize", synth_args()))
    before = fake.total_requests
    again = call(proc, "synthesize", synth_args())
    assert again["isError"] is True
    assert fake.total_requests == before


@needs_ffprobe
def test_billed_failure_says_so_and_keeps_files(fake: FakeElevenLabs, data_dir: Path, run_server: Any) -> None:
    """The fake's alignment spells "Hi.", so sending "Hello." is billed but yields no words.json."""
    proc = run_server(server_env(fake, data_dir))
    result = call(proc, "synthesize", synth_args("Hello."))
    assert result["isError"] is True
    body = text_of(result)
    assert "billed" in body
    assert "Do not call synthesize again" in body
    assert fake.counts[("POST", f"/v1/text-to-speech/{VOICE}/with-timestamps")] == 1
    out = data_dir / "narration"
    assert (out / "take1.alignment.json").is_file()
    assert (out / "take1.wav").is_file()
    assert not (out / "take1.words.json").exists()


@pytest.mark.skipif(shutil.which("ffprobe", path=os.defpath) is not None, reason="ffprobe is on the default PATH")
def test_synthesize_without_ffprobe_makes_zero_requests(fake: FakeElevenLabs, data_dir: Path, run_server: Any) -> None:
    proc = run_server(server_env(fake, data_dir, PATH=os.defpath))
    result = call(proc, "synthesize", synth_args())
    assert result["isError"] is True
    assert "ffprobe" in text_of(result)
    assert fake.total_requests == 0


def test_synthesize_over_model_limit_makes_no_post(
    fake: FakeElevenLabs, data_dir: Path, monkeypatch: pytest.MonkeyPatch, run_server: Any
) -> None:
    small = [dict(MODELS_WITH_LIMITS[0], maximum_text_length_per_request=2)]
    monkeypatch.setattr(fake_elevenlabs, "MODELS", small)
    proc = run_server(server_env(fake, data_dir))
    result = call(proc, "synthesize", synth_args("Hi."))
    assert result["isError"] is True
    assert fake.counts[("POST", f"/v1/text-to-speech/{VOICE}/with-timestamps")] == 0


# ------------------------------------------------------------------ redaction


def test_401_echoing_the_key_is_redacted(fake: FakeElevenLabs, data_dir: Path, run_server: Any) -> None:
    fake.scripted_error = (401, json.dumps({"detail": f"invalid api key {KEY}"}).encode("utf-8"))
    proc = run_server(server_env(fake, data_dir))
    tools: list[tuple[str, dict[str, Any]]] = [("list_voices", {}), ("estimate", {"text": "Hi."})]
    if FFPROBE:  # without ffprobe, synthesize refuses before its GETs, so no 401 is ever seen
        tools.append(("synthesize", synth_args()))
    for name, args in tools:
        result = call(proc, name, args)
        assert result["isError"] is True, name
        body = text_of(result)
        assert "401" in body
        assert "***" in body
        assert KEY not in json.dumps(result)
    assert fake.counts[("POST", f"/v1/text-to-speech/{VOICE}/with-timestamps")] == 0
    # The run_server teardown also asserts the key is absent from all stdout and stderr bytes.


def _load_server_module() -> Any:
    import importlib.util

    spec = importlib.util.spec_from_file_location("tts_mcp_under_test", HERE.parent / "server" / "tts_mcp.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("short_key", ["Zq7Xw", "id"])
def test_short_key_is_redacted_without_breaking_framing(short_key: str) -> None:
    import io

    mod = _load_server_module()
    out = io.BytesIO()
    server = mod.Server(mod.load_config({"ELEVENLABS_API_KEY": short_key}), out)
    server._send({"jsonrpc": "2.0", "id": 7, "error": {"code": -1, "message": f"bad key {short_key}"}})
    line = out.getvalue().decode("ascii")
    reply = json.loads(line)  # framing intact even when the key collides with a protocol field name
    assert reply["id"] == 7 and reply["jsonrpc"] == "2.0"
    assert reply["error"]["message"] == "bad key ***"
