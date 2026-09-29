"""Project service for GitHub Project V2 item operations.

Provides business logic for listing, adding, updating, and archiving
project items. Delegates GraphQL operations to the GraphQLClient and
CLI operations to the GHCLIClient. Applies client-side filtering with
validation against discovered field options.
"""

from __future__ import annotations

import json
import logging
from datetime import date

from core.protocols import GHCLIRunner
from core.protocols import GraphQLExecutor
from core.config import get_settings
from core.exceptions import ValidationError
from graphql.mutations import (
    ADD_ITEM_TO_PROJECT_MUTATION,
    ARCHIVE_ITEM_MUTATION,
    UPDATE_FIELD_MUTATION,
)
from graphql.queries import (
    get_list_items_query,
    get_query_variables,
)
from models.context import GitHubContext
from models.items import ProjectItem
from models.metadata import ProjectField, ProjectMetadata
from services.field_names import bare_field_name, resolve_field

logger = logging.getLogger(__name__)


def bare_option_name(name: str) -> str:
    """Backward-compatible alias for option/field prefix normalization."""
    return bare_field_name(name)


def _resolve_field_tolerant(
    metadata: ProjectMetadata, field_name: str
) -> ProjectField | None:
    """Backward-compatible wrapper for shared field resolution."""
    return resolve_field(metadata, field_name)


