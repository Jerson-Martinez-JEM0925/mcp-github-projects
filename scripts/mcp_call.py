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
import queue
import subprocess
import sys
import threading
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


class _JsonLineReader:
    """Read JSON lines from a pipe with a timeout, skipping log noise."""

    def __init__(self, stream) -> None:
        self._queue: queue.Queue[dict | None] = queue.Queue()

        def consume() -> None:
            try:
                for line in stream:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        parsed = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(parsed, dict):
                        self._queue.put(parsed)
            finally:
                self._queue.put(None)

        self._thread = threading.Thread(target=consume, daemon=True)
        self._thread.start()

    def read(self, timeout: float = 30.0) -> dict | None:
        """Return the next JSON object, or None on EOF/timeout."""
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None


def _readline_json(reader: _JsonLineReader, timeout: float = 30.0) -> dict | None:
    """Compatibility wrapper around the bounded JSON-line reader."""
    return reader.read(timeout=timeout)


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

    assert proc.stdout is not None
    reader = _JsonLineReader(proc.stdout)
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
        initialize_response = _readline_json(reader)
        if initialize_response is None:
            return None
        send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

        payload = arguments if flat else {"params": arguments}
        send({
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": tool, "arguments": payload},
        })
        response = _readline_json(reader, timeout=120.0)
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


def _tool_result_failed(result: dict) -> bool:
    """Return true for protocol errors or application-level ok=false envelopes."""
    if result.get("isError"):
        return True
    structured = result.get("structuredContent")
    if isinstance(structured, dict) and structured.get("ok") is False:
        return True
    content = result.get("content")
    if isinstance(content, list):
        for part in content:
            if not isinstance(part, dict) or not isinstance(part.get("text"), str):
                continue
            try:
                payload = json.loads(part["text"])
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict) and payload.get("ok") is False:
                return True
    return False


def main(argv: list[str]) -> int:
    tool, arguments, flat, raw = _parse_args(argv)
    response = call(tool, arguments, flat=flat)
    if response is None:
        sys.stderr.write("error: no response from server (check token/context env).\n")
        return 1

    result = response.get("result")
    if raw:
        print(json.dumps(response, indent=2, ensure_ascii=False))
        return 1 if result is None or _tool_result_failed(result) else 0

    if result is None:
        # JSON-RPC error envelope.
        print(json.dumps(response, indent=2, ensure_ascii=False))
        return 1

    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 1 if _tool_result_failed(result) else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
