#!/usr/bin/env python3
"""tts_mcp.py - stdio MCP server for voiceover-shorts-tts (standard library only).

Tools (contract: handoffs/voiceover-shorts-public/tts-addon-interface.md):
  list_voices  free, GET /v2/voices once per page, at most MAX_VOICE_PAGES (scripts/el_client.py)
  estimate     free, GET /v1/models + GET /v1/user/subscription (scripts/narrate.py estimate)
  synthesize   paid, behind the scripts/narrate.py spend guard: local checks, the two free GETs,
               then exactly one POST /with-timestamps, never retried

Protocol: dual-era, per modelcontextprotocol.io/specification/2026-07-28/basic/versioning.
  - Modern (2026-07-28): each request carries `_meta` io.modelcontextprotocol/protocolVersion and
    io.modelcontextprotocol/clientCapabilities; `server/discover` is implemented; results carry
    `resultType` and serverInfo in `_meta`; tools/list and server/discover carry ttlMs/cacheScope.
    A version other than 2026-07-28 gets UnsupportedProtocolVersionError (-32022).
  - Legacy (2024-11-05 .. 2025-11-25): an `initialize` request selects legacy semantics for this
    process; later requests without `_meta` are served under the negotiated legacy version.
  Transport: one JSON-RPC message per line on stdin/stdout; stdout carries nothing else.

Configuration comes only from the environment (.mcp.json `env`). A value that is empty or still a
literal `${...}` placeholder counts as unset.
  ELEVENLABS_API_KEY           the key (sensitive userConfig elevenlabs_api_key)
  VOS_TTS_VOICE_ID             voice to narrate with (required by synthesize)
  VOS_TTS_MODEL_ID             default eleven_multilingual_v2
  VOS_TTS_OUTPUT_FORMAT        wav_24000 (default) or wav_44100
  VOS_TTS_MAX_CHARS_PER_CALL   the user's ceiling, default 1500
  VOS_TTS_API_BASE             default https://api.elevenlabs.io (el_client allowlist applies)
  VOS_TTS_TEST_ALLOW_LOOPBACK  "1" lets tests point VOS_TTS_API_BASE at a 127.0.0.1 fake
  CLAUDE_PLUGIN_DATA           default output dir is ${CLAUDE_PLUGIN_DATA}/narration/
  CLAUDE_PLUGIN_ROOT           output is never written inside it
synthesize also needs ffprobe on PATH (narrate.py verifies the WAV with it).

The key is never logged; every line written to stdout has the key replaced by "***".
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

if sys.version_info < (3, 10):  # scripts/el_client.py builds `bytes | None` aliases at import time
    sys.stderr.write(f"tts_mcp.py needs Python 3.10 or newer; this is {sys.version.split()[0]}\n")
    sys.exit(1)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import narrate  # noqa: E402
from el_client import REDACTED, ElevenLabsClient, ElevenLabsError  # noqa: E402

SERVER_INFO = {"name": "voiceover-shorts-tts-elevenlabs", "version": "0.1.0"}
MODERN_VERSIONS = ("2026-07-28",)
LEGACY_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
META_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CLIENT_CAPS = "io.modelcontextprotocol/clientCapabilities"
META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"
CACHE_TTL_MS = 3_600_000

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
UNSUPPORTED_PROTOCOL_VERSION = -32022

ENV_KEY = "ELEVENLABS_API_KEY"
ENV_VOICE = "VOS_TTS_VOICE_ID"
ENV_MODEL = "VOS_TTS_MODEL_ID"
ENV_FORMAT = "VOS_TTS_OUTPUT_FORMAT"
ENV_MAX_CHARS = "VOS_TTS_MAX_CHARS_PER_CALL"
ENV_API_BASE = "VOS_TTS_API_BASE"
ENV_ALLOW_LOOPBACK = "VOS_TTS_TEST_ALLOW_LOOPBACK"
MIN_REDACT_LEN = 8

SETUP_HINT = "run the tts-setup skill"
NO_REQUEST = "No request was sent to ElevenLabs."
NOT_BILLED = "No paid request was sent."
DO_NOT_RETRY = "Do not call synthesize again in this run."
MAYBE_BILLED = (
    "The paid request may already have been billed; check your ElevenLabs history before trying again. "
    + DO_NOT_RETRY
)

INSTRUCTIONS = (
    "ElevenLabs narration for voiceover-shorts. estimate and list_voices are free. synthesize is billed to "
    "the user's ElevenLabs account: call estimate first, get the user's explicit yes for the exact text, "
    "then call synthesize once with confirm_spend true. Never retry synthesize. If a tool says to "
    f"{SETUP_HINT}, do that first."
)

TOOLS: list[dict[str, Any]] = [
    {
        "name": "list_voices",
        "title": "List ElevenLabs voices",
        "description": "Free. List the voices on the user's ElevenLabs account: voice_id, name and category.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "estimate",
        "title": "Estimate narration cost",
        "description": (
            "Free. Count the characters in text and check them against the model's per-request limit and the "
            "user's max_chars_per_call ceiling; also reports the account's remaining characters. Makes two "
            "free GET requests and no paid request. Returns configured:false when the key is not set."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string", "minLength": 1, "description": "Exact narration text."}},
            "required": ["text"],
            "additionalProperties": False,
        },
    },
    {
        "name": "synthesize",
        "title": "Synthesize narration (paid)",
        "description": (
            "PAID: billed to the user's ElevenLabs account. Turn text into a WAV plus words.json word timings "
            "with exactly one ElevenLabs request, never retried. Call only after estimate and after the user "
            "explicitly confirmed this exact text in this conversation. Fails without any request unless "
            "confirm_spend is true, len(text) <= max_chars <= the user's ceiling, and no output file exists."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "minLength": 1, "description": "The confirmed text, unchanged."},
                "name": {
                    "type": "string",
                    "pattern": "^[A-Za-z0-9._-]+$",
                    "description": "Output base name; files are <name>.wav, <name>.words.json, <name>.alignment.json.",
                },
                "confirm_spend": {
                    "type": "boolean",
                    "description": "Must be true: the user explicitly approved this paid request.",
                },
                "max_chars": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Confirmed character count: at least len(text), at most the user's ceiling.",
                },
                "out_dir": {
                    "type": "string",
                    "description": "Optional absolute output directory. Default: ${CLAUDE_PLUGIN_DATA}/narration/.",
                },
            },
            "required": ["text", "name", "confirm_spend", "max_chars"],
            "additionalProperties": False,
        },
    },
]
TOOL_ARGS = {t["name"]: frozenset(t["inputSchema"]["properties"]) for t in TOOLS}


class ToolError(Exception):
    """A tool execution error, reported to the model as an isError result."""


class RpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(code, message)
        self.code = code
        self.message = message
        self.data = data


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


# ------------------------------------------------------------------ configuration


def _setting(env: Mapping[str, str], name: str) -> str | None:
    value = env.get(name, "").strip()
    if not value or value.startswith("${"):
        return None
    return value


def _ceiling(raw: str | None) -> int | str:
    """The user's ceiling as an int; an unparseable value is kept as-is so narrate refuses it by name."""
    if raw is None:
        return narrate.DEFAULT_MAX_CHARS_PER_CALL
    try:
        number = float(raw)
    except ValueError:
        return raw
    return int(number) if number.is_integer() else raw


