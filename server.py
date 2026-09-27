"""MCP Server entry point for GitHub Project Management.

Creates a FastMCP server instance, registers all tools, validates
authentication on startup, and runs with stdio transport for MCP
communication.

Usage:
    python -m app.mcp.github_project.server
"""

from __future__ import annotations

import asyncio
import logging
import sys

from fastmcp import FastMCP

from core.auth import resolve_token, validate_scopes
from core.access import AccessLevel, is_exposed
from core.config import get_settings
from core.context import RequestContextFilter, with_request_context
from tools.projects.archive import (
    archive_project_item,
    move_to_done,
    move_to_trash,
)
from tools.projects.add_item import add_item_to_project
from tools.issues.close import close_issue
from tools.issues.comment_issue import comment_issue
from tools.issues.add_sub_issue import add_sub_issue
from tools.issues.edit_issue import edit_issue
from tools.issues.create_item import create_project_item
from tools.discovery.discover import discover_ids
from tools.fields.estimate import set_estimate
from tools.discovery.list_items import list_project_items
from tools.projects.update_fields import update_project_item_fields
from tools.fields.milestones import create_milestone, close_milestone, list_milestones
from tools.fields.labels import create_label, list_labels
from tools.bulk.bulk_operations import bulk_close_issues, search_issues
from tools.issues.advanced_operations import (
    move_to_status,
    bulk_update_items,
    get_issue_detail,
    list_sub_issues,
    remove_sub_issue,
    reopen_issue,
)
from tools.meta.nice_to_have import (
    get_project_stats,
    get_sprint_summary,
    link_pull_request,
    create_pull_request,
    bulk_assign,
)
from tools.repositories import create_repository
from tools.planning.planning import (
    sprint_planning,
    generate_release_notes,
)
from tools.planning.workflows import (
    complete_issue as complete_issue_workflow,
    daily_standup,
    sprint_review,
    triage_new_issues,
    escalate_overdue,
    handoff_issue,
    create_epic,
    close_sprint,
    blocked_report,
)
from tools.pull_requests.pr_issue_lifecycle import (
    verify_acceptance_criteria,
    get_pr_linked_issues,
    validate_issue_closure_readiness,
    close_issue_on_pr_merge,
    sync_closed_items_to_done,
)
from tools.meta import capability_suite
from tools.ci.actions import CI_TOOLS
from tools.deletes import (
    delete_project_item,
    delete_issue,
    delete_issue_comment,
    delete_label,
    delete_milestone,
)
from tools.meta.server_info import server_info
from tools.project_provisioning import (
    create_project,
    update_project,
    create_project_field,
    link_repository,
    list_projects,
)

# ── FastMCP Server Instance ──────────────────────────────────────────────────

mcp = FastMCP("github-project-management")

# ── Register Tools ───────────────────────────────────────────────────────────

# In FastMCP 2.14+, use mcp.tool() as a decorator wrapper for pre-defined functions.
#
# Registration is GATED by MCP_ACCESS_LEVEL (issue #34): every tool is
# classified read/write/delete in core.access (single source of truth), and
# only tools exposed at the configured level are registered — a hidden write or
# delete tool is not even visible to the client. read < write (DEFAULT) < full.
# The permanent-delete tools appear only at 'full'.

_ALL_TOOLS: list = [
    # Core operational subset (explicit functions).
    discover_ids,
    list_project_items,
    create_project_item,
    update_project_item_fields,
    add_item_to_project,
    set_estimate,
    archive_project_item,
    move_to_done,
    move_to_trash,
    close_issue,
    comment_issue,
    add_sub_issue,
    edit_issue,
    create_milestone,
    close_milestone,
    list_milestones,
    create_label,
    list_labels,
    bulk_close_issues,
    search_issues,
    move_to_status,
    bulk_update_items,
    get_issue_detail,
    list_sub_issues,
    remove_sub_issue,
    reopen_issue,
    get_project_stats,
    get_sprint_summary,
    link_pull_request,
    create_pull_request,
    create_repository,
    bulk_assign,
    sprint_planning,
    generate_release_notes,
    complete_issue_workflow,
    daily_standup,
    sprint_review,
    triage_new_issues,
    escalate_overdue,
    handoff_issue,
    create_epic,
    close_sprint,
    blocked_report,
    verify_acceptance_criteria,
    get_pr_linked_issues,
    validate_issue_closure_readiness,
    close_issue_on_pr_merge,
    sync_closed_items_to_done,
    create_project,
    update_project,
    create_project_field,
    link_repository,
    list_projects,
    # Diagnostics (issue #34).
    server_info,
    # Permanent-delete tools (issue #34) — only registered at 'full'.
    delete_project_item,
    delete_issue,
    delete_issue_comment,
    delete_label,
    delete_milestone,
]

