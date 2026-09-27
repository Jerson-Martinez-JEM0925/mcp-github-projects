"""Documentation drift guard + invoke-every-tool contract test (issue #36).

* docs/TOOLS.md must equal what ``scripts/gen_tool_docs.py`` renders from the
  live registry, so a new, renamed or reclassified tool fails CI until the
  catalog is regenerated.
* Every registered tool must advertise a non-empty description (the
  capability suite used to ship 60 tools with none).
* Every registered tool is invoked end to end through a real FastMCP client
  with schema-derived arguments against injected fakes (no network): each
  call must come back as a structured ``{"ok": ...}`` envelope, never as an
  unhandled exception.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from clients.gh_cli_client import CommandResult  # noqa: E402
from core.config import get_settings  # noqa: E402
from core.factory import use_service_factory  # noqa: E402


class FakeGH:
    async def run(self, args: list[str]) -> CommandResult:
        return CommandResult(stdout="{}", stderr="", return_code=0)

    async def api_graphql(self, query: str, variables: dict) -> dict:
        return {"data": {}}


class FakeGraphQL:
    async def execute(self, query: str, variables: dict | None = None) -> dict:
        return {"data": {}}

    async def execute_with_retry(self, query, variables=None, *, is_mutation=False) -> dict:
        return {"data": {}}


@pytest.fixture(scope="module")
def server_module(tmp_path_factory: pytest.TempPathFactory):
    saved = dict(os.environ)
    os.environ.update(
        {
            "MCP_ACCESS_LEVEL": "full",
            "GH_PROJECT_ORG_NAME": "example-org",
            "GH_PROJECT_REPO_NAME": "example-repo",
            "GH_PROJECT_PROJECT_NUMBER": "1",
            "GH_PROJECT_OWNER_TYPE": "organization",
            "GH_PROJECT_CACHE_PATH": str(tmp_path_factory.mktemp("cache") / "metadata.json"),
        }
    )
    os.environ.pop("GH_PROJECT_SCOPE_LOCK", None)
    get_settings.cache_clear()
    import server

    module = importlib.reload(server)
    yield module
    os.environ.clear()
    os.environ.update(saved)
    get_settings.cache_clear()


# ── Drift guard ─────────────────────────────────────────────────────────────


def test_tools_md_matches_the_registry(server_module) -> None:
    import gen_tool_docs

    current, rendered = gen_tool_docs.updated_text()
    assert rendered == current, (
        "docs/TOOLS.md is stale — regenerate it with scripts/gen_tool_docs.py "
        "(see docs/TOOLS.md#regenerating)"
    )


def test_every_registered_tool_is_documented(server_module) -> None:
    text = (ROOT / "docs" / "TOOLS.md").read_text(encoding="utf-8")
    tools = asyncio.run(server_module.mcp.list_tools())
    missing = [t.name for t in tools if f"| `{t.name}` |" not in text]
    assert not missing, f"undocumented tools: {missing}"


def test_every_registered_tool_has_a_description(server_module) -> None:
    tools = asyncio.run(server_module.mcp.list_tools())
    empty = [t.name for t in tools if not (t.description or "").strip()]
    assert not empty, f"tools without a description: {empty}"


# ── Invoke every tool ───────────────────────────────────────────────────────


def _resolve(schema: dict, defs: dict) -> dict:
    ref = schema.get("$ref", "")
    if ref.startswith("#/$defs/"):
        return _resolve(defs[ref.removeprefix("#/$defs/")], defs)
    if "anyOf" in schema:
        branch = next((b for b in schema["anyOf"] if b.get("type") != "null"), schema["anyOf"][0])
        return _resolve(branch, defs)
    return schema


def _sample(schema: dict, defs: dict) -> Any:
    schema = _resolve(schema, defs)
    if "enum" in schema:
        return schema["enum"][0]
    if "const" in schema:
        return schema["const"]
    kind = schema.get("type")
    if kind == "integer":
        return max(int(schema.get("minimum", 1)), 1)
    if kind == "number":
        return float(max(schema.get("minimum", 1), 1))
    if kind == "boolean":
        return False
    if kind == "array":
        count = int(schema.get("minItems", 0))
        return [_sample(schema.get("items", {"type": "string"}), defs) for _ in range(count)]
    if kind == "object":
        props = schema.get("properties", {})
        return {name: _sample(props[name], defs) for name in schema.get("required", [])}
    length = max(int(schema.get("minLength", 1)), 1)
    if schema.get("format") == "date":
        return "2026-01-01"
    return "x" * length


def _arguments(tool) -> dict:
    schema = tool.parameters
    defs = schema.get("$defs", {})
    props = schema.get("properties", {})
    return {name: _sample(props[name], defs) for name in schema.get("required", [])}


def test_every_registered_tool_returns_an_envelope(server_module) -> None:
    async def run_all() -> list[str]:
        failures: list[str] = []
        tools = await server_module.mcp.list_tools()
        assert len(tools) >= 126
        with use_service_factory(graphql_client=FakeGraphQL(), gh_client=FakeGH()):
            async with Client(server_module.mcp) as client:
                for tool in tools:
                    args = _arguments(tool)
                    result = await client.call_tool(tool.name, args, raise_on_error=False)
                    payload = result.structured_content
                    if payload is None and result.content:
                        try:
                            payload = json.loads(result.content[0].text)
                        except (ValueError, AttributeError):
                            payload = None
                    if result.is_error or not isinstance(payload, dict) or "ok" not in payload:
                        text = result.content[0].text if result.content else ""
                        failures.append(f"{tool.name}({args}): {text[:200]}")
        return failures

    failures = asyncio.run(run_all())
    assert not failures, "tools that did not return an envelope:\n" + "\n".join(failures)
