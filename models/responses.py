"""Pydantic models for MCP tool response envelopes.

All tool responses conform to either ToolSuccess or ToolError structure,
providing a consistent interface for the MCP client.
"""

from pydantic import BaseModel, Field


class ToolSuccess(BaseModel):
    """Successful tool response envelope.

    Contains the operation result data in a consistent format.
    """

    ok: bool = Field(default=True, description="Always True for success responses")
    data: dict | list = Field(description="Operation result data")


class ToolError(BaseModel):
    """Error tool response envelope.

    Contains structured error information with actionable suggestions.
    """

    ok: bool = Field(default=False, description="Always False for error responses")
    error_type: str = Field(
        description=(
            "Error classification: authentication, validation, "
            "not_found, rate_limit, or internal"
        )
    )
    message: str = Field(description="Human-readable error description")
    suggestion: str = Field(description="Actionable resolution suggestion")
    request_id: str | None = Field(
        default=None,
        description="GitHub API request ID when available for support escalation",
    )
    correlation_id: str | None = Field(
        default=None,
        description=(
            "Local ID of this tool call; the same ID tags every stderr log line "
            "of the call, so it can be quoted when reporting a failure"
        ),
    )
