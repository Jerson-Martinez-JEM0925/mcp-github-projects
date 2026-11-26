"""Project-board target selection for multi-project MCP instances."""

from __future__ import annotations

from core.access import enforce_scope
from core.config import get_settings
from core.exceptions import ScopeLockError
from models.context import GitHubContext


def configured_project_numbers() -> tuple[int, ...]:
    """Return the configured project allowlist with the legacy default first."""
    return get_settings().configured_project_numbers()


def resolve_project_number(project_number: int | None = None) -> int:
    """Resolve and validate one project board for a tool call.

    Omitting ``project_number`` preserves the legacy configured default. When
    ``GH_PROJECT_PROJECT_NUMBERS`` is set, an explicit selector must belong to
    that allowlist. The existing scope lock remains authoritative for callers
    that want a single-board fence.
    """
    settings = get_settings()
    selected = settings.project_number if project_number is None else int(project_number)
    if selected < 1 or selected > 100_000:
        raise ValueError("project_number must be between 1 and 100000")

    allowed = configured_project_numbers()
    if selected not in allowed:
        raise ScopeLockError(
            f"project {selected} is not in the configured project allowlist "
            f"{list(allowed)}. Set GH_PROJECT_PROJECT_NUMBERS to include it."
        )

    enforce_scope(project_number=selected)
    return selected


def project_context(project_number: int | None = None) -> GitHubContext:
    """Build a request context for one validated project board."""
    selected = resolve_project_number(project_number)
    return GitHubContext.from_settings(project_number=selected)


__all__ = ["configured_project_numbers", "project_context", "resolve_project_number"]
