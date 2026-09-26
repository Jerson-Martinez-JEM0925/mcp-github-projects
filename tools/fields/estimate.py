"""MCP tool for setting time estimates on GitHub Project V2 items.

Exposes the `set_estimate` tool function that validates the estimate value
against configured range and granularity, then delegates to the FieldService
to update the Estimate field on a project item. Automatically creates the
Estimate field if it doesn't exist in the project.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from core.config import get_settings
from core.error_handling import build_error_response, handle_tool_error
from core.exceptions import ValidationError
from models.responses import ToolSuccess
from core.factory import get_service_factory

logger = logging.getLogger(__name__)


class SetEstimateInput(BaseModel):
    """Input schema for the set_estimate tool.

    Attributes:
        item_id: Project item node ID (e.g., "PVTI_...").
        value: Estimate in hours. Must be between 0.25 and 9999,
               and a multiple of 0.25.
    """

    item_id: str = Field(description="Project item node ID")
    value: float = Field(
        description="Estimate in hours (0.25-9999, multiple of 0.25)"
    )


async def set_estimate(params: SetEstimateInput) -> dict:
    """Set the time estimate on a project item.

    Discovers project metadata to locate (or create) the Estimate field,
    validates the estimate value against configured range and granularity,
    then updates the field on the specified project item.

    Args:
        params: Input containing the project item node ID and the
                numeric estimate value in hours.

    Returns:
        A dict conforming to ToolSuccess (ok=True, data with item_id,
        estimate, and field_id) on success, or ToolError (ok=False,
        error_type, message, suggestion) on failure.
    """
    try:
        graphql_client = await get_service_factory().graphql()
        discovery_service = await get_service_factory().discovery_service(graphql_client)
        field_service = await get_service_factory().field_service(graphql_client)

        # 1. Get project metadata to find the Estimate field.
        metadata = await discovery_service.get_cached_or_discover()

        # 2. Find Estimate field in metadata (may be None if not yet created).
        estimate_field = metadata.fields.get("Estimate")
        estimate_field_id = estimate_field.id if estimate_field else None

        # 3. Ensure the Estimate field exists (create if missing).
        field_id = await field_service.ensure_estimate_field(
            project_id=metadata.project_id,
            field_id=estimate_field_id,
        )

        # 4. Set the estimate value (FieldService validates range + granularity).
        await field_service.set_estimate(
            project_id=metadata.project_id,
            item_id=params.item_id,
            field_id=field_id,
            value=params.value,
        )

        # 5. Return success response.
        return ToolSuccess(
            data={
                "item_id": params.item_id,
                "estimate": params.value,
                "field_id": field_id,
            },
        ).model_dump()

    except ValidationError as exc:
        settings = get_settings()
        logger.warning("Validation error in set_estimate: %s", exc)
        return build_error_response(
            error_type="validation",
            message=str(exc),
            suggestion=(
                f"Provide a value between {settings.estimate_min} and "
                f"{settings.estimate_max} that is a multiple of "
                f"{settings.estimate_granularity}."
            ),
            request_id=exc.request_id,
        )

    except Exception as exc:
        logger.error("Error in set_estimate: %s", exc)
        return handle_tool_error(exc, context="Set estimate failed")
