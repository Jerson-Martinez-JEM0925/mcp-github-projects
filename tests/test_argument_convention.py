"""Uniform argument convention (#4): flat args everywhere, legacy wrapper ok.

Unit tests drive ``core.arguments`` with synthetic tools (one ``params:
Model`` style, one flat style); protocol tests go through a real FastMCP
client against the registered server so the advertised schema and the
accepted shapes are checked end to end.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from fastmcp import Client, FastMCP
from fastmcp.tools.function_tool import FunctionTool
from pydantic import BaseModel, Field, model_validator

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.arguments import ToolArgumentError, accept_flat_or_wrapped  # noqa: E402
from core.config import get_settings  # noqa: E402


class _CommentInput(BaseModel):
    issue_number: int = Field(..., ge=1, description="Issue number")
    body: str = Field(..., min_length=1)
    notify: bool = False

    @model_validator(mode="after")
    def _no_shouting(self) -> "_CommentInput":
        if self.body.isupper():
            raise ValueError("body must not be all caps")
        return self


async def comment(params: _CommentInput) -> dict:
    """Synthetic params-wrapped tool."""
    return {"issue": params.issue_number, "body": params.body, "notify": params.notify}


async def create(title: str, priority: str = "Normal") -> dict:
    """Synthetic flat tool."""
    return {"title": title, "priority": priority}


def _normalize(fn, name):
    original = FunctionTool.from_function(fn, name=name)
    return accept_flat_or_wrapped(fn, original.parameters, name)


def _server(*fns) -> FastMCP:
    mcp = FastMCP("t")
    for fn in fns:
        norm, schema = _normalize(fn, fn.__name__)
        tool = FunctionTool.from_function(norm, name=fn.__name__)
        mcp.add_tool(tool.model_copy(update={"parameters": schema}))
    return mcp


async def _call(mcp: FastMCP, name: str, args: dict):
    async with Client(mcp) as client:
        return await client.call_tool(name, args, raise_on_error=False)


# ── Advertised schema ────────────────────────────────────────────────────────


def test_wrapped_tool_advertises_flat_schema() -> None:
    _, schema = _normalize(comment, "comment")
    assert set(schema["properties"]) == {"issue_number", "body", "notify", "params"}
    assert schema["required"] == ["issue_number", "body"]
    assert schema["properties"]["issue_number"]["minimum"] == 1
    assert "params" not in schema.get("required", [])


def test_flat_tool_keeps_schema_and_gains_optional_wrapper() -> None:
    _, schema = _normalize(create, "create")
    assert schema["required"] == ["title"]
    assert "params" in schema["properties"]
    assert "params" not in schema["required"]


# ── Accepted shapes ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "args",
    [
        {"issue_number": 7, "body": "hi"},
        {"params": {"issue_number": 7, "body": "hi"}},
        {"params": {"issue_number": 7}, "body": "hi"},
    ],
    ids=["flat", "legacy-wrapper", "mixed"],
)
def test_wrapped_tool_accepts_every_shape(args: dict) -> None:
    result = asyncio.run(_call(_server(comment), "comment", args))
    assert not result.is_error, result
    assert result.data == {"issue": 7, "body": "hi", "notify": False}


@pytest.mark.parametrize(
    "args",
    [{"title": "x"}, {"params": {"title": "x"}}],
    ids=["flat", "legacy-wrapper"],
)
def test_flat_tool_accepts_both_shapes(args: dict) -> None:
    result = asyncio.run(_call(_server(create), "create", args))
    assert not result.is_error, result
    assert result.data == {"title": "x", "priority": "Normal"}


def test_flat_value_wins_over_wrapper() -> None:
    result = asyncio.run(
        _call(_server(create), "create", {"params": {"title": "old"}, "title": "new"})
    )
    assert result.data["title"] == "new"


# ── Validation stays strict, errors name the shape ───────────────────────────


def test_missing_field_error_names_expected_shape() -> None:
    norm, _ = _normalize(comment, "comment")
    with pytest.raises(ToolArgumentError) as info:
        asyncio.run(norm(body="hi"))
    message = str(info.value)
    assert "issue_number" in message
    assert "Pass arguments flat" in message
    assert "params." not in message, "location must not leak the internal wrapper"


def test_constraints_and_model_validators_still_apply() -> None:
    norm, _ = _normalize(comment, "comment")
    with pytest.raises(ToolArgumentError):
        asyncio.run(norm(issue_number=0, body="hi"))
    with pytest.raises(ToolArgumentError, match="all caps"):
        asyncio.run(norm(issue_number=1, body="LOUD"))


def test_non_object_wrapper_is_rejected() -> None:
    norm, _ = _normalize(create, "create")
    with pytest.raises(ToolArgumentError, match="must be an object"):
        asyncio.run(norm(params="title=x"))


# ── Every registered server tool follows the convention ──────────────────────


def _server_tools():
    os.environ["MCP_ACCESS_LEVEL"] = "full"
    get_settings.cache_clear()

    async def _noop(_: str) -> None:
        return None

    with patch("core.auth.validate_scopes", new=_noop):
        import importlib

        import server as server_module

        importlib.reload(server_module)
    return server_module


def test_every_registered_tool_accepts_flat_and_wrapper() -> None:
    server_module = _server_tools()
    tools = asyncio.run(server_module.mcp.list_tools())
    assert len(tools) > 100
    for tool in tools:
        props = tool.parameters.get("properties", {})
        assert "params" in props, f"{tool.name}: no legacy wrapper property"
        assert "params" not in tool.parameters.get("required", []), tool.name
        nested = props.get("params", {})
        assert "$ref" not in nested and "properties" not in nested, (
            f"{tool.name}: still advertises its fields nested under params"
        )


def test_server_info_answers_flat_and_wrapped() -> None:
    server_module = _server_tools()
    for args in ({}, {"params": {}}):
        result = asyncio.run(_call(server_module.mcp, "server_info", args))
        assert not result.is_error, (args, result)
        assert result.structured_content["data"]["access_level"] == "full"
