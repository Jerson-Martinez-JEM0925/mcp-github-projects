"""MCP tool for adding an existing issue/PR to the project board (issue #18).

Exposes ``add_item_to_project``: given an issue or pull-request number, it
resolves the content node ID and calls ``addProjectV2ItemById`` via
:meth:`ProjectService.add_item_by_content_id`. Owner-type aware — the project
node ID already encodes the owner, so this works for both user- and
organization-owned boards. Adding an item already on the board is idempotent
(GitHub returns the existing item).
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from core.error_handling import build_error_response, handle_tool_error
from models.responses import ToolSuccess
from core.factory import get_service_factory

logger = logging.getLogger(__name__)


class AddItemToProjectInput(BaseModel):
    """Input schema for the add_item_to_project tool."""

    issue_or_pr_number: int = Field(
        gt=0,
        description="Issue or pull-request number to add to the project board",
    )


async def add_item_to_project(params: AddItemToProjectInput) -> dict:
    """Add an existing issue or PR to the configured project board.

    Resolves the issue/PR to its content node ID, then adds it via
    ``addProjectV2ItemById``. Returns the resulting project item node ID
    (which can then be passed to update_project_item_fields).

    Args:
        params: Input carrying the issue/PR number.

    Returns:
        Serialized ToolSuccess with ``item_id`` and ``issue_or_pr_number``,
        or ToolError on failure.
    """
    try:
        graphql_client = await get_service_factory().graphql()
        gh_client = get_service_factory().gh()

        discovery_service = await get_service_factory().discovery_service(graphql_client)
        project_service = await get_service_factory().project_service(graphql_client, gh_client)

        metadata = await discovery_service.get_cached_or_discover()

        content_id = await project_service.resolve_content_id(
            params.issue_or_pr_number
        )
        item_id = await project_service.add_item_by_content_id(
            metadata=metadata,
            content_id=content_id,
        )

        if not item_id:
            return build_error_response(
                error_type="internal",
                message=(
                    f"addProjectV2ItemById returned no item for "
                    f"#{params.issue_or_pr_number}."
                ),
                suggestion=(
                    "Verify the project exists and the token has project "
                    "write scope."
                ),
            )

        return ToolSuccess(
            data={
                "issue_or_pr_number": params.issue_or_pr_number,
                "item_id": item_id,
                "message": (
                    f"#{params.issue_or_pr_number} added to project "
                    f"{metadata.project_number} (item {item_id})."
                ),
            },
        ).model_dump()

    except Exception as exc:
        logger.error("Error in add_item_to_project: %s", exc)
        return handle_tool_error(exc, context="Add item to project failed")
