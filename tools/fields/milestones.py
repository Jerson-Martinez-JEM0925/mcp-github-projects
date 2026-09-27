"""MCP tools for GitHub milestone management.

Exposes tools for creating, closing, and listing milestones
in the repository via the GH CLI / REST API.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from pydantic import BaseModel, Field

from clients.gh_cli_client import CLIError
from core.config import get_settings
from core.error_handling import build_error_response, handle_tool_error
from models.responses import ToolSuccess
from core.factory import get_service_factory

logger = logging.getLogger(__name__)


class CreateMilestoneInput(BaseModel):
    """Input schema for the create_milestone tool."""

    title: str = Field(description="Milestone title (e.g., 'Sprint 1 - Jul 7-13')")
    description: Optional[str] = Field(default=None, description="Milestone description")
    due_date: Optional[str] = Field(default=None, description="Due date in ISO 8601 format (YYYY-MM-DD)")


class CloseMilestoneInput(BaseModel):
    """Input schema for the close_milestone tool."""

    title: str = Field(description="Milestone title to close (exact match)")


class ListMilestonesInput(BaseModel):
    """Input schema for the list_milestones tool."""

    state: str = Field(default="open", description="Filter by state: 'open', 'closed', or 'all'")


async def create_milestone(params: CreateMilestoneInput) -> dict:
    """Create a new milestone in the repository.

    Args:
        params: Input containing title, optional description and due date.

    Returns:
        ToolSuccess with milestone number and URL on success.
    """
    try:
        await get_service_factory().ensure_auth()
        settings = get_settings()
        repo = f"{settings.org_name}/{settings.repo_name}"
        gh_client = get_service_factory().gh()

        api_args = ["api", f"repos/{repo}/milestones", "--method", "POST",
                    "-f", f"title={params.title}"]

        if params.description:
            api_args.extend(["-f", f"description={params.description}"])

        if params.due_date:
            api_args.extend(["-f", f"due_on={params.due_date}T23:59:59Z"])

        result = await gh_client.run(api_args)
        data = json.loads(result.stdout)

        return ToolSuccess(
            data={
                "number": data.get("number"),
                "title": data.get("title"),
                "html_url": data.get("html_url"),
                "message": f"Milestone '{params.title}' created successfully.",
            },
        ).model_dump()

    except CLIError as exc:
        logger.error("CLI error in create_milestone: %s", exc)
        if "already_exists" in exc.stderr.lower() or "422" in exc.stderr:
            return build_error_response(
                error_type="validation",
                message=f"Milestone '{params.title}' already exists.",
                suggestion="Use a different title or list_milestones to find existing ones.",
            )
        return build_error_response(
            error_type="internal",
            message=f"Failed to create milestone: {exc.stderr.strip()}",
            suggestion="Check the title format and token permissions.",
        )
    except Exception as exc:
        logger.error("Error in create_milestone: %s", exc)
        return handle_tool_error(exc, context="Create milestone failed")


async def close_milestone(params: CloseMilestoneInput) -> dict:
    """Close a milestone by title.

    Args:
        params: Input containing the milestone title to close.

    Returns:
        ToolSuccess on success, or ToolError if not found.
    """
    try:
        await get_service_factory().ensure_auth()
        settings = get_settings()
        repo = f"{settings.org_name}/{settings.repo_name}"
        gh_client = get_service_factory().gh()

        list_result = await gh_client.run([
            "api", f"repos/{repo}/milestones?state=open&per_page=100",
        ])
        milestones = json.loads(list_result.stdout)

        target = None
        for ms in milestones:
            if ms.get("title") == params.title:
                target = ms
                break

        if not target:
            return build_error_response(
                error_type="not_found",
                message=f"Milestone '{params.title}' not found among open milestones.",
                suggestion="Use list_milestones to see available milestones.",
            )

        await gh_client.run([
            "api", f"repos/{repo}/milestones/{target['number']}",
            "--method", "PATCH", "-f", "state=closed",
        ])

        return ToolSuccess(
            data={
                "number": target["number"],
                "title": params.title,
                "state": "closed",
                "message": f"Milestone '{params.title}' closed successfully.",
            },
        ).model_dump()

    except CLIError as exc:
        logger.error("CLI error in close_milestone: %s", exc)
        return build_error_response(
            error_type="internal",
            message=f"Failed to close milestone: {exc.stderr.strip()}",
            suggestion="Verify the milestone exists and token has write permissions.",
        )
    except Exception as exc:
        logger.error("Error in close_milestone: %s", exc)
        return handle_tool_error(exc, context="Close milestone failed")


async def list_milestones(params: ListMilestonesInput) -> dict:
    """List milestones in the repository.

    Args:
        params: Input containing state filter ('open', 'closed', or 'all').

    Returns:
        ToolSuccess with list of milestones.
    """
    try:
        await get_service_factory().ensure_auth()
        settings = get_settings()
        repo = f"{settings.org_name}/{settings.repo_name}"
        gh_client = get_service_factory().gh()

        result = await gh_client.run([
            "api", f"repos/{repo}/milestones?state={params.state}&per_page=100&sort=due_on",
        ])
        milestones = json.loads(result.stdout)

        items = []
        for ms in milestones:
            items.append({
                "number": ms.get("number"),
                "title": ms.get("title"),
                "state": ms.get("state"),
                "description": ms.get("description", ""),
                "due_on": ms.get("due_on"),
                "open_issues": ms.get("open_issues", 0),
                "closed_issues": ms.get("closed_issues", 0),
            })

        return ToolSuccess(
            data={"milestones": items, "count": len(items)},
        ).model_dump()

    except CLIError as exc:
        logger.error("CLI error in list_milestones: %s", exc)
        return build_error_response(
            error_type="internal",
            message=f"Failed to list milestones: {exc.stderr.strip()}",
            suggestion="Check token permissions and network connectivity.",
        )
    except Exception as exc:
        logger.error("Error in list_milestones: %s", exc)
        return handle_tool_error(exc, context="List milestones failed")
