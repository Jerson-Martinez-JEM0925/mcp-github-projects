"""MCP tool for commenting on GitHub issues.

Exposes the `comment_issue` tool function that adds a comment to
a GitHub issue via the GH CLI.
"""
from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from clients.gh_cli_client import CLIError
from core.config import get_settings
from core.error_handling import build_error_response, handle_tool_error
from models.responses import ToolSuccess
from core.factory import get_service_factory

logger = logging.getLogger(__name__)


class CommentIssueInput(BaseModel):
    """Input schema for the comment_issue tool."""

    issue_number: int = Field(description="GitHub issue number to comment on")
    body: str = Field(description="Comment body text (supports Markdown)", min_length=1, max_length=65536)


async def comment_issue(params: CommentIssueInput) -> dict:
    """Add a comment to a GitHub issue.

    Args:
        params: Input containing the issue number and comment body.

    Returns:
        ToolSuccess with comment URL on success, or ToolError on failure.
    """
    try:
        await get_service_factory().ensure_auth()
        settings = get_settings()
        repo = f"{settings.org_name}/{settings.repo_name}"
        gh_client = get_service_factory().gh()

        result = await gh_client.run([
            "issue", "comment", str(params.issue_number),
            "--repo", repo,
            "--body", params.body,
        ])

        comment_url = result.stdout.strip() if result.stdout else ""

        return ToolSuccess(
            data={
                "issue_number": params.issue_number,
                "comment_url": comment_url,
                "message": "Comment added successfully.",
            },
        ).model_dump()

    except CLIError as exc:
        logger.error("CLI error in comment_issue: %s", exc)
        return build_error_response(
            error_type="internal",
            message=f"Failed to comment on issue: {exc.stderr.strip()}",
            suggestion="Verify the issue number exists and the token has write permissions.",
        )
    except Exception as exc:
        logger.error("Error in comment_issue: %s", exc)
        return handle_tool_error(exc, context="Comment issue failed")
