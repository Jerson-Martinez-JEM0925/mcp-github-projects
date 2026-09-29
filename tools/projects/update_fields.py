"""MCP tool for updating fields on a project item.

Exposes the `update_project_item_fields` tool function that accepts a
project item node ID (or an issue/PR number to resolve one, owner-type
aware) and a dict of field name → value pairs. Applies updates
sequentially, collecting per-field results (success/failure). Returns all
current field values on full success, or a partial success response with
per-field outcomes when some updates fail.

Fields are routed to the appropriate update mechanism:
- Any board field (SINGLE_SELECT such as Status/Priority/Area/Work Type,
  NUMBER such as Estimate, DATE such as Due date, TEXT) → resolved against
  discovered metadata and written via ProjectService.update_field() GraphQL
- Body, Assignees, Labels → IssueService.update() via gh issue edit

Optionally fills omitted board fields with configured defaults
(apply_defaults) and can enforce that no board field is left unset
(GH_PROJECT_ENFORCE_FIELDS). See issue #14 / #17.
"""

from __future__ import annotations

import logging
import re
from datetime import date
from typing import Any

from pydantic import BaseModel, Field

from clients.gh_cli_client import CLIError
from core.protocols import GraphQLExecutor
from core.config import get_settings
from core.error_handling import build_error_response, handle_tool_error
from core.exceptions import (
    GitHubProjectError,
    GraphQLError,
    ValidationError,
)
from graphql.queries import GET_ITEM_STATUS_QUERY
from models.metadata import ProjectMetadata
from models.responses import ToolSuccess
from services.field_defaults import compute_defaults, unset_board_fields
from services.field_names import resolve_field_name
from services.issue_service import IssueService
from services.project_service import ProjectService
from core.factory import get_service_factory

logger = logging.getLogger(__name__)

# Fields that route through the Issues API (gh issue edit).
_ISSUE_API_FIELDS: frozenset[str] = frozenset({"body", "assignees", "labels"})

# Well-known canonical field name mapping (user-friendly → board name).
# Any OTHER name is matched case-insensitively against discovered board
# fields at runtime, so custom single-selects (Area, Work Type), NUMBER
# (Estimate) and DATE fields all resolve without a hardcoded whitelist
# (issue #14).
_FIELD_NAME_MAP: dict[str, str] = {
    "status": "Status",
    "priority": "Priority",
    "milestone": "Milestone",
    "due_date": "Due date",
    "due date": "Due date",
    "duedate": "Due date",
    "estimate": "Estimate",
    "area": "Area",
    "work_type": "Work Type",
    "work type": "Work Type",
    "worktype": "Work Type",
    "body": "body",
    "assignees": "assignees",
    "assignee": "assignees",
    "labels": "labels",
    "label": "labels",
}


class UpdateFieldsInput(BaseModel):
    """Input schema for the update_project_item_fields tool.

    Attributes:
        item_id: The project item node ID to update. Alternatively pass
            issue_number to resolve the item on the board (owner-type aware).
        issue_number: Issue / PR number to resolve to an item_id when item_id
            is not supplied (works on user- and org-owned boards, issue #17).
        fields: Dict mapping field names to their new values.
        apply_defaults: When True, fill omitted board fields (Priority, Area,
            Estimate, Due date, Work Type, Status) with configured defaults
            before writing (issue #14).
        labels: Issue labels used to infer the Work Type default.
        enforce: Override GH_PROJECT_ENFORCE_FIELDS for this call; when True
            and a board field is still unset after defaults, the call errors.
    """

    item_id: str | None = Field(
        default=None,
        description="Project item node ID to update (or pass issue_number)",
    )
    issue_number: int | None = Field(
        default=None,
        description=(
            "Issue/PR number to resolve to an item_id when item_id is omitted"
        ),
    )
    fields: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Dict of field name → value pairs. Supported: any board field — "
            "SINGLE_SELECT (Status, Priority, Area, Work Type — value is the "
            "option name), NUMBER (Estimate), DATE (Due date, YYYY-MM-DD), "
            "TEXT; plus issue fields body (string), assignees (list), "
            "labels (list)."
        ),
    )
    apply_defaults: bool = Field(
        default=False,
        description=(
            "Fill omitted board fields with configured defaults before "
            "writing (Due date=today+N, Estimate, Priority, Status, Area, "
            "Work Type inferred from labels)."
        ),
    )
    labels: list[str] | None = Field(
        default=None,
        description="Issue labels, used to infer the Work Type default",
    )
    enforce: bool | None = Field(
        default=None,
        description=(
            "Override GH_PROJECT_ENFORCE_FIELDS; error if a board field is "
            "still unset after defaults"
        ),
    )


