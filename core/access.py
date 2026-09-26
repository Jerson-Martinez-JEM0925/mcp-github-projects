"""Access-level and scope-lock enforcement — the single source of truth.

This module classifies every registered MCP tool into one of three access
tiers and exposes the machinery that the server uses to decide which tools to
register for a given ``MCP_ACCESS_LEVEL`` and to fence every mutation to the
configured target when ``GH_PROJECT_SCOPE_LOCK`` is on.

Three access levels (parity with mcp-monday-projects, richer 3-tier model):

    read   — only read-only tools are exposed.
    write  — today's operational surface: create / update / close / archive,
             but NO permanent deletes (this is the DEFAULT, nothing removed).
    full   — everything, including the permanent-delete tools.

Tool classification (``ToolAccess``):

    READ    — no mutation reaches GitHub.
    WRITE   — a reversible / recoverable mutation (create, update, close,
              archive, move). Exposed at ``write`` and ``full``.
    DELETE  — a PERMANENT delete with no GitHub-side recovery. Exposed ONLY
              at ``full`` and each such tool additionally requires
              ``confirm: true``.

Scope lock (parity with ``MONDAY_WORKSPACE_ID``):

    When ``GH_PROJECT_SCOPE_LOCK=true`` every tool is confined to the
    configured ``GH_PROJECT_ORG_NAME`` / ``GH_PROJECT_REPO_NAME`` /
    ``GH_PROJECT_PROJECT_NUMBER``. A tool that accepts an owner / repo /
    project override targeting anything else is refused with a typed
    :class:`~core.exceptions.ScopeLockError` — naming the variable — BEFORE
    any mutation is built. When the lock is off the server stays as wide as
    the token allows (backward compatible).
"""

from __future__ import annotations

from enum import Enum

from core.exceptions import ScopeLockError

# ── Access levels ────────────────────────────────────────────────────────────


class AccessLevel(str, Enum):
    """The three server-wide access levels (``MCP_ACCESS_LEVEL``)."""

    READ = "read"
    WRITE = "write"
    FULL = "full"

    @classmethod
    def parse(cls, value: str | None) -> "AccessLevel":
        """Parse a level string; default to WRITE for empty/unknown input.

        Empty falls back to the documented default (``write``). An explicitly
        invalid value raises ``ValueError`` so the server fails loudly at
        startup rather than silently widening or narrowing access.
        """
        if value is None or not str(value).strip():
            return cls.WRITE
        normalized = str(value).strip().lower()
        for level in cls:
            if level.value == normalized:
                return level
        raise ValueError(
            f"MCP_ACCESS_LEVEL must be 'read', 'write', or 'full' "
            f"(got '{value}')."
        )


class ToolAccess(str, Enum):
    """The access tier a single tool requires."""

    READ = "read"
    WRITE = "write"
    DELETE = "delete"


# Rank used to decide exposure: a tool is exposed when its tier rank is <= the
# server level's rank.
_LEVEL_RANK: dict[AccessLevel, int] = {
    AccessLevel.READ: 0,
    AccessLevel.WRITE: 1,
    AccessLevel.FULL: 2,
}
_TOOL_RANK: dict[ToolAccess, int] = {
    ToolAccess.READ: 0,
    ToolAccess.WRITE: 1,
    ToolAccess.DELETE: 2,
}


# ── Tool → access-tier classification (single source of truth) ───────────────
# Read/write classification is DERIVED from capabilities.TOOL_CAPABILITIES so
# there is exactly one place that says whether a tool mutates: a tool whose
# required capability set contains any ``*.write`` capability is WRITE, else
# READ. This table only records the two things capabilities.py cannot express:
#   1. the DELETE tier — permanent, unrecoverable deletes (issue #34); and
#   2. tools that are not in the capability map at all (e.g. server_info).
# tests/test_access_levels asserts EVERY registered tool resolves to a tier, so
# a new tool added without a capability entry AND without an override fails CI
# rather than defaulting silently.

from core.capabilities import TOOL_CAPABILITIES as _TOOL_CAPABILITIES

_ACCESS_OVERRIDES: dict[str, ToolAccess] = {
    # Permanent-delete tools (exposed only at 'full', each needs confirm=true).
    "delete_project_item": ToolAccess.DELETE,
    "delete_issue": ToolAccess.DELETE,
    "delete_issue_comment": ToolAccess.DELETE,
    "delete_label": ToolAccess.DELETE,
    "delete_milestone": ToolAccess.DELETE,
    # Diagnostics tool — read-only, not in the capability map.
    "server_info": ToolAccess.READ,
}


def _derive_from_capabilities(tool_name: str) -> ToolAccess | None:
    """Classify a tool as READ/WRITE from its capability requirements.

    Returns WRITE when the tool requires any ``*.write`` capability, READ when
    it is in the capability map with only read (or no) capabilities, and None
    when the tool is not in the capability map at all.
    """
    required = _TOOL_CAPABILITIES.get(tool_name)
    if required is None:
        return None
    if any(cap.value.endswith(".write") for cap in required):
        return ToolAccess.WRITE
    return ToolAccess.READ


