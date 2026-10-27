"""Manifest wiring for the add-on's bundled MCP server (blueprint W7, bead kss-aad).

`.mcp.json` must declare exactly one stdio server, `elevenlabs` (interface contract: tools appear as
`mcp__plugin_voiceover-shorts-tts_elevenlabs__<tool>`), that runs `server/tts_mcp.py` from
`${CLAUDE_PLUGIN_ROOT}` and passes the userConfig options through `env` as `${user_config.KEY}`
(code.claude.com/docs/en/plugins-reference, "Reference a saved value" and "Where each variable
resolves": MCP stdio servers substitute in `command`, `args`, `env`). The hosted ElevenLabs MCP is
not declared (blueprint, key design point 3).

`wiring_problems()` holds every check and runs on the real file and on planted temp copies, so a
broken copy is shown to be caught. The end-to-end test launches the server exactly as `.mcp.json`
describes, with no key, and speaks legacy `initialize` + `tools/list` over stdio. Nothing here
contacts ElevenLabs: with no key the server makes no requests, and only those two methods are sent.
"""

from __future__ import annotations

import ast
import copy
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
MCP_JSON = ROOT / ".mcp.json"
PLUGIN_JSON = ROOT / ".claude-plugin" / "plugin.json"
SERVER_SOURCE = ROOT / "server" / "tts_mcp.py"

SERVER_NAME = "elevenlabs"
SERVER_REL = "server/tts_mcp.py"
ROOT_VAR = "${CLAUDE_PLUGIN_ROOT}"
USER_CONFIG_REF = re.compile(r"\$\{user_config\.([^}]*)\}")
KEY_ENV = "ELEVENLABS_API_KEY"
LOOPBACK_ENV = "VOS_TTS_TEST_ALLOW_LOOPBACK"
TOOL_NAMES = ["list_voices", "estimate", "synthesize"]
LEGACY = "2025-11-25"
TIMEOUT_S = 30


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def server_env_names(source: Path = SERVER_SOURCE) -> set[str]:
    """The env names the server reads: the string values of its module-level `ENV_*` constants."""
    names: set[str] = set()
    for node in ast.parse(source.read_text(encoding="utf-8")).body:
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Constant):
            continue
        if not isinstance(node.value.value, str):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id.startswith("ENV_"):
                names.add(node.value.value)
    return names


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [s for item in value for s in _strings(item)]
    if isinstance(value, dict):
        return [s for item in value.values() for s in _strings(item)]
    return []


def wiring_problems(mcp_path: Path, manifest: dict[str, Any], server_envs: set[str], plugin_root: Path) -> list[str]:
    """Every way `mcp_path` fails the W7 wiring contract; an empty list means it passes."""
    try:
        doc = load_json(mcp_path)
    except (OSError, ValueError) as exc:
        return [f"{mcp_path.name} does not parse: {exc}"]
    servers = doc.get("mcpServers") if isinstance(doc, dict) else None
    if not isinstance(servers, dict):
        return ["top level must be an object with an mcpServers object"]
    problems: list[str] = []
    if sorted(servers) != [SERVER_NAME]:
        problems.append(f"servers must be exactly [{SERVER_NAME!r}] (bundled server only), got {sorted(servers)}")
    entry = servers.get(SERVER_NAME)
    if not isinstance(entry, dict):
        return [*problems, f"server {SERVER_NAME!r} is missing or not an object"]

    if "url" in entry or entry.get("type", "stdio") != "stdio":
        problems.append("server must be a local stdio server, not a remote url")
    if entry.get("command") != "python3":
        problems.append(f"command must be 'python3', got {entry.get('command')!r}")
    args = entry.get("args")
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        problems.append("args must be a list of strings")
        args = []
    scripts = [a for a in args if a.startswith(ROOT_VAR)]
    if len(scripts) != 1:
        problems.append(f"args must name exactly one {ROOT_VAR} path, got {scripts}")
    else:
        script = scripts[0]
        resolved = Path(script.replace(ROOT_VAR, str(plugin_root), 1)).resolve()
        if resolved != (plugin_root / SERVER_REL).resolve():
            problems.append(f"script must be {ROOT_VAR}/{SERVER_REL}, got {script}")
        elif not resolved.is_file():
            problems.append(f"script {resolved} does not exist under the plugin root")
        if "-I" not in args[: args.index(script)]:
            problems.append("python3 must run the server isolated (-I before the script path)")

    options = manifest.get("userConfig", {})
    referenced: set[str] = set()
    for text in _strings(entry):
        for key in USER_CONFIG_REF.findall(text):
            referenced.add(key)
            if key not in options:
                problems.append(f"${{user_config.{key}}} is not a userConfig option in plugin.json")

    env = entry.get("env")
    if not isinstance(env, dict) or not all(isinstance(v, str) for v in env.values()):
        return [*problems, "env must be an object of strings"]
    for name in sorted(env):
        if name == LOOPBACK_ENV:
            problems.append(f"{LOOPBACK_ENV} is a test-only flag and must not be set")
        elif name not in server_envs:
            problems.append(f"env {name} is not read by {SERVER_REL}")
    for name in sorted(server_envs - {LOOPBACK_ENV} - set(env)):
        problems.append(f"server setting {name} is not wired in env")
    for key in sorted(set(options) - referenced):
        problems.append(f"userConfig option {key} is never passed to the server")

    key_ref = USER_CONFIG_REF.fullmatch(env.get(KEY_ENV, ""))
    if key_ref is None:
        problems.append(f"{KEY_ENV} must be exactly one ${{user_config.KEY}} reference")
    elif options.get(key_ref.group(1), {}).get("sensitive") is not True:
        problems.append(f"{KEY_ENV} must come from a sensitive: true option, not {key_ref.group(1)}")
    return problems