async def update_project_item_fields(params: UpdateFieldsInput) -> dict:
    """Update one or more fields on a project item.

    For each field in the request:
    - Status, Priority, Milestone → ProjectService.update_field() via GraphQL
    - Due date → ProjectService.update_field() (date type)
    - body → IssueService.update() via gh issue edit
    - assignees → IssueService.update() (replaces entire list)
    - labels → IssueService.update() (replaces entire list)

    Updates are applied sequentially. Successful changes are never rolled
    back on partial failure.

    Args:
        params: Validated UpdateFieldsInput with item_id and fields dict.

    Returns:
        Serialized ToolSuccess or ToolError dict.
    """
    try:
        # Resolve token and initialize services.
        graphql_client = await get_service_factory().graphql()
        gh_client = get_service_factory().gh()

        discovery_service = await get_service_factory().discovery_service(graphql_client)
        project_service = await get_service_factory().project_service(graphql_client, gh_client)
        issue_service = get_service_factory().issue_service(gh_client)

        # 1. Discover project metadata for field resolution.
        metadata = await discovery_service.get_cached_or_discover()

        # 2. Resolve the item_id — accept an explicit id, else resolve from
        #    an issue/PR number on the board (owner-type aware, issue #17).
        item_id = params.item_id
        if not item_id:
            if params.issue_number is None:
                return build_error_response(
                    error_type="validation",
                    message="Provide either item_id or issue_number.",
                    suggestion=(
                        "Pass the project item node ID as item_id, or the "
                        "issue/PR number as issue_number to resolve it."
                    ),
                )
            item_id = await project_service.resolve_item_id(
                metadata, params.issue_number
            )
            if not item_id:
                return build_error_response(
                    error_type="not_found",
                    message=(
                        f"Issue/PR #{params.issue_number} is not on the "
                        "project board."
                    ),
                    suggestion=(
                        "Add it first with add_item_to_project, then retry."
                    ),
                )

        # 3. Verify the item exists and get its issue_number/content type.
        item_info = await _resolve_item(graphql_client, item_id)
        if item_info is None:
            return build_error_response(
                error_type="not_found",
                message=(
                    f"Project item '{item_id}' not found. "
                    "The item may have been deleted or the ID is invalid."
                ),
                suggestion=(
                    "Verify the item_id is correct by running list_project_items. "
                    "Run discover_ids if the project structure has changed."
                ),
            )

        issue_number = item_info.get("issue_number")
        content_type = item_info.get("content_type", "")

        # 4. Normalize provided field names, then optionally fill defaults.
        settings = get_settings()
        provided_canonical: dict[str, Any] = {}
        unknown_fields: list[str] = []
        for raw_name, value in params.fields.items():
            canonical = _normalize_field_name(raw_name, metadata)
            if canonical is None:
                unknown_fields.append(raw_name)
                continue
            provided_canonical[canonical] = value

        if unknown_fields:
            valid = _known_field_names(metadata)
            return build_error_response(
                error_type="validation",
                message=(
                    f"Unknown field(s): {', '.join(unknown_fields)}."
                ),
                suggestion=f"Valid fields: {', '.join(valid)}.",
            )

        # Board-level provided values feed the defaults computation; issue
        # fields (body/assignees/labels) pass through untouched.
        board_provided = {
            k: v for k, v in provided_canonical.items()
            if k.lower() not in _ISSUE_API_FIELDS
        }
        effective: dict[str, Any] = dict(provided_canonical)

        if params.apply_defaults:
            defaults = compute_defaults(
                metadata=metadata,
                settings=settings,
                provided=board_provided,
                labels=params.labels,
            )
            for name, value in defaults.items():
                effective.setdefault(name, value)

        # 5. Strict mode — error if any board field is still unset.
        enforce = settings.enforce_fields if params.enforce is None else params.enforce
        if enforce:
            board_effective = {
                k: v for k, v in effective.items()
                if k.lower() not in _ISSUE_API_FIELDS
            }
            missing = unset_board_fields(metadata, board_effective)
            if missing:
                return build_error_response(
                    error_type="validation",
                    message=(
                        "Strict field enforcement: these board fields are "
                        f"still unset: {', '.join(missing)}."
                    ),
                    suggestion=(
                        "Provide values for them, or set apply_defaults=true "
                        "so defaults are filled in."
                    ),
                )

        if not effective:
            return build_error_response(
                error_type="validation",
                message="No fields to update.",
                suggestion="Provide at least one field, or apply_defaults=true.",
            )

        # 6. Apply updates sequentially, collecting per-field results.
        results: list[dict[str, str]] = []

        for canonical_name, value in effective.items():
            outcome = await _apply_single_field_update(
                raw_field_name=canonical_name,
                value=value,
                item_id=item_id,
                issue_number=issue_number,
                content_type=content_type,
                metadata=metadata,
                project_service=project_service,
                issue_service=issue_service,
            )
            results.append(outcome)

        # 7. Determine response type based on outcomes.
        all_success = all(r["outcome"] == "success" for r in results)
        any_success = any(r["outcome"] == "success" for r in results)

        if all_success:
            return ToolSuccess(
                data={
                    "item_id": item_id,
                    "results": results,
                    "status": "all_fields_updated",
                },
            ).model_dump()
        elif any_success:
            # Partial success: some fields updated, some failed.
            return ToolSuccess(
                data={
                    "item_id": item_id,
                    "results": results,
                    "status": "partial_success",
                },
            ).model_dump()
        else:
            # All updates failed.
            return build_error_response(
                error_type="validation",
                message="All field updates failed.",
                suggestion=(
                    "Check the field names and values. Run discover_ids "
                    "to see valid field options."
                ),
            )

    except Exception as exc:
        logger.error("Error in update_project_item_fields: %s", exc)
        return handle_tool_error(exc, context="Update fields failed")


