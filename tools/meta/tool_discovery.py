"""MCP tool for compact discovery of the registered tool catalog."""

from __future__ import annotations

import logging
import sys
from typing import Any, Literal

from pydantic import BaseModel, Field

from core.access import ToolAccess, classify
from core.capabilities import TOOL_CAPABILITIES
from core.error_handling import build_error_response, handle_tool_error
from models.responses import ToolSuccess

logger = logging.getLogger(__name__)


class SearchToolsInput(BaseModel):
    """Input schema for search_tools."""

    query: str = Field(
        default="",
        max_length=120,
        description="Case-insensitive text matched against tool names, descriptions and capabilities.",
    )
    access_tier: Literal["read", "write", "delete"] | None = Field(
        default=None,
        description="Optional exact access tier filter.",
    )
    limit: int = Field(
        default=20,
        ge=1,
        le=100,
        description="Maximum matching tools returned.",
    )


def _compact_description(value: str) -> str:
    """Return one bounded single-line description."""
    first = " ".join((value or "").strip().split())
    return first[:180]


async def _registered_tools() -> list[Any] | None:
    """Read the live FastMCP registry when the server module is available."""
    server_module = sys.modules.get("server") or sys.modules.get("__main__")
    if server_module is None or not hasattr(server_module, "mcp"):
        return None
    return await server_module.mcp.list_tools()


async def search_tools(params: SearchToolsInput) -> dict:
    """Search registered MCP tools and return a compact catalog without input schemas.

    Use this before choosing an operation in large profiles. Results include
    only metadata: name, access tier, required capabilities and a short
    description; arguments and repository data are never included.
    """
    try:
        registered = await _registered_tools()
        if registered is None:
            return build_error_response(
                error_type="internal",
                message="Live MCP registry is unavailable in this process.",
                suggestion="Call search_tools through the running MCP server.",
            )

        query = params.query.strip().casefold()
        matches: list[dict[str, Any]] = []
        total_registered = len(registered)
        for tool in registered:
            name = str(getattr(tool, "name", ""))
            if not name:
                continue
            try:
                tier = classify(name)
            except KeyError:
                continue
            if params.access_tier and tier is not ToolAccess(params.access_tier):
                continue
            capabilities = sorted(cap.value for cap in TOOL_CAPABILITIES.get(name, frozenset()))
            description = _compact_description(getattr(tool, "description", "") or "")
            haystack = " ".join([name, description, " ".join(capabilities)]).casefold()
            if query and query not in haystack:
                continue
            matches.append(
                {
                    "name": name,
                    "access_tier": tier.value,
                    "capabilities": capabilities,
                    "description": description,
                }
            )

        matches.sort(key=lambda item: (item["access_tier"] != "read", item["name"]))
        rows = matches[: params.limit]
        return ToolSuccess(
            data={
                "query": params.query,
                "access_tier": params.access_tier,
                "count": len(rows),
                "matched": len(matches),
                "total_registered": total_registered,
                "truncated": len(matches) > len(rows),
                "tools": rows,
            }
        ).model_dump()
    except Exception as exc:
        logger.error("Error in search_tools: %s", exc)
        return handle_tool_error(exc, context="Search tools failed")