# ------------------------------------------------------------------ the real files


def test_server_source_names_the_settings_checked_here() -> None:
    envs = server_env_names()
    assert KEY_ENV in envs and LOOPBACK_ENV in envs, envs  # otherwise the checks below are vacuous


def test_mcp_json_parses_with_one_elevenlabs_server() -> None:
    doc = load_json(MCP_JSON)
    assert list(doc["mcpServers"]) == [SERVER_NAME]


def test_api_key_option_is_sensitive() -> None:
    assert load_json(PLUGIN_JSON)["userConfig"]["elevenlabs_api_key"]["sensitive"] is True


def test_real_mcp_json_is_wired() -> None:
    assert wiring_problems(MCP_JSON, load_json(PLUGIN_JSON), server_env_names(), ROOT) == []


# ------------------------------------------------------------------ planted negatives


def _plant(tmp_path: Path, doc: dict[str, Any]) -> Path:
    path = tmp_path / ".mcp.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def _entry(doc: dict[str, Any]) -> dict[str, Any]:
    entry: dict[str, Any] = doc["mcpServers"][SERVER_NAME]
    return entry


def _unknown_key(doc: dict[str, Any]) -> None:
    _entry(doc)["env"]["VOS_TTS_VOICE_ID"] = "${user_config.voice_idd}"


def _loopback(doc: dict[str, Any]) -> None:
    _entry(doc)["env"][LOOPBACK_ENV] = "1"


def _unknown_env(doc: dict[str, Any]) -> None:
    _entry(doc)["env"]["VOS_TTS_VOICE"] = "${user_config.voice_id}"


def _hosted_server(doc: dict[str, Any]) -> None:
    doc["mcpServers"]["elevenlabs-hosted"] = {"type": "http", "url": "https://example.invalid/mcp"}


def _missing_script(doc: dict[str, Any]) -> None:
    entry = _entry(doc)
    entry["args"] = [a.replace(SERVER_REL, "server/missing.py") for a in entry["args"]]


def _not_isolated(doc: dict[str, Any]) -> None:
    entry = _entry(doc)
    entry["args"] = [a for a in entry["args"] if a != "-I"]