async def _resolve_item(
    graphql_client: GraphQLExecutor,
    item_id: str,
) -> dict | None:
    """Resolve a project item by node ID to verify existence and get issue_number.

    Queries the GitHub GraphQL API using the GET_ITEM_STATUS_QUERY to
    retrieve the item's content type and issue number.

    Args:
        graphql_client: The GraphQL client instance.
        item_id: The project item node ID to look up.

    Returns:
        A dict with 'issue_number' (int or None) and 'content_type' (str)
        if the item exists, or None if not found.
    """
    try:
        response = await graphql_client.execute_with_retry(
            GET_ITEM_STATUS_QUERY,
            {"itemId": item_id},
            is_mutation=False,
        )
    except GraphQLError as exc:
        # If the query itself fails with a NOT_FOUND type error, treat as missing.
        error_types = [
            e.get("type", "").upper()
            for e in getattr(exc, "errors", [])
        ]
        if "NOT_FOUND" in error_types:
            return None
        raise

    node = response.get("data", {}).get("node")
    if node is None:
        return None

    content = node.get("content")
    if content is None:
        return None

    content_type = content.get("__typename", "")
    issue_number: int | None = None

    if content_type == "Issue":
        issue_number = content.get("number")

    return {
        "issue_number": issue_number,
        "content_type": content_type,
    }


