"""MCP tool exposing aggregate in-process operational metrics."""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from core.error_handling import handle_tool_error
from core.metrics import metrics_snapshot
from models.responses import ToolSuccess

logger = logging.getLogger(__name__)


class GetOperationalMetricsInput(BaseModel):
    """Input schema for get_operational_metrics."""

    tool: str | None = Field(
        default=None,
        max_length=120,
        description="Optional exact tool name to inspect; omit for aggregate metrics.",
    )
    limit: int = Field(
        default=50,
        ge=1,
        le=200,
        description="Maximum per-tool series returned, sorted by call count.",
    )


async def get_operational_metrics(params: GetOperationalMetricsInput) -> dict:
    """Return aggregate local MCP metrics: latency per tool and rate-limit counters.

    Metrics are process-local and metadata-only. They contain no arguments,
    repository content, bearer tokens, or error messages, and reset when the
    server restarts.
    """
    try:
        snapshot = metrics_snapshot(tool_name=params.tool, limit=params.limit)
        return ToolSuccess(data=snapshot).model_dump()
    except Exception as exc:
        logger.error("Error in get_operational_metrics: %s", exc)
        return handle_tool_error(exc, context="Get operational metrics failed")