def classify(tool_name: str) -> ToolAccess:
    """Return a tool's access tier from the single classification pipeline.

    Order: explicit override (deletes / server_info) → derived from
    capabilities. Raises KeyError if the tool cannot be classified either way,
    which is exactly the condition the sync test guards against.
    """
    if tool_name in _ACCESS_OVERRIDES:
        return _ACCESS_OVERRIDES[tool_name]
    derived = _derive_from_capabilities(tool_name)
    if derived is None:
        raise KeyError(
            f"tool '{tool_name}' has no access classification: add it to "
            "capabilities.TOOL_CAPABILITIES or to _ACCESS_OVERRIDES in "
            "core/access.py."
        )
    return derived


def all_classified_tools() -> list[str]:
    """Every tool name that has a classification (capability map + overrides)."""
    names = set(_TOOL_CAPABILITIES) | set(_ACCESS_OVERRIDES)
    return sorted(names)


def tool_access(tool_name: str) -> ToolAccess:
    """Return a tool's access tier. Raises KeyError for an unclassified tool."""
    return classify(tool_name)


def is_exposed(tool_name: str, level: AccessLevel) -> bool:
    """Whether ``tool_name`` is exposed at server ``level``.

    A tool is exposed when its tier rank is at or below the level rank:
    read exposes read-only tools; write adds write tools; full adds deletes.
    """
    return _TOOL_RANK[classify(tool_name)] <= _LEVEL_RANK[level]


def tools_for_level(level: AccessLevel) -> list[str]:
    """Return every classified tool name exposed at ``level`` (sorted)."""
    return sorted(
        name for name in all_classified_tools() if is_exposed(name, level)
    )


def level_counts(level: AccessLevel) -> dict[str, int]:
    """Return exposed/hidden tallies for diagnostics at ``level``."""
    classified = all_classified_tools()
    exposed = set(tools_for_level(level))
    hidden = [name for name in classified if name not in exposed]
    delete_hidden = [
        name for name in hidden if classify(name) is ToolAccess.DELETE
    ]
    return {
        "classified_total": len(classified),
        "exposed": len(exposed),
        "hidden": len(hidden),
        "hidden_delete_tools": len(delete_hidden),
    }


# ── Scope lock (parity with MONDAY_WORKSPACE_ID) ─────────────────────────────

_SCOPE_VARIABLE = "GH_PROJECT_SCOPE_LOCK"


def _norm(value: object) -> str:
    """Case-insensitive, whitespace-trimmed comparison key for logins/repos."""
    return str(value).strip().lower()


def enforce_scope(
    *,
    owner: str | None = None,
    repo: str | None = None,
    project_number: int | None = None,
) -> None:
    """Refuse any target outside the configured scope when the lock is on.

    Called by every tool that accepts an owner / repo / project OVERRIDE,
    BEFORE the mutation is built. A ``None`` argument means "the tool will use
    the configured default" and is always allowed. When the lock is off this
    is a no-op, so unscoped servers keep their current behaviour.

    Raises:
        ScopeLockError: when an argument names a target other than the
            configured one. The message names ``GH_PROJECT_SCOPE_LOCK`` and
            the offending field so the caller knows exactly what to change.
    """
    from core.config import get_settings

    settings = get_settings()
    if not getattr(settings, "scope_lock", False):
        return

    if owner is not None and _norm(owner) != _norm(settings.org_name):
        raise ScopeLockError(
            f"owner '{owner}' is outside the locked scope "
            f"'{settings.org_name}'. {_SCOPE_VARIABLE}=true confines this "
            f"server to {settings.org_name}/{settings.repo_name} "
            f"(project {settings.project_number}). Set {_SCOPE_VARIABLE}=false "
            "to target other owners, or omit the override to use the "
            "configured owner."
        )
    if repo is not None and _norm(repo) != _norm(settings.repo_name):
        raise ScopeLockError(
            f"repository '{repo}' is outside the locked scope "
            f"'{settings.repo_name}'. {_SCOPE_VARIABLE}=true confines this "
            f"server to {settings.org_name}/{settings.repo_name}. Set "
            f"{_SCOPE_VARIABLE}=false to target other repositories, or omit "
            "the override to use the configured repository."
        )
    if project_number is not None and int(project_number) != int(
        settings.project_number
    ):
        raise ScopeLockError(
            f"project {project_number} is outside the locked scope "
            f"(project {settings.project_number}). {_SCOPE_VARIABLE}=true "
            f"confines this server to project {settings.project_number}. Set "
            f"{_SCOPE_VARIABLE}=false to target other projects, or omit the "
            "override to use the configured project."
        )