async def _apply_single_field_update(
    *,
    raw_field_name: str,
    value: Any,
    item_id: str,
    issue_number: int | None,
    content_type: str,
    metadata: ProjectMetadata,
    project_service: ProjectService,
    issue_service: IssueService,
) -> dict[str, str]:
    """Apply a single field update and return the outcome.

    Routes the update to the appropriate service based on the field type:
    - Project-level fields (Status, Priority, Milestone, Due date) use
      ProjectService.update_field() via GraphQL mutation.
    - Issue-level fields (body, assignees, labels) use IssueService.update()
      via gh issue edit CLI command.

    Args:
        raw_field_name: The field name as provided by the user.
        value: The new value for the field.
        item_id: The project item node ID.
        issue_number: The issue number (None for DraftIssues).
        content_type: The content type ("Issue" or "DraftIssue").
        metadata: Discovered project metadata.
        project_service: Service for project field mutations.
        issue_service: Service for issue-level updates.

    Returns:
        A dict with keys: "field", "outcome" ("success" or "failure"),
        and either "value" (on success) or "reason" (on failure).
    """
    # Normalize field name to canonical form (metadata-aware).
    canonical_name = _normalize_field_name(raw_field_name, metadata)

    if canonical_name is None:
        return {
            "field": raw_field_name,
            "outcome": "failure",
            "reason": (
                f"Unknown field '{raw_field_name}'. "
                f"Valid fields: {', '.join(_known_field_names(metadata))}"
            ),
        }

    # Route to the appropriate update mechanism.
    if canonical_name.lower() in _ISSUE_API_FIELDS:
        return await _update_issue_field(
            canonical_name=canonical_name,
            value=value,
            issue_number=issue_number,
            content_type=content_type,
            issue_service=issue_service,
        )
    else:
        return await _update_project_field(
            canonical_name=canonical_name,
            value=value,
            item_id=item_id,
            metadata=metadata,
            project_service=project_service,
        )


async def _update_project_field(
    *,
    canonical_name: str,
    value: Any,
    item_id: str,
    metadata: ProjectMetadata,
    project_service: ProjectService,
) -> dict[str, str]:
    """Update a project-level field via GraphQL mutation.

    Args:
        canonical_name: The canonical field name (e.g., "Status", "Due date").
        value: The new value for the field.
        item_id: The project item node ID.
        metadata: Discovered project metadata.
        project_service: Service for project field mutations.

    Returns:
        Per-field outcome dict.
    """
    # Validate due_date format if applicable.
    if canonical_name == "Due date":
        if not _is_valid_date(str(value)):
            return {
                "field": canonical_name,
                "outcome": "failure",
                "reason": (
                    f"Invalid date format: '{value}'. "
                    "Expected ISO 8601 date format: YYYY-MM-DD"
                ),
            }

    try:
        await project_service.update_field(
            metadata, item_id, canonical_name, value
        )
        return {
            "field": canonical_name,
            "outcome": "success",
            "value": str(value),
        }
    except ValidationError as exc:
        return {
            "field": canonical_name,
            "outcome": "failure",
            "reason": str(exc),
        }
    except GitHubProjectError as exc:
        return {
            "field": canonical_name,
            "outcome": "failure",
            "reason": str(exc),
        }
    except Exception as exc:
        logger.warning(
            "Unexpected error updating field '%s' on item %s: %s",
            canonical_name,
            item_id,
            exc,
        )
        return {
            "field": canonical_name,
            "outcome": "failure",
            "reason": f"Unexpected error: {exc}",
        }


async def _update_issue_field(
    *,
    canonical_name: str,
    value: Any,
    issue_number: int | None,
    content_type: str,
    issue_service: IssueService,
) -> dict[str, str]:
    """Update an issue-level field via gh issue edit.

    Args:
        canonical_name: The canonical field name (body, assignees, labels).
        value: The new value for the field.
        issue_number: The issue number (None for DraftIssues).
        content_type: The content type ("Issue" or "DraftIssue").
        issue_service: Service for issue-level updates.

    Returns:
        Per-field outcome dict.
    """
    # DraftIssues don't have underlying issues — can't update via Issues API.
    if content_type == "DraftIssue" or issue_number is None:
        return {
            "field": canonical_name,
            "outcome": "failure",
            "reason": (
                f"Cannot update '{canonical_name}' on a DraftIssue. "
                "Convert it to a full Issue first."
            ),
        }

    # Validate value types.
    validation_error = _validate_issue_field_value(canonical_name, value)
    if validation_error is not None:
        return validation_error

    try:
        if canonical_name == "body":
            await issue_service.update(issue_number, body=str(value))
            return {
                "field": canonical_name,
                "outcome": "success",
                "value": str(value),
            }

        elif canonical_name == "assignees":
            assignee_list = _coerce_to_string_list(value)
            await issue_service.update(issue_number, assignees=assignee_list)
            return {
                "field": canonical_name,
                "outcome": "success",
                "value": ", ".join(assignee_list) if assignee_list else "(none)",
            }

        elif canonical_name == "labels":
            label_list = _coerce_to_string_list(value)
            await issue_service.update(issue_number, labels=label_list)
            return {
                "field": canonical_name,
                "outcome": "success",
                "value": ", ".join(label_list) if label_list else "(none)",
            }

        else:
            return {
                "field": canonical_name,
                "outcome": "failure",
                "reason": f"Unsupported issue field: '{canonical_name}'",
            }

    except CLIError as exc:
        return {
            "field": canonical_name,
            "outcome": "failure",
            "reason": f"CLI error: {exc}",
        }
    except Exception as exc:
        logger.warning(
            "Unexpected error updating issue field '%s' on #%d: %s",
            canonical_name,
            issue_number,
            exc,
        )
        return {
            "field": canonical_name,
            "outcome": "failure",
            "reason": f"Unexpected error: {exc}",
        }