# GitHub Actions workflows / runs / checks (issue #32).
_ALL_TOOLS.extend(CI_TOOLS)

# Extend with the 60-tool capability suite (dynamically-defined functions).
_ALL_TOOLS.extend(
    getattr(capability_suite, _name) for _name in capability_suite.CAPABILITY_TOOL_NAMES
)


def _register_tools() -> dict[str, int]:
    """Register every tool exposed at the configured access level.

    Returns a small tally (registered / hidden / total) for the startup log.
    ``complete_issue_workflow`` registers under its MCP name ``complete_issue``,
    which is how it is classified in core.access.
    """
    level = AccessLevel.parse(get_settings().access_level)
    registered = 0
    hidden = 0
    for _fn in _ALL_TOOLS:
        # FastMCP registers a tool under the function's __name__; the workflow
        # complete_issue is imported under an alias, so resolve its real name.
        tool_name = "complete_issue" if _fn is complete_issue_workflow else _fn.__name__
        if is_exposed(tool_name, level):
            # Each call runs in its own RequestContext (correlation ID in logs
            # and error envelopes). functools.wraps keeps name + schema intact.
            mcp.tool()(with_request_context(_fn, tool_name))
            registered += 1
        else:
            hidden += 1
    return {"level": level.value, "registered": registered, "hidden": hidden}


_REGISTRATION = _register_tools()


# ── Startup Authentication ───────────────────────────────────────────────────


async def _validate_auth_on_startup() -> None:
    """Validate authentication within the configured timeout.

    Resolves the token and verifies it's valid by making a test API call.
    Scope validation is skipped for fine-grained tokens (github_pat_*)
    since they don't return X-OAuth-Scopes headers.
    """
    settings = get_settings()
    timeout = settings.timeout_seconds

    try:
        token = await asyncio.wait_for(
            resolve_token(),
            timeout=timeout,
        )
        # Skip scope validation for fine-grained PATs (github_pat_*)
        # They don't support X-OAuth-Scopes header but still have permissions
        if not token.startswith("github_pat_"):
            await asyncio.wait_for(
                validate_scopes(token),
                timeout=timeout,
            )
    except asyncio.TimeoutError:
        print(
            f"Error: Authentication validation timed out after "
            f"{timeout} seconds.\n"
            "Could not verify token and scopes within the time limit.",
            file=sys.stderr,
        )
        sys.exit(1)
    except SystemExit:
        raise
    except Exception as exc:
        print(
            f"Error: Authentication validation failed: {exc}\n"
            "Verify that GITHUB_TOKEN or GH_TOKEN is set with "
            "repo, project, and read:org scopes.",
            file=sys.stderr,
        )
        sys.exit(1)

    print(
        "github-project-management MCP server ready. "
        "Authentication validated successfully. "
        f"Access level: {_REGISTRATION['level']} "
        f"({_REGISTRATION['registered']} tools registered, "
        f"{_REGISTRATION['hidden']} hidden). "
        f"Scope lock: {'on' if get_settings().scope_lock else 'off'}.",
        file=sys.stderr,
    )


# ── Main Entry Point ─────────────────────────────────────────────────────────


def _configure_logging() -> None:
    """Send logs to stderr (stdout is the MCP protocol), tagged per request."""
    handler = logging.StreamHandler(sys.stderr)
    handler.addFilter(RequestContextFilter())
    handler.setFormatter(
        logging.Formatter("%(levelname)s [%(correlation_id)s %(tool)s] %(name)s: %(message)s")
    )
    root = logging.getLogger()
    root.addHandler(handler)
    if root.level == logging.NOTSET or root.level > logging.WARNING:
        root.setLevel(logging.WARNING)


def main() -> None:
    """Run the MCP server with stdio transport.

    1. Validates authentication (token + scopes) within timeout.
    2. Starts the FastMCP server on stdio transport.
    """
    _configure_logging()
    asyncio.run(_validate_auth_on_startup())
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
