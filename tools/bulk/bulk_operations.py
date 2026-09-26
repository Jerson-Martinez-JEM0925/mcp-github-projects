"""MCP tools for bulk issue operations.

Exposes tools for closing multiple issues at once and searching issues.
"""
from __future__ import annotations

import json
import logging
from typing import Literal, Optional

from pydantic import BaseModel, Field

from clients.gh_cli_client import CLIError
from core.config import get_settings
from core.error_handling import build_error_response, handle_tool_error
from models.responses import ToolSuccess
from core.factory import get_service_factory

logger = logging.getLogger(__name__)


class BulkCloseIssuesInput(BaseModel):
    """Input schema for the bulk_close_issues tool."""

    issue_numbers: list[int] = Field(
        max_length=50,
        description="List of issue numbers to close (maximum 50)",
    )
    reason: str = Field(default="completed", description="Close reason: 'completed' or 'not_planned'")
    comment: Optional[str] = Field(default=None, description="Optional comment to add before closing each issue")


class SearchIssuesInput(BaseModel):
    """Input schema for the search_issues tool."""

    query: str = Field(default="", description="Search text (title/body)")
    state: Literal["open", "closed", "all"] = Field(
        default="open",
        description="Filter: 'open', 'closed', or 'all'",
    )
    labels: Optional[list[str]] = Field(default=None, description="Filter by labels (any match)")
    milestone: Optional[str] = Field(default=None, description="Filter by milestone title")
    assignee: Optional[str] = Field(default=None, description="Filter by assignee username")
    limit: int = Field(
        default=30,
        ge=1,
        le=100,
        description="Max results to return (1-100)",
    )


async def bulk_close_issues(params: BulkCloseIssuesInput) -> dict:
    """Close multiple issues at once with optional comment.

    Args:
        params: Input containing list of issue numbers, reason, and optional comment.

    Returns:
        ToolSuccess with results per issue.
    """
    try:
        await get_service_factory().ensure_auth()
        settings = get_settings()
        repo = f"{settings.org_name}/{settings.repo_name}"
        gh_client = get_service_factory().gh()

        results: list[dict] = []

        for issue_num in params.issue_numbers:
            try:
                if params.comment:
                    await gh_client.run([
                        "issue", "comment", str(issue_num),
                        "--repo", repo, "--body", params.comment,
                    ])

                close_args = ["issue", "close", str(issue_num), "--repo", repo,
                              "--reason", params.reason]
                await gh_client.run(close_args)

                results.append({"issue": issue_num, "status": "closed"})
            except CLIError as exc:
                if "already closed" in exc.stderr.lower():
                    results.append({"issue": issue_num, "status": "already_closed"})
                else:
                    results.append({"issue": issue_num, "status": "failed", "error": exc.stderr.strip()})

        closed_count = sum(1 for r in results if r["status"] == "closed")
        return ToolSuccess(
            data={
                "results": results,
                "total": len(params.issue_numbers),
                "closed": closed_count,
                "message": f"Closed {closed_count}/{len(params.issue_numbers)} issues.",
            },
        ).model_dump()

    except Exception as exc:
        logger.error("Error in bulk_close_issues: %s", exc)
        return handle_tool_error(exc, context="Bulk close issues failed")


async def search_issues(params: SearchIssuesInput) -> dict:
    """Search issues in the repository with filters.

    Args:
        params: Input with search query and filters.

    Returns:
        ToolSuccess with matching issues.
    """
    try:
        await get_service_factory().ensure_auth()
        settings = get_settings()
        repo = f"{settings.org_name}/{settings.repo_name}"
        gh_client = get_service_factory().gh()

        args = ["issue", "list", "--repo", repo, "--state", params.state,
                "--limit", str(params.limit),
                "--json", "number,title,state,labels,milestone,assignees,createdAt"]

        if params.labels:
            for label in params.labels:
                args.extend(["--label", label])

        if params.milestone:
            args.extend(["--milestone", params.milestone])

        if params.assignee:
            args.extend(["--assignee", params.assignee])

        if params.query:
            args.extend(["--search", params.query])

        result = await gh_client.run(args)
        issues = json.loads(result.stdout)

        items = []
        for issue in issues:
            items.append({
                "number": issue.get("number"),
                "title": issue.get("title"),
                "state": issue.get("state"),
                "labels": [l.get("name") for l in issue.get("labels", [])],
                "milestone": issue.get("milestone", {}).get("title") if issue.get("milestone") else None,
                "assignees": [a.get("login") for a in issue.get("assignees", [])],
                "created_at": issue.get("createdAt"),
            })

        return ToolSuccess(
            data={"issues": items, "count": len(items)},
        ).model_dump()

    except CLIError as exc:
        logger.error("CLI error in search_issues: %s", exc)
        return build_error_response(
            error_type="internal",
            message=f"Failed to search issues: {exc.stderr.strip()}",
            suggestion="Check query syntax and token permissions.",
        )
    except Exception as exc:
        logger.error("Error in search_issues: %s", exc)
        return handle_tool_error(exc, context="Search issues failed")
