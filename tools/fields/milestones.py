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
from core.access import enforce_scope
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


class UpdateMilestoneInput(BaseModel):
    """Input schema for the update_milestone tool."""

    milestone_number: int = Field(gt=0, description="Milestone number to update")
    title: Optional[str] = Field(default=None, description="New milestone title", min_length=1)
    description: Optional[str] = Field(default=None, description="New milestone description")
    due_on: Optional[str] = Field(
        default=None,
        description="New due date as YYYY-MM-DD; null or empty removes the due date",
    )
    state: Optional[str] = Field(default=None, description="New milestone state: 'open' or 'closed'")
    owner: Optional[str] = Field(default=None, description="Optional GitHub owner override")
    repo: Optional[str] = Field(default=None, description="Optional repository name override")


async def update_milestone(params: UpdateMilestoneInput) -> dict:
    """Update selected fields of an existing milestone via the GitHub REST API.

    Args:
        params: Milestone number, optional fields to update, and optional target
            owner/repository overrides.

    Returns:
        ToolSuccess with the updated milestone, or a structured error envelope.
    """
    try:
        repository = f"{params.owner or '<configured-owner>'}/{params.repo or '<configured-repo>'}"
        updates: dict[str, object] = {}
        fields_set = params.model_fields_set
        if "title" in fields_set and params.title is not None:
            updates["title"] = params.title
        if "description" in fields_set and params.description is not None:
            updates["description"] = params.description
        if "due_on" in fields_set:
            if params.due_on is None or params.due_on == "":
                updates["due_on"] = None
            else:
                from datetime import datetime

                try:
                    datetime.strptime(params.due_on, "%Y-%m-%d")
                except ValueError:
                    return build_error_response(
                        error_type="validation",
                        message=f"Invalid due_on '{params.due_on}'.",
                        suggestion="due_on must be YYYY-MM-DD, null, or an empty string.",
                    )
                updates["due_on"] = f"{params.due_on}T00:00:00Z"
        if "state" in fields_set and params.state is not None:
            if params.state not in ("open", "closed"):
                return build_error_response(
                    error_type="validation",
                    message=f"Invalid state '{params.state}'.",
                    suggestion="state must be either 'open' or 'closed'.",
                )
            updates["state"] = params.state

        if not updates:
            return build_error_response(
                error_type="validation",
                message="Provide at least one field to update.",
                suggestion="Pass at least one of title, description, due_on, or state.",
            )

        enforce_scope(owner=params.owner, repo=params.repo)
        await get_service_factory().ensure_auth()
        settings = get_settings()
        owner = params.owner or settings.org_name
        repo = params.repo or settings.repo_name
        repository = f"{owner}/{repo}"
        gh_client = get_service_factory().gh()

        api_args = [
            "api",
            f"repos/{repository}/milestones/{params.milestone_number}",
            "--method",
            "PATCH",
        ]
        for key in ("title", "description", "state"):
            if key in updates:
                api_args.extend(["-f", f"{key}={updates[key]}"])
        if "due_on" in updates:
            if updates["due_on"] is None:
                api_args.extend(["-F", "due_on=null"])
            else:
                api_args.extend(["-f", f"due_on={updates['due_on']}"])

        result = await gh_client.run(api_args)
        data = json.loads(result.stdout)
        return ToolSuccess(
            data={
                "number": data.get("number", params.milestone_number),
                "title": data.get("title"),
                "description": data.get("description"),
                "state": data.get("state"),
                "due_on": data.get("due_on"),
                "html_url": data.get("html_url"),
                "updated_fields": sorted(updates),
                "message": f"Milestone #{params.milestone_number} updated successfully.",
            },
        ).model_dump()

    except CLIError as exc:
        logger.error("CLI error in update_milestone: %s", exc)
        stderr = exc.stderr.strip()
        if "404" in stderr or "not found" in stderr.lower():
            return build_error_response(
                error_type="not_found",
                message=f"Milestone #{params.milestone_number} not found in {repository}.",
                suggestion="Verify the milestone number and repository with list_milestones.",
            )
        return build_error_response(
            error_type="internal",
            message=f"Failed to update milestone: {stderr}",
            suggestion="Verify the milestone exists and token has write permissions.",
        )
    except Exception as exc:
        logger.error("Error in update_milestone: %s", exc)
        return handle_tool_error(exc, context="Update milestone failed")


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