@dataclass(frozen=True)
class Config:
    key: str | None
    voice_id: str | None
    model_id: str
    output_format: str
    max_chars: int | str
    api_base: str
    allow_loopback: bool
    plugin_data: str | None
    plugin_root: str | None


def load_config(env: Mapping[str, str]) -> Config:
    key = _setting(env, ENV_KEY)
    return Config(
        key=key if narrate.key_configured(key) else None,
        voice_id=_setting(env, ENV_VOICE),
        model_id=_setting(env, ENV_MODEL) or narrate.DEFAULT_MODEL_ID,
        output_format=_setting(env, ENV_FORMAT) or narrate.DEFAULT_OUTPUT_FORMAT,
        max_chars=_ceiling(_setting(env, ENV_MAX_CHARS)),
        api_base=_setting(env, ENV_API_BASE) or narrate.DEFAULT_API_BASE,
        allow_loopback=env.get(ENV_ALLOW_LOOPBACK) == "1",
        plugin_data=_setting(env, "CLAUDE_PLUGIN_DATA"),
        plugin_root=_setting(env, "CLAUDE_PLUGIN_ROOT"),
    )


# ------------------------------------------------------------------ tools


class Tools:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg

    def _check_args(self, tool: str, args: dict[str, Any]) -> None:
        unknown = sorted(set(args) - TOOL_ARGS[tool])
        if unknown:
            raise ToolError(f"{tool}: unknown argument(s) {', '.join(unknown)}. {NO_REQUEST}")

    def _client(self) -> ElevenLabsClient:
        if self.cfg.key is None:
            raise ToolError(f"{narrate.TTS_SETUP_HINT}. {NO_REQUEST}")
        try:
            return ElevenLabsClient(self.cfg.key, self.cfg.api_base, allow_loopback=self.cfg.allow_loopback)
        except ValueError as exc:  # el_client redacts the key from this message
            raise ToolError(f"Invalid plugin configuration: {exc}. {NO_REQUEST}") from None

    @staticmethod
    def _text(args: dict[str, Any]) -> str:
        text = args.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ToolError(f"text must be a non-empty string. {NO_REQUEST}")
        return text

    def list_voices(self, args: dict[str, Any]) -> dict[str, Any]:
        self._check_args("list_voices", args)
        client = self._client()
        try:
            doc = client.list_voices()
        except ElevenLabsError as exc:
            raise ToolError(f"{exc}. Nothing was billed.") from None
        voices = doc.get("voices") if isinstance(doc, dict) else None
        if not isinstance(voices, list):
            raise ToolError("GET /v2/voices returned no voices list. Nothing was billed.")
        return {
            "voices": [
                {"voice_id": v.get("voice_id"), "name": v.get("name"), "category": v.get("category")}
                for v in voices
                if isinstance(v, dict)
            ]
        }

    def estimate(self, args: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
        self._check_args("estimate", args)
        text = self._text(args)
        if self.cfg.key is None:
            payload = {
                "chars": len(text),
                "model_id": self.cfg.model_id,
                "max_text_length": None,
                "character_cost_multiplier": None,
                "remaining_chars": None,
                "user_ceiling": self.cfg.max_chars,
                "within_ceiling": False,
                "configured": False,
            }
            return payload, f"{narrate.TTS_SETUP_HINT}. {NO_REQUEST}"
        try:
            return narrate.estimate(self._client(), text, self.cfg.model_id, self.cfg.max_chars), None
        except narrate.Refused as exc:
            raise ToolError(f"{exc}. {NOT_BILLED}") from None

    def synthesize(self, args: dict[str, Any]) -> dict[str, Any]:
        self._check_args("synthesize", args)
        client = self._client()
        out_dir = args.get("out_dir")
        if out_dir is None:
            if self.cfg.plugin_data is None:
                raise ToolError(f"out_dir is required because CLAUDE_PLUGIN_DATA is not set. {NO_REQUEST}")
            out_dir = os.path.join(self.cfg.plugin_data, "narration")
        elif not isinstance(out_dir, str):
            raise ToolError(f"out_dir must be an absolute path string. {NO_REQUEST}")
        text = self._text(args)
        name = args.get("name")
        if not isinstance(name, str):
            raise ToolError(f"name must be a string matching [A-Za-z0-9._-]+. {NO_REQUEST}")
        try:
            return narrate.synthesize(  # validates the remaining arguments before any request
                client,
                text=text,
                name=name,
                confirm_spend=args.get("confirm_spend"),
                max_chars=args.get("max_chars"),
                user_ceiling=self.cfg.max_chars,
                out_dir=out_dir,
                voice_id=self.cfg.voice_id or "",
                model_id=self.cfg.model_id,
                output_format=self.cfg.output_format,
                plugin_root=self.cfg.plugin_root,
            )
        except narrate.Refused as exc:
            raise ToolError(f"{exc}. {NOT_BILLED}") from None
        except narrate.PossiblyBilled as exc:
            raise ToolError(f"{exc}. {DO_NOT_RETRY}") from None


# ------------------------------------------------------------------ protocol


class Server:
    def __init__(self, cfg: Config, out: BinaryIO) -> None:
        self.cfg = cfg
        self.out = out
        self.tools = Tools(cfg)
        self.legacy_version: str | None = None

    # ---- output

    def _redact(self, line: str) -> str:
        key = self.cfg.key
        if key and len(key) >= MIN_REDACT_LEN:
            line = line.replace(key, REDACTED).replace(json.dumps(key)[1:-1], REDACTED)
        return line

    def _send(self, message: dict[str, Any]) -> None:
        line = self._redact(json.dumps(message, ensure_ascii=True, separators=(",", ":")))
        self.out.write(line.encode("ascii") + b"\n")
        self.out.flush()

    def _error(self, req_id: Any, code: int, message: str, data: Any = None) -> None:
        error: dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        self._send({"jsonrpc": "2.0", "id": req_id, "error": error})

    # ---- input

    def handle_line(self, raw: bytes) -> None:
        if not raw.strip():
            return
        try:
            msg = json.loads(raw)
        except ValueError:
            self._error(None, PARSE_ERROR, "Parse error")
            return
        if not isinstance(msg, dict):
            self._error(None, INVALID_REQUEST, "Invalid request: expected one JSON-RPC object per line")
            return
        method = msg.get("method")
        if "id" not in msg:
            return  # notification (notifications/initialized, notifications/cancelled, ...): never answered
        req_id = msg["id"]
        if method is None:
            return  # a response; this server sends no requests
        if not (isinstance(req_id, str) or _is_int(req_id)) or not isinstance(method, str):
            self._error(None, INVALID_REQUEST, "Invalid request: id must be a string or integer, method a string")
            return
        try:
            result = self._dispatch(method, msg.get("params", {}))
        except RpcError as exc:
            self._error(req_id, exc.code, exc.message, exc.data)
            return
        self._send({"jsonrpc": "2.0", "id": req_id, "result": result})

    def _dispatch(self, method: str, params: Any) -> dict[str, Any]:
        if not isinstance(params, dict):
            raise RpcError(INVALID_PARAMS, "params must be an object")
        if method == "initialize":
            return self._initialize(params)
        meta = params.get("_meta")
        version = meta.get(META_VERSION) if isinstance(meta, dict) else None
        if isinstance(meta, dict) and version is not None:
            if version not in MODERN_VERSIONS:
                raise RpcError(
                    UNSUPPORTED_PROTOCOL_VERSION,
                    "Unsupported protocol version",
                    {"supported": list(MODERN_VERSIONS), "requested": version},
                )
            if not isinstance(meta.get(META_CLIENT_CAPS), dict):
                raise RpcError(INVALID_PARAMS, f"Missing required _meta field {META_CLIENT_CAPS}")
            return self._modern(method, params)
        if self.legacy_version is not None:
            return self._legacy(method, params)
        raise RpcError(
            INVALID_PARAMS,
            f"Missing required _meta field {META_VERSION} (supported: {', '.join(MODERN_VERSIONS)}); "
            f"legacy clients must send initialize first (supported: {', '.join(LEGACY_VERSIONS)})",
        )

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        requested = params.get("protocolVersion")
        version = requested if requested in LEGACY_VERSIONS else LEGACY_VERSIONS[0]
        self.legacy_version = version
        return {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": dict(SERVER_INFO),
            "instructions": INSTRUCTIONS,
        }

    def _legacy(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": TOOLS}
        if method == "tools/call":
            return self._call_tool(params)
        raise RpcError(METHOD_NOT_FOUND, f"Method not found: {method}")

    def _modern(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any]
        if method == "server/discover":
            result = {
                "supportedVersions": list(MODERN_VERSIONS),
                "capabilities": {"tools": {"listChanged": False}},
                "instructions": INSTRUCTIONS,
                "ttlMs": CACHE_TTL_MS,
                "cacheScope": "public",
            }
        elif method == "tools/list":
            result = {"tools": TOOLS, "ttlMs": CACHE_TTL_MS, "cacheScope": "public"}
        elif method == "tools/call":
            result = self._call_tool(params)
        else:
            raise RpcError(METHOD_NOT_FOUND, f"Method not found: {method}")
        return {"resultType": "complete", **result, "_meta": {META_SERVER_INFO: dict(SERVER_INFO)}}

    def _call_tool(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        handlers: dict[str, Callable[[dict[str, Any]], Any]] = {
            "list_voices": self.tools.list_voices,
            "estimate": self.tools.estimate,
            "synthesize": self.tools.synthesize,
        }
        if not isinstance(name, str) or name not in handlers:
            raise RpcError(INVALID_PARAMS, f"Unknown tool: {name}")
        args = params.get("arguments", {})
        if not isinstance(args, dict):
            raise RpcError(INVALID_PARAMS, "arguments must be an object")
        note: str | None = None
        try:
            outcome = handlers[name](args)
        except ToolError as exc:
            return _error_result(str(exc))
        except Exception as exc:  # noqa: BLE001 - report, never crash the stdio loop
            billed = f" {MAYBE_BILLED}" if name == "synthesize" else " Nothing was billed."
            return _error_result(f"Internal error in {name}: {type(exc).__name__}: {exc}.{billed}")
        if isinstance(outcome, tuple):
            outcome, note = outcome
        content = [{"type": "text", "text": json.dumps(outcome, ensure_ascii=False)}]
        if note:
            content.append({"type": "text", "text": note})
        return {"content": content, "structuredContent": outcome, "isError": False}


def _error_result(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def main() -> int:
    out = sys.stdout.buffer
    sys.stdout = sys.stderr  # a stray print can never corrupt the protocol stream
    server = Server(load_config(os.environ), out)
    try:
        for raw in sys.stdin.buffer:
            server.handle_line(raw)
    except (BrokenPipeError, KeyboardInterrupt):
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