def _key_from_plain_option(doc: dict[str, Any]) -> None:
    _entry(doc)["env"][KEY_ENV] = "${user_config.voice_id}"


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (_unknown_key, "${user_config.voice_idd} is not a userConfig option"),
        (_loopback, f"{LOOPBACK_ENV} is a test-only flag"),
        (_unknown_env, "env VOS_TTS_VOICE is not read"),
        (_hosted_server, "servers must be exactly ['elevenlabs']"),
        (_missing_script, f"script must be {ROOT_VAR}/{SERVER_REL}"),
        (_not_isolated, "-I before the script path"),
        (_key_from_plain_option, "must come from a sensitive: true option"),
    ],
    ids=["unknown-user-config-key", "loopback-flag", "unknown-env", "hosted-server", "missing-script",
         "not-isolated", "key-not-sensitive"],
)
def test_planted_copy_is_flagged(tmp_path: Path, mutate: Any, expected: str) -> None:
    doc = copy.deepcopy(load_json(MCP_JSON))
    mutate(doc)
    problems = wiring_problems(_plant(tmp_path, doc), load_json(PLUGIN_JSON), server_env_names(), ROOT)
    assert any(expected in p for p in problems), problems


def test_planted_non_sensitive_key_option_is_flagged(tmp_path: Path) -> None:
    manifest = copy.deepcopy(load_json(PLUGIN_JSON))
    manifest["userConfig"]["elevenlabs_api_key"]["sensitive"] = False
    problems = wiring_problems(MCP_JSON, manifest, server_env_names(), ROOT)
    assert any("must come from a sensitive: true option" in p for p in problems), problems


def test_planted_root_without_the_server_is_flagged(tmp_path: Path) -> None:
    problems = wiring_problems(MCP_JSON, load_json(PLUGIN_JSON), server_env_names(), tmp_path)
    assert any("does not exist under the plugin root" in p for p in problems), problems


def test_planted_unparseable_copy_is_flagged(tmp_path: Path) -> None:
    path = tmp_path / ".mcp.json"
    path.write_text('{"mcpServers": {', encoding="utf-8")
    problems = wiring_problems(path, load_json(PLUGIN_JSON), server_env_names(), ROOT)
    assert len(problems) == 1 and "does not parse" in problems[0], problems


# ------------------------------------------------------------------ end to end


def _launch_env(entry: dict[str, Any], options: dict[str, Any], data_dir: Path) -> dict[str, str]:
    """The env Claude Code would give the server with no key saved.

    Built from scratch (never os.environ, so no real key can leak in). An option with a default is
    substituted with it; one without (the key, the voice) stays the literal placeholder, which the
    server treats as unset. CLAUDE_PLUGIN_ROOT/DATA are exported to MCP stdio servers per the docs.
    """

    def substitute(value: str) -> str:
        def one(match: re.Match[str]) -> str:
            option = options[match.group(1)]
            return str(option["default"]) if "default" in option else match.group(0)

        return USER_CONFIG_REF.sub(one, value.replace(ROOT_VAR, str(ROOT)))

    env = {name: substitute(value) for name, value in entry["env"].items()}
    assert env[KEY_ENV].startswith("${user_config."), "the key must reach the server unset in this test"
    env.update(PATH=os.environ.get("PATH", os.defpath), CLAUDE_PLUGIN_ROOT=str(ROOT),
               CLAUDE_PLUGIN_DATA=str(data_dir))
    return env


def test_server_launches_as_declared_and_lists_three_tools(tmp_path: Path) -> None:
    entry = _entry(load_json(MCP_JSON))
    exe = shutil.which(entry["command"])
    assert exe is not None, f"{entry['command']} is not on PATH"
    argv = [exe, *(a.replace(ROOT_VAR, str(ROOT)) for a in entry["args"])]
    env = _launch_env(entry, load_json(PLUGIN_JSON)["userConfig"], tmp_path / "data")
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": LEGACY, "capabilities": {}, "clientInfo": {"name": "pytest", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    stdin = "".join(json.dumps(m) + "\n" for m in messages)
    proc = subprocess.run(argv, input=stdin, capture_output=True, text=True, env=env, cwd=tmp_path,
                          timeout=TIMEOUT_S)
    assert proc.returncode == 0, proc.stderr
    replies = [json.loads(line) for line in proc.stdout.splitlines()]
    assert [r.get("id") for r in replies] == [1, 2], proc.stdout
    init, listed = replies[0]["result"], replies[1]["result"]
    assert init["protocolVersion"] == LEGACY
    assert "tools" in init["capabilities"]
    assert [t["name"] for t in listed["tools"]] == TOOL_NAMES
