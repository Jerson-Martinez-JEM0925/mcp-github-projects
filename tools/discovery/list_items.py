"""MCP tool for listing GitHub Project V2 items with optional filters.

Exposes the `list_project_items` tool function that fetches project items
and applies client-side filtering by status, priority, labels, assignee,
milestone, and due date. Returns results in the ToolSuccess envelope or
ToolError on failure.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from core.error_handling import build_error_response, handle_tool_error
from models.responses import ToolSuccess
from core.factory import get_service_factory

logger = logging.getLogger(__name__)


class ListProjectItemsInput(BaseModel):
    """Input schema for the list_project_items tool.

    All filter fields are optional. When multiple filters are provided,
    they are applied conjunctively (AND): only items matching all filters
    are returned.

    Attributes:
        status: Filter by Status column (exact match).
        priority: Filter by Priority field (exact match).
        labels: Filter by labels (any-match: item has at least one).
        assignee: Filter by assignee username (exact match in list).
        milestone: Filter by milestone title (exact match).
        due_date: ISO 8601 date for due date comparison (YYYY-MM-DD).
        due_date_op: Comparison operator: "before", "after", or "exact".
    """

    status: str | None = Field(
        default=None, description="Filter by Status column"
    )
    priority: str | None = Field(
        default=None, description="Filter by Priority field"
    )
    labels: list[str] | None = Field(
        default=None, description="Filter by labels (any match)"
    )
    assignee: str | None = Field(
        default=None, description="Filter by assignee username"
    )
    milestone: str | None = Field(
        default=None, description="Filter by milestone title"
    )
    due_date: str | None = Field(
        default=None, description="ISO 8601 date (YYYY-MM-DD)"
    )
    due_date_op: str | None = Field(
        default=None, description="Comparison: before, after, exact"
    )


async def list_project_items(
    params: ListProjectItemsInput | None = None,
) -> dict:
    """List project items with optional field filters.

    Discovers project metadata, then delegates to ProjectService.list_items()
    which handles fetching via GraphQL and applying client-side filters.
    Returns a list of ProjectItem objects in a ToolSuccess envelope, an
    empty list message when no matches are found, or a ToolError for
    invalid filters or API failures.

    Args:
        params: Input containing optional filter criteria for status,
                priority, labels, assignee, milestone, and due date.

    Returns:
        A dict conforming to ToolSuccess (ok=True, data=list of items)
        on success, or ToolError (ok=False, error_type, message,
        suggestion) on failure.
    """
    try:
        params = params or ListProjectItemsInput()
        # Validate due_date_op if provided.
        if params.due_date_op is not None:
            valid_ops = ("before", "after", "exact")
            if params.due_date_op not in valid_ops:
                return build_error_response(
                    error_type="validation",
                    message=(
                        f"Invalid due_date_op '{params.due_date_op}'. "
                        f"Valid operators: {', '.join(valid_ops)}"
                    ),
                    suggestion=(
                        "Use one of: 'before', 'after', or 'exact' "
                        "as the due_date_op value."
                    ),
                )

        # Validate that due_date_op requires due_date.
        if params.due_date_op is not None and params.due_date is None:
            return build_error_response(
                error_type="validation",
                message=(
                    "due_date_op was provided without a due_date value."
                ),
                suggestion=(
                    "Provide a due_date in YYYY-MM-DD format when using "
                    "due_date_op."
                ),
            )

        # Resolve token and initialize services.
        graphql_client = await get_service_factory().graphql()
        gh_client = get_service_factory().gh()
        discovery_service = await get_service_factory().discovery_service(graphql_client)
        project_service = await get_service_factory().project_service(graphql_client, gh_client)

        # 1. Discover project metadata (cached or fresh).
        metadata = await discovery_service.get_cached_or_discover()

        # 2. Build filters dict from input params.
        filters: dict = {}
        if params.status is not None:
            filters["status"] = params.status
        if params.priority is not None:
            filters["priority"] = params.priority
        if params.labels is not None:
            filters["labels"] = params.labels
        if params.assignee is not None:
            filters["assignee"] = params.assignee
        if params.milestone is not None:
            filters["milestone"] = params.milestone
        if params.due_date is not None:
            filters["due_date"] = params.due_date
            filters["due_date_op"] = params.due_date_op or "exact"

        # 3. Delegate to ProjectService.list_items().
        items = await project_service.list_items(
            metadata=metadata,
            filters=filters if filters else None,
        )

        # 4. Build response.
        items_data = [item.model_dump(mode="json") for item in items]

        if not items_data:
            return ToolSuccess(
                data={
                    "items": [],
                    "count": 0,
                    "message": "No items matched the provided filters.",
                },
            ).model_dump()

        return ToolSuccess(
            data={
                "items": items_data,
                "count": len(items_data),
            },
        ).model_dump()

    except Exception as exc:
        logger.error("Error in list_project_items: %s", exc)
        return handle_tool_error(exc, context="List project items failed")
