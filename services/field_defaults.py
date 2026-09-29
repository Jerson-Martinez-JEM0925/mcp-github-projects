"""Board-field default computation and enforcement (issue #14).

Central place that decides what value a project board field gets when the
caller omits it, so an item never lands with empty custom fields. Also holds
the strict-mode check that reports fields still unset after defaults.

No I/O here — this module is pure logic over discovered
:class:`~models.metadata.ProjectMetadata` plus the configured defaults. The
tool layer resolves values here, then hands them to
:meth:`ProjectService.update_field` for the actual GraphQL write.

Security: never logs or echoes tokens; it only reads field metadata.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from core.config import GitHubProjectSettings
from models.metadata import ProjectMetadata
from services.field_names import resolve_field

logger = logging.getLogger(__name__)

# Board fields this module knows how to default, in a stable apply order.
# Status default = first option; the rest come from configured defaults.
DEFAULTABLE_FIELDS: tuple[str, ...] = (
    "Status",
    "Priority",
    "Area",
    "Estimate",
    "Due date",
    "Work Type",
)


def _first_option_name(metadata: ProjectMetadata, field_name: str) -> str | None:
    """Return the first option name of a single-select field, if present."""
    field = resolve_field(metadata, field_name)
    if field is None or not field.options:
        return None
    return field.options[0].name


def _option_exists(metadata: ProjectMetadata, field_name: str, value: str) -> bool:
    """Whether ``value`` is a valid option name for a single-select field."""
    field = resolve_field(metadata, field_name)
    if field is None or not field.options:
        return False
    return any(opt.name == value for opt in field.options)


def infer_work_type(labels: list[str] | None, settings: GitHubProjectSettings) -> str:
    """Infer the Work Type option from issue labels.

    Rule (issue #14): a ``bug`` label → ``Bug``; otherwise the configured
    default, falling back to ``Feature``.

    Args:
        labels: Issue label names (may be None).
        settings: Settings carrying ``default_work_type``.

    Returns:
        The Work Type option name to apply.
    """
    lowered = {lbl.lower() for lbl in (labels or [])}
    if "bug" in lowered:
        return "Bug"
    return settings.default_work_type or "Feature"


def compute_defaults(
    *,
    metadata: ProjectMetadata,
    settings: GitHubProjectSettings,
    provided: dict[str, Any],
    labels: list[str] | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """Compute the full set of board field values, filling omitted ones.

    Only fields that actually exist on the board are returned, and a
    single-select default is dropped when the configured option is not a
    valid choice (so we never send an invalid option id).

    Args:
        metadata: Discovered project metadata (field definitions/options).
        settings: Configured defaults.
        provided: Field values the caller explicitly supplied (canonical
            field names → value). These are never overridden.
        labels: Issue labels, used to infer Work Type.
        today: Injectable "today" for deterministic tests.

    Returns:
        A dict of canonical field name → value covering provided values plus
        computed defaults for omitted, board-present fields.
    """
    today = today or date.today()
    resolved: dict[str, Any] = {}

    # Start from explicitly-provided values (canonical names, non-None only).
    for name, value in provided.items():
        if value is not None:
            resolved[name] = value

    def _needs(field_name: str) -> bool:
        return resolve_field(metadata, field_name) is not None and field_name not in resolved

    # Status → first option.
    if _needs("Status"):
        first = _first_option_name(metadata, "Status")
        if first is not None:
            resolved["Status"] = first

    # Priority → configured default if it is a valid option, else first.
    if _needs("Priority"):
        if _option_exists(metadata, "Priority", settings.default_priority):
            resolved["Priority"] = settings.default_priority
        else:
            first = _first_option_name(metadata, "Priority")
            if first is not None:
                resolved["Priority"] = first

    # Area → configured default if set and valid.
    if _needs("Area") and settings.default_area:
        if _option_exists(metadata, "Area", settings.default_area):
            resolved["Area"] = settings.default_area

    # Estimate (NUMBER) → configured default.
    if _needs("Estimate"):
        resolved["Estimate"] = settings.default_estimate

    # Due date (DATE) → today + configured days.
    if _needs("Due date"):
        due = today + timedelta(days=settings.default_due_days)
        resolved["Due date"] = due.isoformat()

    # Work Type → inferred from labels (falls back to configured/Feature).
    if _needs("Work Type"):
        inferred = infer_work_type(labels, settings)
        if _option_exists(metadata, "Work Type", inferred):
            resolved["Work Type"] = inferred
        else:
            first = _first_option_name(metadata, "Work Type")
            if first is not None:
                resolved["Work Type"] = first

    return resolved


def unset_board_fields(
    metadata: ProjectMetadata,
    resolved: dict[str, Any],
) -> list[str]:
    """List defaultable board fields still unset after defaults were applied.

    Used by strict mode (``GH_PROJECT_ENFORCE_FIELDS``). Only fields that
    exist on the board are considered.

    Args:
        metadata: Discovered project metadata.
        resolved: Field name → value that will be written.

    Returns:
        Sorted list of board field names present on the board but absent
        from ``resolved``.
    """
    missing = [
        name
        for name in DEFAULTABLE_FIELDS
        if resolve_field(metadata, name) is not None and name not in resolved
    ]
    return sorted(missing)
