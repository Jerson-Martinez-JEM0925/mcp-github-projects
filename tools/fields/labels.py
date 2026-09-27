"""MCP tools for GitHub label management.

Exposes tools for creating and listing labels in the repository.
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


class CreateLabelInput(BaseModel):
    """Input schema for the create_label tool."""

    name: str = Field(description="Label name (e.g., '🔒 Security')")
    color: str = Field(description="Hex color without # (e.g., 'd93f0b')")
    description: Optional[str] = Field(default=None, description="Label description")


class ListLabelsInput(BaseModel):
    """Input schema for the list_labels tool."""

    limit: int = Field(default=50, description="Max labels to return")


async def create_label(params: CreateLabelInput) -> dict:
    """Create a new label in the repository.

    If the label already exists, updates it (force behavior).

    Args:
        params: Input containing name, color, and optional description.

    Returns:
        ToolSuccess with label details on success.
    """
    try:
        await get_service_factory().ensure_auth()
        settings = get_settings()
        repo = f"{settings.org_name}/{settings.repo_name}"
        gh_client = get_service_factory().gh()

        args = ["label", "create", params.name, "--repo", repo,
                "--color", params.color, "--force"]

        if params.description:
            args.extend(["--description", params.description])

        await gh_client.run(args)

        return ToolSuccess(
            data={
                "name": params.name,
                "color": params.color,
                "message": f"Label '{params.name}' created/updated successfully.",
            },
        ).model_dump()

    except CLIError as exc:
        logger.error("CLI error in create_label: %s", exc)
        return build_error_response(
            error_type="internal",
            message=f"Failed to create label: {exc.stderr.strip()}",
            suggestion="Check label name format and token permissions.",
        )
    except Exception as exc:
        logger.error("Error in create_label: %s", exc)
        return handle_tool_error(exc, context="Create label failed")


async def list_labels(params: ListLabelsInput) -> dict:
    """List all labels in the repository.

    Args:
        params: Input containing limit for results.

    Returns:
        ToolSuccess with list of labels.
    """
    try:
        await get_service_factory().ensure_auth()
        settings = get_settings()
        repo = f"{settings.org_name}/{settings.repo_name}"
        gh_client = get_service_factory().gh()

        result = await gh_client.run([
            "label", "list", "--repo", repo, "--limit", str(params.limit),
            "--json", "name,color,description",
        ])
        labels = json.loads(result.stdout)

        return ToolSuccess(
            data={"labels": labels, "count": len(labels)},
        ).model_dump()

    except CLIError as exc:
        logger.error("CLI error in list_labels: %s", exc)
        return build_error_response(
            error_type="internal",
            message=f"Failed to list labels: {exc.stderr.strip()}",
            suggestion="Check token permissions.",
        )
    except Exception as exc:
        logger.error("Error in list_labels: %s", exc)
        return handle_tool_error(exc, context="List labels failed")
