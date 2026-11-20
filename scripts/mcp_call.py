#!/usr/bin/env python3
"""Minimal stdio CLI client for the GitHub Projects MCP server.

Launches the MCP server (`python server.py`) as a stdio subprocess,
performs the FastMCP handshake, calls exactly one tool with JSON
arguments, prints the JSON response, and exits.

This is a thin operator/debug convenience so you do not have to
hand-write a JSON-RPC client every time you want to smoke-test a tool.

Usage:
    python scripts/mcp_call.py <TOOL> [JSON_ARGS] [--wrap] [--raw]

    TOOL        Name of the tool to call (e.g. list_labels).
    JSON_ARGS   Tool arguments as a JSON object. Default: {}.
    --wrap      Nest the arguments under a legacy "params" key. Every tool
                accepts flat arguments (the default) and still unwraps the
                legacy form, so this only exists to exercise that path.
                `--flat` is accepted as a no-op for older scripts.
    --raw       Print the full JSON-RPC envelope instead of just the
                tool result payload.

Authentication:
    The GitHub token is resolved from the environment in this order:
        GITHUB_TOKEN, then GH_TOKEN.
    The token is passed through to the server process; it is never
    printed or logged by this client.

Project context:
    The server also expects GH_PROJECT_ORG_NAME / GH_PROJECT_REPO_NAME /
    GH_PROJECT_PROJECT_NUMBER (and optionally GH_PROJECT_OWNER_TYPE) in the
    environment. When run via `make call`, these come from your .env file.

Examples:
    python scripts/mcp_call.py list_labels
    python scripts/mcp_call.py get_issue_detail '{"issue_number": 1}'
    python scripts/mcp_call.py create_project_item '{"title": "Hi"}'
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

# Server entrypoint lives at the repository root, one level up from scripts/.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SERVER = _REPO_ROOT / "server.py"

_PROTOCOL_VERSION = "2024-11-05"


def _resolve_token() -> str:
    """Return the GitHub token from GITHUB_TOKEN then GH_TOKEN, or exit."""
    token = (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or "").strip()
    if not token:
        sys.stderr.write(
            "error: no token found. Set GITHUB_TOKEN or GH_TOKEN in the "
            "environment (or your .env when using `make call`).\n"
        )
        raise SystemExit(2)
    return token


def _readline_json(proc: subprocess.Popen[str]) -> dict | None:
    """Read stdout lines until one parses as JSON; skip log noise."""
    assert proc.stdout is not None
    while True:
        line = proc.stdout.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            continue
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            # Non-JSON line (e.g. a startup log). Ignore and keep reading.
            continue


def call(tool: str, arguments: dict, *, flat: bool) -> dict | None:
    """Spawn the server, handshake, call one tool, return the response."""
    token = _resolve_token()
    env = dict(os.environ)
    env["GITHUB_TOKEN"] = token
    env["GH_TOKEN"] = token

    proc = subprocess.Popen(
        [sys.executable, str(_SERVER)],
        cwd=str(_REPO_ROOT),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )

    def send(obj: dict) -> None:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()

    try:
        send({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "mcp_call", "version": "1"},
            },
        })
        _readline_json(proc)  # initialize result
        send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

        payload = arguments if flat else {"params": arguments}
        send({
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": tool, "arguments": payload},
        })
        response = _readline_json(proc)
    finally:
        if proc.stdin is not None:
            proc.stdin.close()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()

    return response


def _parse_args(argv: list[str]) -> tuple[str, dict, bool, bool]:
    flat = "--wrap" not in argv  # flat is the convention (#4); --flat is a no-op
    raw = "--raw" in argv
    positional = [a for a in argv if not a.startswith("--")]
    if not positional:
        sys.stderr.write(__doc__ or "usage: mcp_call.py <TOOL> [JSON_ARGS]\n")
        raise SystemExit(2)
    tool = positional[0]
    try:
        arguments = json.loads(positional[1]) if len(positional) > 1 else {}
    except json.JSONDecodeError as exc:
        sys.stderr.write(f"error: JSON_ARGS is not valid JSON: {exc}\n")
        raise SystemExit(2) from exc
    if not isinstance(arguments, dict):
        sys.stderr.write("error: JSON_ARGS must be a JSON object.\n")
        raise SystemExit(2)
    return tool, arguments, flat, raw


def main(argv: list[str]) -> int:
    tool, arguments, flat, raw = _parse_args(argv)
    response = call(tool, arguments, flat=flat)
    if response is None:
        sys.stderr.write("error: no response from server (check token/context env).\n")
        return 1

    if raw:
        print(json.dumps(response, indent=2, ensure_ascii=False))
        return 0

    result = response.get("result")
    if result is None:
        # JSON-RPC error envelope.
        print(json.dumps(response, indent=2, ensure_ascii=False))
        return 1

    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 1 if result.get("isError") else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
