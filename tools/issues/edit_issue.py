"""MCP tool for editing GitHub issues.

Exposes the `edit_issue` tool function that modifies issue properties
like title, body, milestone, labels, and assignees via the GH CLI.
"""
from __future__ import annotations

import logging
import os
import tempfile
from typing import Optional

from pydantic import BaseModel, Field

from clients.gh_cli_client import CLIError
from core.config import get_settings
from core.error_handling import build_error_response, handle_tool_error
from models.responses import ToolSuccess
from core.factory import get_service_factory

logger = logging.getLogger(__name__)


class EditIssueInput(BaseModel):
    """Input schema for the edit_issue tool."""

    issue_number: int = Field(description="GitHub issue number to edit")
    title: Optional[str] = Field(default=None, description="New title for the issue")
    body: Optional[str] = Field(default=None, description="New body content for the issue")
    milestone: Optional[str] = Field(default=None, description="Milestone title to assign")
    add_labels: Optional[list[str]] = Field(default=None, description="Labels to add")
    remove_labels: Optional[list[str]] = Field(default=None, description="Labels to remove")
    add_assignees: Optional[list[str]] = Field(default=None, description="GitHub usernames to assign")
    remove_assignees: Optional[list[str]] = Field(default=None, description="GitHub usernames to unassign")


async def edit_issue(
    issue_number: int,
    title: str | None = None,
    body: str | None = None,
    milestone: str | None = None,
    add_labels: list[str] | None = None,
    remove_labels: list[str] | None = None,
    add_assignees: list[str] | None = None,
    remove_assignees: list[str] | None = None,
) -> dict:
    """Edit a GitHub issue's properties.

    Modifies one or more properties of an existing issue including
    title, body, milestone, labels, and assignees. For long body content,
    uses --body-file with a temporary file to avoid shell argument limits.

    Args:
        issue_number: The GitHub issue number to edit.
        title: New title for the issue.
        body: New body content for the issue (supports long markdown content).
        milestone: Milestone title to assign.
        add_labels: Labels to add.
        remove_labels: Labels to remove.
        add_assignees: GitHub usernames to assign.
        remove_assignees: GitHub usernames to unassign.

    Returns:
        ToolSuccess with updated fields on success, or ToolError on failure.
    """
    try:
        await get_service_factory().ensure_auth()
        settings = get_settings()
        repo = f"{settings.org_name}/{settings.repo_name}"
        gh_client = get_service_factory().gh()

        args: list[str] = ["issue", "edit", str(issue_number), "--repo", repo]
        updated_fields: list[str] = []
        temp_file_path: str | None = None

        if title is not None:
            args.extend(["--title", title])
            updated_fields.append("title")

        if body is not None:
            # Use --body-file for all body content to avoid shell limits
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                suffix=".md",
                prefix="gh_body_",
                delete=False,
            ) as body_file:
                body_file.write(body)
                temp_file_path = body_file.name
            args.extend(["--body-file", temp_file_path])
            updated_fields.append("body")

        if milestone is not None:
            args.extend(["--milestone", milestone])
            updated_fields.append("milestone")

        if add_labels:
            for label in add_labels:
                args.extend(["--add-label", label])
            updated_fields.append(f"added labels: {add_labels}")

        if remove_labels:
            for label in remove_labels:
                args.extend(["--remove-label", label])
            updated_fields.append(f"removed labels: {remove_labels}")

        if add_assignees:
            for assignee in add_assignees:
                args.extend(["--add-assignee", assignee])
            updated_fields.append(f"added assignees: {add_assignees}")

        if remove_assignees:
            for assignee in remove_assignees:
                args.extend(["--remove-assignee", assignee])
            updated_fields.append(f"removed assignees: {remove_assignees}")

        if not updated_fields:
            return build_error_response(
                error_type="validation",
                message="No fields provided to update.",
                suggestion="Provide at least one field to modify (title, body, milestone, labels, or assignees).",
            )

        try:
            await gh_client.run(args)
        finally:
            if temp_file_path and os.path.exists(temp_file_path):
                os.unlink(temp_file_path)

        return ToolSuccess(
            data={
                "issue_number": issue_number,
                "updated_fields": updated_fields,
                "message": f"Issue #{issue_number} updated successfully.",
            },
        ).model_dump()

    except CLIError as exc:
        logger.error("CLI error in edit_issue: %s", exc)
        stderr = exc.stderr.strip()
        if "not found" in stderr.lower():
            return build_error_response(
                error_type="not_found",
                message=f"Issue #{issue_number} or referenced resource not found.",
                suggestion="Verify issue number, milestone title, label names, and usernames are correct.",
            )
        return build_error_response(
            error_type="internal",
            message=f"Failed to edit issue: {stderr}",
            suggestion="Check that all field values are valid and the token has write permissions.",
        )
    except Exception as exc:
        logger.error("Error in edit_issue: %s", exc)
        return handle_tool_error(exc, context="Edit issue failed")