def _normalize_field_name(
    raw_name: str,
    metadata: ProjectMetadata | None = None,
) -> str | None:
    """Normalize a user-provided field name to its canonical board name.

    Resolution order:
    1. Well-known aliases (``due_date`` → ``Due date``, ``work_type`` →
       ``Work Type``, issue fields body/assignees/labels).
    2. Case-insensitive match against any field actually on the board
       (so custom single-selects / NUMBER / DATE fields resolve without a
       hardcoded whitelist — issue #14).

    Args:
        raw_name: The field name as provided by the user (or already
            canonical, in which case it round-trips).
        metadata: Discovered project metadata; when provided, board field
            names are matched dynamically.

    Returns:
        The canonical field name, or None if unrecognized.
    """
    lower_name = raw_name.lower().strip()

    if lower_name in _FIELD_NAME_MAP:
        return _FIELD_NAME_MAP[lower_name]

    # Dynamic match against discovered board fields (case-insensitive and
    # tolerant of a unique leading emoji/symbol prefix).
    if metadata is not None:
        return resolve_field_name(metadata, raw_name)

    return None


def _known_field_names(metadata: ProjectMetadata) -> list[str]:
    """Return the set of accepted field names for error messages."""
    names = set(metadata.fields.keys())
    names.update({"body", "assignees", "labels"})
    return sorted(names)


def _validate_issue_field_value(
    field_name: str,
    value: Any,
) -> dict[str, str] | None:
    """Validate the value type for an issue-level field.

    Args:
        field_name: The canonical field name.
        value: The value to validate.

    Returns:
        A failure outcome dict if validation fails, None if valid.
    """
    if field_name == "body":
        if not isinstance(value, str):
            return {
                "field": field_name,
                "outcome": "failure",
                "reason": (
                    f"Invalid value type for 'body': expected string, "
                    f"got {type(value).__name__}"
                ),
            }
        if len(value) > 65536:
            return {
                "field": field_name,
                "outcome": "failure",
                "reason": (
                    "Body exceeds maximum length of 65536 characters."
                ),
            }

    elif field_name in ("assignees", "labels"):
        if not isinstance(value, list):
            return {
                "field": field_name,
                "outcome": "failure",
                "reason": (
                    f"Invalid value type for '{field_name}': expected list "
                    f"of strings, got {type(value).__name__}"
                ),
            }
        for item in value:
            if not isinstance(item, str):
                return {
                    "field": field_name,
                    "outcome": "failure",
                    "reason": (
                        f"Invalid item in '{field_name}': expected string, "
                        f"got {type(item).__name__}"
                    ),
                }

    return None


def _coerce_to_string_list(value: Any) -> list[str]:
    """Coerce a value to a list of strings.

    Handles both list inputs and comma-separated string inputs.

    Args:
        value: The value to coerce.

    Returns:
        A list of strings.
    """
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return []


def _is_valid_date(date_str: str) -> bool:
    """Check if a string is a valid ISO 8601 date (YYYY-MM-DD).

    Args:
        date_str: The string to validate.

    Returns:
        True if the string represents a valid calendar date.
    """
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_str):
        return False

    try:
        date.fromisoformat(date_str)
        return True
    except ValueError:
        return False
