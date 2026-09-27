"""Field service for managing custom fields on GitHub Project V2 items.

Handles estimate validation and field creation for the GitHub Project
Management MCP Server. Uses GraphQL mutations for write operations.
"""

from __future__ import annotations

import logging

from core.protocols import GraphQLExecutor
from core.config import get_settings
from core.exceptions import ValidationError
from graphql.mutations import (
    CREATE_FIELD_MUTATION,
    UPDATE_FIELD_MUTATION,
)

logger = logging.getLogger(__name__)


class FieldService:
    """Service for managing project item field values.

    Provides estimate validation, field value updates, and
    automatic creation of the Estimate field when missing.
    """

    def __init__(self, graphql_client: GraphQLExecutor) -> None:
        self._client = graphql_client

    async def set_estimate(
        self,
        project_id: str,
        item_id: str,
        field_id: str,
        value: float,
    ) -> None:
        """Set the Estimate field value on a project item.

        Validates the value against configured range and granularity
        before executing the GraphQL mutation.

        Args:
            project_id: The project node ID (e.g., "PVT_...").
            item_id: The project item node ID.
            field_id: The Estimate field node ID.
            value: Numeric estimate in hours.

        Raises:
            ValidationError: If value is out of range or wrong granularity.
            GraphQLError: If the mutation fails.
        """
        settings = get_settings()

        if value < settings.estimate_min or value > settings.estimate_max:
            raise ValidationError(
                f"Estimate value {value} is out of range. "
                f"Accepted range: {settings.estimate_min} to {settings.estimate_max}."
            )

        if abs(value % settings.estimate_granularity) > 1e-9:
            raise ValidationError(
                f"Estimate value {value} does not meet granularity requirement. "
                f"Value must be a multiple of {settings.estimate_granularity}."
            )

        variables = {
            "projectId": project_id,
            "itemId": item_id,
            "fieldId": field_id,
            "value": {"number": value},
        }

        await self._client.execute_with_retry(
            UPDATE_FIELD_MUTATION,
            variables,
            is_mutation=True,
        )

        logger.info(
            "Set estimate to %s on item %s in project %s",
            value,
            item_id,
            project_id,
        )

    async def create_estimate_field(self, project_id: str) -> str:
        """Create a number-type "Estimate" field in the project.

        Uses the createProjectV2Field mutation to add a NUMBER field
        named "Estimate" to the specified project.

        Args:
            project_id: The project node ID (e.g., "PVT_...").

        Returns:
            The new field's node ID.

        Raises:
            GraphQLError: If field creation fails.
        """
        variables = {
            "projectId": project_id,
            "name": "Estimate",
            "dataType": "NUMBER",
        }

        result = await self._client.execute_with_retry(
            CREATE_FIELD_MUTATION,
            variables,
            is_mutation=True,
        )

        field_id: str = result["data"]["createProjectV2Field"]["projectV2Field"]["id"]

        logger.info(
            "Created Estimate field with ID %s in project %s",
            field_id,
            project_id,
        )

        return field_id

    async def ensure_estimate_field(
        self,
        project_id: str,
        field_id: str | None,
    ) -> str:
        """Ensure the Estimate field exists, creating it if needed.

        Args:
            project_id: The project node ID.
            field_id: The existing Estimate field ID, or None if not found.

        Returns:
            The Estimate field node ID (existing or newly created).

        Raises:
            GraphQLError: If field creation fails when field_id is None.
        """
        if field_id is not None:
            return field_id

        logger.info(
            "Estimate field not found in project %s, creating it",
            project_id,
        )
        return await self.create_estimate_field(project_id)
