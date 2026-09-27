"""Discovery service for GitHub Project V2 metadata.

Executes the GraphQL discovery query to fetch project ID, field IDs,
and field option IDs, then caches the result locally. Provides
cache-aware access with TTL-based freshness checks.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from clients.cache_manager import CacheManager
from core.protocols import GraphQLExecutor
from core.config import get_settings
from core.exceptions import (
    AuthenticationError,
    GraphQLError,
    RateLimitError,
)
from graphql.queries import (
    get_discovery_query,
    get_query_variables,
    extract_project_data,
)
from models.context import GitHubContext
from models.metadata import (
    FieldOption,
    ProjectField,
    ProjectMetadata,
)

logger = logging.getLogger(__name__)


class DiscoveryService:
    """Discovers and caches GitHub Project V2 metadata.

    Uses the GraphQL API to fetch project ID, field IDs, and option IDs,
    then persists them in a local JSON cache with a 24-hour TTL.

    Args:
        graphql_client: Client for executing GraphQL queries.
        cache_manager: Manager for reading/writing the local cache file.
        cache_path: Optional override for the cache file location.
    """

    def __init__(
        self,
        graphql_client: GraphQLExecutor,
        cache_manager: CacheManager,
        cache_path: Path | None = None,
        context: GitHubContext | None = None,
    ) -> None:
        self._graphql_client = graphql_client
        self._cache_manager = cache_manager
        self._context = context or GitHubContext.from_settings(
            token_provider=lambda: ""
        )
        self._cache_path = cache_path or get_settings().cache_path

    async def discover(self, force: bool = False) -> ProjectMetadata:
        """Execute the discovery query and cache the results.

        Queries the GitHub GraphQL API for the project's node ID, all
        custom field IDs, and all field option IDs. Parses the response
        into a ProjectMetadata object and writes it to the cache file.

        Args:
            force: If True, skip cache check and always query the API.

        Returns:
            ProjectMetadata with all discovered IDs and timestamp.

        Raises:
            AuthenticationError: On HTTP 401 or insufficient token scopes.
            RateLimitError: On HTTP 403 with rate-limit headers.
            GraphQLError: On GraphQL-level errors in the response.
        """
        if not force:
            cached = self._cache_manager.read_cache(self._cache_path)
            if cached is not None and self._is_compatible(cached) and self._cache_manager.is_fresh(cached):
                logger.info("Using fresh cached metadata (age < TTL)")
                return cached

        target = self._context.target
        owner_type = target.owner_type
        variables = get_query_variables(
            owner_login=target.owner_login,
            project_number=target.project_number,
            owner_type=owner_type,
        )
        query = get_discovery_query(owner_type)

        try:
            response = await self._graphql_client.execute_with_retry(
                query,
                variables,
                is_mutation=False,
            )
        except AuthenticationError:
            logger.error(
                "Authentication failed during discovery. "
                "Ensure the token has repo, project, and read:org scopes."
            )
            raise
        except RateLimitError:
            logger.error("Rate limit exceeded during discovery.")
            raise
        except (GraphQLError, Exception) as exc:
            # Re-raise with context for any other API errors.
            if isinstance(exc, GraphQLError):
                raise
            raise GraphQLError(
                f"Discovery failed: {exc}",
                request_id=getattr(exc, "request_id", None),
            ) from exc

        metadata = self._parse_response(
            response,
            target.owner_login,
            target.project_number,
            owner_type=owner_type,
        )
        self._cache_manager.write_cache(metadata, self._cache_path)
        logger.info("Discovery complete — cached %d fields", len(metadata.fields))
        return metadata

    async def get_cached_or_discover(self) -> ProjectMetadata:
        """Return cached metadata if fresh, otherwise re-discover.

        Checks the local cache file for valid, fresh metadata. If the
        cache is missing, corrupted, or stale (older than TTL), triggers
        a fresh discovery query.

        Returns:
            ProjectMetadata from cache or fresh API query.

        Raises:
            AuthenticationError: On HTTP 401 or insufficient token scopes.
            RateLimitError: On HTTP 403 with rate-limit headers.
            GraphQLError: On GraphQL-level errors in the response.
        """
        cached = self._cache_manager.read_cache(self._cache_path)
        if cached is not None and self._is_compatible(cached) and self._cache_manager.is_fresh(cached):
            logger.debug("Returning fresh cached metadata")
            return cached

        if cached is not None:
            logger.info("Cache is stale (age >= TTL), re-discovering")
        else:
            logger.info("No valid cache found, discovering")

        return await self.discover(force=True)

    def _is_compatible(self, metadata: ProjectMetadata) -> bool:
        """Ensure a cache entry belongs to the configured project."""
        target = self._context.target
        return (
            metadata.owner == target.owner_login
            and metadata.project_number == target.project_number
        )

    def _parse_response(
        self,
        response: dict,
        owner: str,
        project_number: int,
        owner_type: str = "organization",
    ) -> ProjectMetadata:
        """Parse the GraphQL discovery response into ProjectMetadata.

        Handles different field type fragments and validates required keys.

        Args:
            response: Parsed response from GraphQLClient.execute_with_retry().
            owner: Organization or user login name.
            project_number: Project number.
            owner_type: 'organization' or 'user'.

        Returns:
            A fully populated ProjectMetadata instance.

        Raises:
            GraphQLError: If the response structure is unexpected.
        """
        data = response.get("data", {})
        request_id = response.get("request_id")

        # Extract project data based on owner_type (organization or user).
        project = extract_project_data(response, owner_type)
        if not project:
            owner_label = "user" if owner_type == "user" else "organization"
            raise GraphQLError(
                f"Project V2 number {project_number} not found for {owner_label} '{owner}'. "
                "Verify the project exists and the token has project scope.",
                request_id=request_id,
            )

        project_id: str = project["id"]
        field_nodes: list[dict] = project.get("fields", {}).get("nodes", [])

        fields: dict[str, ProjectField] = {}
        for node in field_nodes:
            if not node:
                # GraphQL fragments can return null nodes for unmatched types.
                continue

            field_id = node.get("id")
            field_name = node.get("name")
            data_type = node.get("dataType")

            if not all([field_id, field_name, data_type]):
                # Skip nodes that don't have the minimum required fields.
                continue

            # Extract options for SINGLE_SELECT fields.
            options: list[FieldOption] = []
            raw_options = node.get("options", [])
            for opt in raw_options:
                if opt and "id" in opt and "name" in opt:
                    options.append(FieldOption(id=opt["id"], name=opt["name"]))

            fields[field_name] = ProjectField(
                id=field_id,
                name=field_name,
                data_type=data_type,
                options=options,
            )

        return ProjectMetadata(
            project_id=project_id,
            owner=owner,
            project_number=project_number,
            fields=fields,
            discovered_at=datetime.now(timezone.utc),
        )
