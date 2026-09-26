"""MCP tools for archiving, moving to Done, and moving to Trash.

Exposes three tool functions:
- `archive_project_item`: Archives an item from the project board.
- `move_to_done`: Sets the Status field to "Done" on a project item.
- `move_to_trash`: Sets the Status field to "Trash" on a project item.

All operations handle idempotent state transitions: if the item is
already in the requested state, a success response is returned
indicating no change was needed.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from core.error_handling import build_error_response, handle_tool_error
from core.exceptions import (
    GraphQLError,
    ValidationError,
)
from models.responses import ToolSuccess
from core.factory import get_service_factory

logger = logging.getLogger(__name__)


class ArchiveItemInput(BaseModel):
    """Input schema for the archive_project_item tool.

    Attributes:
        item_id: Project item node ID (e.g., "PVTI_...").
    """

    item_id: str = Field(description="Project item node ID to archive")


class MoveToStatusInput(BaseModel):
    """Input schema for move_to_done and move_to_trash tools.

    Attributes:
        item_id: Project item node ID (e.g., "PVTI_...").
    """

    item_id: str = Field(description="Project item node ID to update")


async def archive_project_item(params: ArchiveItemInput) -> dict:
    """Archive a project item from the GitHub Project board.

    Executes the archiveProjectV2Item GraphQL mutation to remove the
    item from the active project board. If the item is already archived,
    returns success with a no-change message.

    Args:
        params: Input containing the project item node ID to archive.

    Returns:
        A dict conforming to ToolSuccess (ok=True, data with item_id
        and status) on success, or ToolError (ok=False, error_type,
        message, suggestion) on failure.
    """
    try:
        graphql_client = await get_service_factory().graphql()
        gh_client = get_service_factory().gh()
        discovery_service = await get_service_factory().discovery_service(graphql_client)
        project_service = await get_service_factory().project_service(graphql_client, gh_client)

        # Get project metadata.
        metadata = await discovery_service.get_cached_or_discover()

        # Attempt to archive the item.
        try:
            await project_service.archive_item(
                metadata=metadata,
                item_id=params.item_id,
            )
        except GraphQLError as exc:
            # Check if already archived (idempotent).
            error_msg = str(exc).lower()
            if "already archived" in error_msg or "archived" in error_msg:
                return ToolSuccess(
                    data={
                        "item_id": params.item_id,
                        "status": "archived",
                        "message": "Item is already archived. No change needed.",
                    },
                ).model_dump()
            # Check for not-found errors.
            if "not found" in error_msg or "could not resolve" in error_msg:
                return build_error_response(
                    error_type="not_found",
                    message=(
                        f"Project item '{params.item_id}' was not found "
                        "in the project."
                    ),
                    suggestion=(
                        "Verify the item_id is a valid project item node ID. "
                        "Run list_project_items or discover_ids to find "
                        "valid item IDs."
                    ),
                    request_id=exc.request_id,
                )
            raise

        return ToolSuccess(
            data={
                "item_id": params.item_id,
                "status": "archived",
                "message": "Item archived successfully.",
            },
        ).model_dump()

    except Exception as exc:
        logger.error("Error in archive_project_item: %s", exc)
        return handle_tool_error(exc, context="Archive failed")


async def move_to_done(params: MoveToStatusInput) -> dict:
    """Move a project item to Done status.

    Sets the Status field to "Done" on the specified project item.
    If the item is already in "Done" status, returns success with a
    no-change message (idempotent behavior).

    Args:
        params: Input containing the project item node ID.

    Returns:
        A dict conforming to ToolSuccess (ok=True, data with item_id
        and status) on success, or ToolError (ok=False, error_type,
        message, suggestion) on failure.
    """
    return await _move_to_status(params.item_id, "Done")


async def move_to_trash(params: MoveToStatusInput) -> dict:
    """Move a project item to Trash status.

    Sets the Status field to "Trash" on the specified project item.
    If the item is already in "Trash" status, returns success with a
    no-change message (idempotent behavior).

    Args:
        params: Input containing the project item node ID.

    Returns:
        A dict conforming to ToolSuccess (ok=True, data with item_id
        and status) on success, or ToolError (ok=False, error_type,
        message, suggestion) on failure.
    """
    return await _move_to_status(params.item_id, "Trash")


async def _move_to_status(item_id: str, target_status: str) -> dict:
    """Move a project item to the specified status.

    Internal helper that handles the status transition for both
    move_to_done and move_to_trash tools.

    Args:
        item_id: The project item node ID to update.
        target_status: The target Status value (e.g., "Done", "Trash").

    Returns:
        A dict conforming to ToolSuccess or ToolError.
    """
    try:
        graphql_client = await get_service_factory().graphql()
        gh_client = get_service_factory().gh()
        discovery_service = await get_service_factory().discovery_service(graphql_client)
        project_service = await get_service_factory().project_service(graphql_client, gh_client)

        # Get project metadata to resolve Status field and option IDs.
        metadata = await discovery_service.get_cached_or_discover()

        # Check if item is already in the target status by attempting
        # to fetch it. If the update mutation returns no error but the
        # item is already in the state, we handle it as idempotent.
        try:
            await project_service.update_field(
                metadata=metadata,
                item_id=item_id,
                field_name="Status",
                value=target_status,
            )
        except GraphQLError as exc:
            error_msg = str(exc).lower()
            # Check for not-found errors.
            if "not found" in error_msg or "could not resolve" in error_msg:
                return build_error_response(
                    error_type="not_found",
                    message=(
                        f"Project item '{item_id}' was not found in the project."
                    ),
                    suggestion=(
                        "Verify the item_id is a valid project item node ID. "
                        "Run list_project_items or discover_ids to find "
                        "valid item IDs."
                    ),
                    request_id=exc.request_id,
                )
            # Check for already-in-state (idempotent).
            if "already" in error_msg or "no change" in error_msg:
                return ToolSuccess(
                    data={
                        "item_id": item_id,
                        "status": target_status,
                        "message": (
                            f"Item is already in '{target_status}' status. "
                            "No change needed."
                        ),
                    },
                ).model_dump()
            raise
        except ValidationError as exc:
            # The target status option doesn't exist in the project.
            logger.warning(
                "Validation error moving item to %s: %s",
                target_status,
                exc,
            )
            return build_error_response(
                error_type="validation",
                message=str(exc),
                suggestion=(
                    f"The '{target_status}' status option may not exist in "
                    "your project. Run discover_ids to see available options."
                ),
                request_id=exc.request_id,
            )

        return ToolSuccess(
            data={
                "item_id": item_id,
                "status": target_status,
                "message": f"Item moved to '{target_status}' successfully.",
            },
        ).model_dump()

    except Exception as exc:
        logger.error(
            "Error moving item to %s: %s", target_status, exc
        )
        return handle_tool_error(
            exc, context=f"Move to {target_status} failed"
        )
