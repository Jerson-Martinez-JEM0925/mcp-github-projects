"""MCP tool for closing GitHub issues associated with project items.

Exposes the `close_issue` tool function that closes the underlying issue
for a project item via the GH CLI. Handles idempotent state transitions
(already closed → success) and DraftIssue rejection.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from clients.gh_cli_client import CLIError
from core.error_handling import build_error_response, handle_tool_error
from models.responses import ToolSuccess
from services.issue_service import (
    DraftIssueError,
)
from core.factory import get_service_factory

logger = logging.getLogger(__name__)


class CloseIssueInput(BaseModel):
    """Input schema for the close_issue tool.

    Attributes:
        issue_number: The GitHub issue number to close.
    """

    issue_number: int = Field(description="GitHub issue number to close")


async def close_issue(params: CloseIssueInput) -> dict:
    """Close a GitHub issue associated with a project item.

    Closes the specified issue via the GH CLI. If the issue is already
    closed, returns success with a no-change message (idempotent behavior).
    If the item is a DraftIssue with no underlying issue, returns an error.

    Args:
        params: Input containing the issue number to close.

    Returns:
        A dict conforming to ToolSuccess (ok=True, data with issue_number
        and state) on success, or ToolError (ok=False, error_type,
        message, suggestion) on failure.
    """
    try:
        await get_service_factory().ensure_auth()
        gh_client = get_service_factory().gh()
        issue_service = get_service_factory().issue_service(gh_client)

        try:
            await issue_service.close(issue_number=params.issue_number)
        except DraftIssueError as exc:
            logger.warning("DraftIssue close rejection: %s", exc)
            return build_error_response(
                error_type="validation",
                message=str(exc),
                suggestion=(
                    "Draft issues cannot be closed because they have no "
                    "underlying GitHub issue. Convert the draft to a full "
                    "issue first, then close it."
                ),
            )
        except CLIError as exc:
            stderr_lower = exc.stderr.lower()
            # Handle already-closed (idempotent).
            if "already closed" in stderr_lower:
                return ToolSuccess(
                    data={
                        "issue_number": params.issue_number,
                        "state": "closed",
                        "message": (
                            "Issue is already closed. No change needed."
                        ),
                    },
                ).model_dump()
            # Handle not-found.
            if (
                "not found" in stderr_lower
                or "could not resolve" in stderr_lower
                or "no issue" in stderr_lower
            ):
                return build_error_response(
                    error_type="not_found",
                    message=(
                        f"Issue #{params.issue_number} was not found in "
                        "the repository."
                    ),
                    suggestion=(
                        "Verify the issue number is correct. "
                        "Run list_project_items to find valid issue numbers."
                    ),
                )
            # Re-raise for generic CLI errors.
            raise

        return ToolSuccess(
            data={
                "issue_number": params.issue_number,
                "state": "closed",
                "message": "Issue closed successfully.",
            },
        ).model_dump()

    except CLIError as exc:
        logger.error("CLI error in close_issue: %s", exc)
        return build_error_response(
            error_type="internal",
            message=f"Failed to close issue: {exc.stderr.strip()}",
            suggestion=(
                "Check that the issue number is valid and the token has "
                "permission to close issues. Verify network connectivity."
            ),
        )

    except Exception as exc:
        logger.error("Error in close_issue: %s", exc)
        return handle_tool_error(exc, context="Close issue failed")