class ProjectService:
    """Manages project item operations: list, add, update, archive.

    Combines GraphQL queries/mutations with GH CLI commands to provide
    full CRUD functionality on GitHub Project V2 items.

    Args:
        graphql_client: Client for executing GraphQL queries and mutations.
        gh_client: Client for executing GH CLI commands.
    """

    def __init__(
        self,
        graphql_client: GraphQLExecutor,
        gh_client: GHCLIRunner,
        context: GitHubContext | None = None,
    ) -> None:
        self._graphql_client = graphql_client
        self._gh_client = gh_client
        self._context = context or GitHubContext.from_settings(
            token_provider=lambda: ""
        )

    async def list_items(
        self,
        metadata: ProjectMetadata,
        filters: dict | None = None,
    ) -> list[ProjectItem]:
        """List project items with optional filters.

        Fetches all project items via paginated GraphQL queries, parses
        them into ProjectItem objects, then applies client-side filters.

        Steps:
        1. Execute LIST_ITEMS_QUERY with pagination (fetch up to max_items)
        2. Parse response nodes into ProjectItem objects
        3. Validate filter values against discovered field options
        4. Apply client-side filters on the result set

        Filter logic:
        - status: exact match on item.status
        - priority: exact match on item.priority
        - labels: any-match (item has at least one of the filter labels)
        - assignee: exact match (item has that assignee in its list)
        - milestone: exact match on item.milestone
        - due_date + due_date_op:
          - "before": item.due_date < filter_date
          - "after": item.due_date > filter_date
          - "exact": item.due_date == filter_date

        Args:
            metadata: Discovered project metadata with field/option IDs.
            filters: Optional dict with filter keys (status, priority,
                     labels, assignee, milestone, due_date, due_date_op).

        Returns:
            List of ProjectItem objects matching all filters, up to 200.

        Raises:
            ValidationError: If a filter value is not a valid field option.
        """
        filters = filters or {}
        settings = get_settings()

        # Validate filter values against known field options before fetching.
        self._validate_filters(metadata, filters)

        # Fetch all items with pagination.
        items = await self._fetch_all_items(metadata, settings.max_items)

        # Apply client-side filters.
        filtered = self._apply_filters(items, filters)

        return filtered[:settings.max_items]

    async def list_all_items(
        self,
        metadata: ProjectMetadata,
    ) -> list[ProjectItem]:
        """List EVERY item on the board, paging past ``GH_PROJECT_MAX_ITEMS``.

        Unlike :meth:`list_items` (which caps at ``max_items`` for interactive
        listing), this consumes the whole board via exhaustive pagination.
        Reconciliation tools that must not miss any card on large boards use
        this. Owner-type aware (org/user) via ``_fetch_all_items``.

        Args:
            metadata: Discovered project metadata.

        Returns:
            All parsed ProjectItem objects on the board.
        """
        return await self._fetch_all_items(
            metadata, get_settings().max_items, exhaustive=True
        )

    async def add_item(
        self,
        metadata: ProjectMetadata,
        issue_number: int,
    ) -> str:
        """Add an issue to the project.

        Uses the `gh project item-add` CLI command to add an existing
        issue to the GitHub Project board.

        Args:
            metadata: Discovered project metadata.
            issue_number: The issue number to add to the project.

        Returns:
            The project item node ID of the newly added item.

        Raises:
            CLIError: If the GH CLI command fails.
            CLITimeoutError: If the command exceeds the configured timeout.
        """
        target = self._context.target
        if target.repository is None:
            raise ValidationError(
                "Adding an issue requires a target repository"
            )
        issue_url = (
            f"https://github.com/{target.owner_login}/{target.repository}"
            f"/issues/{issue_number}"
        )

        result = await self._gh_client.run([
            "project",
            "item-add",
            str(metadata.project_number),
            "--owner",
            metadata.owner,
            "--url",
            issue_url,
            "--format",
            "json",
        ])

        # Parse the item ID from stdout.
        # The gh CLI outputs JSON with the item ID when --format json is used.
        try:
            output = json.loads(result.stdout)
            if isinstance(output, dict):
                item_id = (
                    output.get("id")
                    or (output.get("item") or {}).get("id")
                    or output.get("item_id")
                    or ""
                )
            else:
                item_id = ""
        except (json.JSONDecodeError, AttributeError, TypeError):
            # Fallback: try to extract ID from plain text output.
            item_id = result.stdout.strip()

        if not item_id:
            raise ValidationError(
                "GitHub CLI item-add returned no project item ID. "
                "Expected JSON with an 'id' (or 'item.id') field; "
                "verify the gh CLI version and project write permissions."
            )

        logger.info(
            "Added issue #%d to project %d, item ID: %s",
            issue_number,
            metadata.project_number,
            item_id,
        )
        return item_id

    async def resolve_item_id(
        self,
        metadata: ProjectMetadata,
        issue_or_pr_number: int,
    ) -> str | None:
        """Resolve a project item node ID from an issue / PR number.

        Scans the board (owner-type aware — works on both user- and
        organization-owned projects, see issue #17) and returns the node
        ID of the item whose underlying Issue/PR number matches.

        Args:
            metadata: Discovered project metadata.
            issue_or_pr_number: The issue or pull-request number to locate.

        Returns:
            The project item node ID, or None if the number is not on the
            board.
        """
        items = await self._fetch_all_items(metadata, get_settings().max_items)
        for item in items:
            if item.issue_number == issue_or_pr_number:
                return item.node_id
        return None

    async def resolve_content_id(
        self,
        issue_or_pr_number: int,
    ) -> str:
        """Resolve an issue / PR number to its content node ID via ``gh``.

        The node ID is required by ``addProjectV2ItemById``. Issues and pull
        requests share a numbering space in a repo but are distinct REST
        resources, so this tries the issue endpoint first and falls back to
        the pulls endpoint.

        Args:
            issue_or_pr_number: The issue or pull-request number.

        Returns:
            The GraphQL global node ID (``node_id``) of the issue or PR.

        Raises:
            ValidationError: If the target repository is unknown, or the
                number resolves to neither an issue nor a PR.
        """
        target = self._context.target
        if target.repository is None:
            raise ValidationError(
                "Resolving a content ID requires a target repository"
            )
        repo = f"{target.owner_login}/{target.repository}"

        for kind in ("issues", "pulls"):
            try:
                result = await self._gh_client.run([
                    "api",
                    f"repos/{repo}/{kind}/{issue_or_pr_number}",
                    "--jq",
                    ".node_id",
                ])
            except Exception:  # noqa: BLE001 - try the next resource kind
                continue
            node_id = (result.stdout or "").strip()
            if node_id:
                return node_id

        raise ValidationError(
            f"#{issue_or_pr_number} is neither an issue nor a pull request "
            f"in {repo}, or is not accessible with the current token."
        )

    async def add_item_by_content_id(
        self,
        metadata: ProjectMetadata,
        content_id: str,
    ) -> str:
        """Add an issue/PR to the project by its content node ID via GraphQL.

        Uses ``addProjectV2ItemById`` (owner-agnostic — the project node ID
        already encodes the owner), returning the created (or pre-existing)
        item node ID. Adding an item that is already on the board is
        idempotent: GitHub returns the existing item.

        Args:
            metadata: Discovered project metadata (provides project_id).
            content_id: The issue/PR global node ID.

        Returns:
            The project item node ID.

        Raises:
            GraphQLError: If the mutation fails.
        """
        variables = {
            "projectId": metadata.project_id,
            "contentId": content_id,
        }
        response = await self._graphql_client.execute_with_retry(
            ADD_ITEM_TO_PROJECT_MUTATION,
            variables,
            is_mutation=True,
        )
        item_id: str = (
            response.get("data", {})
            .get("addProjectV2ItemById", {})
            .get("item", {})
            .get("id", "")
        )
        if not item_id:
            raise ValidationError(
                "GitHub returned no project item ID after addProjectV2ItemById. "
                "Verify project access and refresh metadata with discover_ids."
            )
        logger.info(
            "Added content %s to project %d, item ID: %s",
            content_id,
            metadata.project_number,
            item_id,
        )
        return item_id

    async def update_field(
        self,
        metadata: ProjectMetadata,
        item_id: str,
        field_name: str,
        value: str | float,
    ) -> None:
        """Update a single field on a project item.

        Resolves field_name to field_id via metadata. For single-select
        fields, resolves the value to an option_id. Constructs the
        appropriate value format based on field data type.

        Args:
            metadata: Discovered project metadata with field/option IDs.
            item_id: The project item node ID to update.
            field_name: The display name of the field to update.
            value: The new value (string for text/date/single-select,
                   float for number fields).

        Raises:
            ValidationError: If field_name is not found in metadata,
                or if value is not a valid option for single-select fields.
        """
        # Resolve field_name using the same exact/case-insensitive/prefix-tolerant
        # rules used by validation and defaults.
        field = resolve_field(metadata, field_name)
        if field is None:
            valid_fields = list(metadata.fields.keys())
            raise ValidationError(
                f"Field '{field_name}' not found in project. "
                f"Valid fields: {', '.join(valid_fields)}"
            )

        # Construct the value payload based on field type.
        field_value = self._build_field_value(field, value)

        variables = {
            "projectId": metadata.project_id,
            "itemId": item_id,
            "fieldId": field.id,
            "value": field_value,
        }

        await self._graphql_client.execute_with_retry(
            UPDATE_FIELD_MUTATION,
            variables,
            is_mutation=True,
        )

        logger.info(
            "Updated field '%s' on item %s to '%s'",
            field_name,
            item_id,
            value,
        )

    async def archive_item(
        self,
        metadata: ProjectMetadata,
        item_id: str,
    ) -> None:
        """Archive a project item.

        Executes the archiveProjectV2Item GraphQL mutation to remove
        the item from the active project board.

        Args:
            metadata: Discovered project metadata.
            item_id: The project item node ID to archive.

        Raises:
            GraphQLError: If the mutation fails.
        """
        variables = {
            "projectId": metadata.project_id,
            "itemId": item_id,
        }

        await self._graphql_client.execute_with_retry(
            ARCHIVE_ITEM_MUTATION,
            variables,
            is_mutation=True,
        )

        logger.info("Archived item %s from project", item_id)

    # ── Private Helpers ──────────────────────────────────────────────────────

    def _validate_filters(
        self,
        metadata: ProjectMetadata,
        filters: dict,
    ) -> None:
        """Validate filter values against discovered field options.

        For Status and Priority filters, checks that the provided value
        exists as a valid field option. Raises ValidationError with a
        list of valid options if the value is invalid.

        Args:
            metadata: Project metadata with field definitions.
            filters: Dict of filter keys and values.

        Raises:
            ValidationError: If a filter value is not a valid option.
        """
        # Validate Status filter.
        status_value = filters.get("status")
        if status_value is not None:
            self._validate_field_option(metadata, "Status", status_value)

        # Validate Priority filter.
        priority_value = filters.get("priority")
        if priority_value is not None:
            self._validate_field_option(metadata, "Priority", priority_value)

    def _validate_field_option(
        self,
        metadata: ProjectMetadata,
        field_name: str,
        value: str,
    ) -> None:
        """Validate a single value against a field's options.

        Args:
            metadata: Project metadata with field definitions.
            field_name: The field to validate against.
            value: The value to check.

        Raises:
            ValidationError: If value is not in the field's options.
        """
        field = resolve_field(metadata, field_name)
        if field is None:
            # Field doesn't exist in project — skip validation.
            return

        if not field.options:
            # Field has no options (not single-select) — skip validation.
            return

        valid_names = [opt.name for opt in field.options]
        if value not in valid_names:
            raise ValidationError(
                f"Invalid value '{value}' for field '{field_name}'. "
                f"Valid options: {', '.join(valid_names)}"
            )

    async def _fetch_all_items(
        self,
        metadata: ProjectMetadata,
        max_items: int,
        exhaustive: bool = False,
    ) -> list[ProjectItem]:
        """Fetch all project items with pagination.

        Executes the LIST_ITEMS_QUERY repeatedly, following pagination
        cursors until all items are fetched or max_items is reached.

        Args:
            metadata: Project metadata for query variables.
            max_items: Maximum number of items to fetch (ignored when
                ``exhaustive`` is True, except as an absolute safety ceiling).
            exhaustive: When True, follow pagination cursors until the board is
                fully consumed (``hasNextPage`` is false), regardless of
                ``max_items``. Used by reconciliation tools that must see every
                card even on boards larger than ``GH_PROJECT_MAX_ITEMS``.

        Returns:
            List of parsed ProjectItem objects.
        """
        items: list[ProjectItem] = []
        cursor: str | None = None
        page_size = get_settings().page_size
        # Absolute ceiling so an exhaustive scan can never loop unbounded on a
        # pathological board or a cursor bug.
        hard_ceiling = 100_000 if exhaustive else max_items

        # Owner-type awareness: user-owned boards are reached via
        # ``user(login:)`` with a ``$login`` variable, org boards via
        # ``organization(login:)`` with ``$org`` (see issue #17). The
        # context already resolves 'auto' → concrete type at build time.
        owner_type = self._context.target.owner_type
        query = get_list_items_query(owner_type)
        owner_key = "user" if owner_type == "user" else "organization"

        while len(items) < hard_ceiling:
            if exhaustive:
                fetch_count = page_size
            else:
                remaining = max_items - len(items)
                fetch_count = min(page_size, remaining)

            variables = get_query_variables(
                owner_login=metadata.owner,
                project_number=metadata.project_number,
                owner_type=owner_type,
                first=fetch_count,
            )
            if cursor is not None:
                variables["after"] = cursor

            response = await self._graphql_client.execute_with_retry(
                query,
                variables,
                is_mutation=False,
            )

            data = response.get("data", {})
            project_data = (
                data.get(owner_key, {})
                .get("projectV2", {})
                .get("items", {})
            )

            nodes = project_data.get("nodes", [])
            page_info = project_data.get("pageInfo", {})

            for node in nodes:
                if node is None:
                    continue
                item = self._parse_item_node(node)
                if item is not None:
                    items.append(item)

            # Check if there are more pages.
            has_next = page_info.get("hasNextPage", False)
            next_cursor = page_info.get("endCursor")
            if not has_next or not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor

        return items

    def _parse_item_node(self, node: dict) -> ProjectItem | None:
        """Parse a single GraphQL item node into a ProjectItem.

        Handles both Issue and DraftIssue content types, extracting
        field values from the fieldValues fragment.

        Args:
            node: A single node from the items.nodes array.

        Returns:
            A ProjectItem instance, or None if the node is unparseable.
        """
        if not isinstance(node, dict):
            return None

        content = node.get("content")
        if not isinstance(content, dict):
            return None

        content_type = content.get("__typename", "")
        node_id: str = node.get("id", "")

        # Extract content-level fields.
        title = ""
        body = ""
        issue_number: int | None = None
        issue_url: str | None = None
        assignees: list[str] = []
        labels: list[str] = []
        content_state: str | None = None

        if content_type in ("Issue", "PullRequest"):
            title = content.get("title", "")
            body = content.get("body", "")
            issue_number = content.get("number")
            issue_url = content.get("url")
            content_state = content.get("state")
            assignees = [
                a.get("login")
                for a in content.get("assignees", {}).get("nodes", [])
                if isinstance(a, dict) and a.get("login")
            ]
            labels = [
                lbl.get("name")
                for lbl in content.get("labels", {}).get("nodes", [])
                if isinstance(lbl, dict) and lbl.get("name")
            ]
        elif content_type == "DraftIssue":
            title = content.get("title", "")
            body = content.get("body", "")
        else:
            # Unknown content type — skip.
            return None

        # Extract project-level field values.
        status: str | None = None
        priority: str | None = None
        milestone: str | None = None
        due_date: str | None = None
        estimate: float | None = None

        field_values = node.get("fieldValues", {}).get("nodes", [])
        for fv in field_values:
            if not fv:
                continue

            field_info = fv.get("field")
            if not field_info:
                continue
            field_name = field_info.get("name", "")
            canonical_field_name = bare_option_name(field_name)

            # ProjectV2ItemFieldSingleSelectValue
            if "name" in fv and field_name:
                select_value = fv.get("name")
                if canonical_field_name == "status":
                    status = select_value
                elif canonical_field_name == "priority":
                    priority = select_value

            # ProjectV2ItemFieldDateValue
            if "date" in fv and field_name:
                date_value = fv.get("date")
                if canonical_field_name == "due date":
                    due_date = date_value

            # ProjectV2ItemFieldNumberValue
            if "number" in fv and field_name:
                number_value = fv.get("number")
                if canonical_field_name == "estimate":
                    estimate = number_value

            # ProjectV2ItemFieldMilestoneValue
            if "milestone" in fv and field_name:
                milestone_data = fv.get("milestone")
                if milestone_data and canonical_field_name == "milestone":
                    milestone = milestone_data.get("title")

            # ProjectV2ItemFieldTextValue — title field from text type.
            # (Title is handled from content, not fieldValues.)

        return ProjectItem(
            node_id=node_id,
            content_type=content_type,
            title=title,
            body=body,
            issue_number=issue_number,
            issue_url=issue_url,
            status=status,
            priority=priority,
            milestone=milestone,
            due_date=due_date,
            estimate=estimate,
            assignees=assignees,
            labels=labels,
            content_state=content_state,
        )

    def _apply_filters(
        self,
        items: list[ProjectItem],
        filters: dict,
    ) -> list[ProjectItem]:
        """Apply client-side filters to a list of project items.

        All filters are conjunctive (AND): an item must match all
        specified filters to be included in the result.

        Args:
            items: List of ProjectItem objects to filter.
            filters: Dict of filter keys and values.

        Returns:
            Filtered list of ProjectItem objects.
        """
        result = items

        # Status: exact match.
        status_filter = filters.get("status")
        if status_filter is not None:
            result = [item for item in result if item.status == status_filter]

        # Priority: exact match.
        priority_filter = filters.get("priority")
        if priority_filter is not None:
            result = [item for item in result if item.priority == priority_filter]

        # Labels: any-match (item has at least one of the filter labels).
        labels_filter = filters.get("labels")
        if labels_filter is not None:
            filter_labels_set = set(labels_filter)
            result = [
                item for item in result
                if set(item.labels) & filter_labels_set
            ]

        # Assignee: exact match (item has that assignee in its list).
        assignee_filter = filters.get("assignee")
        if assignee_filter is not None:
            result = [
                item for item in result
                if assignee_filter in item.assignees
            ]

        # Milestone: exact match.
        milestone_filter = filters.get("milestone")
        if milestone_filter is not None:
            result = [
                item for item in result
                if item.milestone == milestone_filter
            ]

        # Due date with operator.
        due_date_filter = filters.get("due_date")
        due_date_op = filters.get("due_date_op", "exact")
        if due_date_filter is not None:
            result = self._filter_by_due_date(result, due_date_filter, due_date_op)

        return result

    def _filter_by_due_date(
        self,
        items: list[ProjectItem],
        filter_date_str: str,
        operator: str,
    ) -> list[ProjectItem]:
        """Filter items by due date with a comparison operator.

        Args:
            items: List of items to filter.
            filter_date_str: ISO 8601 date string (YYYY-MM-DD).
            operator: One of "before", "after", "exact".

        Returns:
            Items whose due date satisfies the comparison.
        """
        if operator not in {"before", "after", "exact"}:
            raise ValidationError(
                f"Invalid due date operator '{operator}'. "
                "Expected one of: before, after, exact."
            )

        try:
            filter_date = date.fromisoformat(filter_date_str)
        except ValueError:
            raise ValidationError(
                f"Invalid due date format: '{filter_date_str}'. "
                "Expected ISO 8601 date format: YYYY-MM-DD"
            )

        filtered: list[ProjectItem] = []
        for item in items:
            if item.due_date is None:
                continue

            try:
                item_date = date.fromisoformat(str(item.due_date))
            except (TypeError, ValueError):
                # Skip items with unparseable due dates.
                continue

            if operator == "before" and item_date < filter_date:
                filtered.append(item)
            elif operator == "after" and item_date > filter_date:
                filtered.append(item)
            elif operator == "exact" and item_date == filter_date:
                filtered.append(item)

        return filtered

    def _build_field_value(
        self,
        field: ProjectField,
        value: str | float,
    ) -> dict:
        """Build the GraphQL field value payload based on field type.

        Constructs the appropriate value object for the
        updateProjectV2ItemFieldValue mutation.

        Args:
            field: The ProjectField definition from metadata.
            value: The raw value to set.

        Returns:
            A dict matching the ProjectV2FieldValue input type.

        Raises:
            ValidationError: If value doesn't match field type expectations.
        """
        data_type = field.data_type.upper()

        if data_type == "SINGLE_SELECT":
            # Resolve display name to option ID.
            option_id = self._resolve_option_id(field, str(value))
            return {"singleSelectOptionId": option_id}

        elif data_type == "DATE":
            # Validate ISO 8601 date format.
            date_str = str(value)
            try:
                date.fromisoformat(date_str)
            except ValueError:
                raise ValidationError(
                    f"Invalid date format for field '{field.name}': '{value}'. "
                    "Expected ISO 8601 date format: YYYY-MM-DD"
                )
            return {"date": date_str}

        elif data_type == "NUMBER":
            # Ensure value is numeric.
            try:
                numeric_value = float(value)
            except (TypeError, ValueError):
                raise ValidationError(
                    f"Invalid number format for field '{field.name}': '{value}'. "
                    "Expected a numeric value."
                )
            return {"number": numeric_value}

        elif data_type == "TEXT":
            return {"text": str(value)}

        else:
            raise ValidationError(
                f"Unsupported field type '{field.data_type}' for field '{field.name}'. "
                "Supported types: SINGLE_SELECT, DATE, NUMBER, TEXT"
            )

    def _resolve_option_id(
        self,
        field: ProjectField,
        value: str,
    ) -> str:
        """Resolve a display name to an option ID for single-select fields.

        Args:
            field: The ProjectField with options to search.
            value: The display name of the option.

        Returns:
            The option node ID.

        Raises:
            ValidationError: If value is not a valid option name.
        """
        for option in field.options:
            if option.name == value:
                return option.id

        # Fallback: match ignoring a leading emoji/symbol prefix and case, so
        # "Done" resolves "✅ Done" and "in progress" resolves "🛠 In Progress"
        # (move_to_done / move_to_trash pass bare names). Only an UNIQUE match
        # is accepted; ambiguity still fails with the list of valid options.
        wanted = bare_option_name(value)
        matches = [opt for opt in field.options if bare_option_name(opt.name) == wanted]
        if wanted and len(matches) == 1:
            return matches[0].id

        valid_options = [opt.name for opt in field.options]
        raise ValidationError(
            f"Invalid value '{value}' for field '{field.name}'. "
            f"Valid options: {', '.join(valid_options)}"
        )
