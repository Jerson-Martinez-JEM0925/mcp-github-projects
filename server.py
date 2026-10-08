"""MCP Server entry point for GitHub Project Management.

Creates a FastMCP server instance, registers all tools, validates
authentication on startup, and runs with stdio transport for MCP
communication.

Usage (the container's default command):
    python server.py
    python .          # same, via __main__.py
"""

from __future__ import annotations

import asyncio
import logging
import sys

import uvicorn
from fastmcp import FastMCP
from fastmcp.tools.function_tool import FunctionTool
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from core.arguments import accept_flat_or_wrapped
from core.auth import resolve_token, validate_scopes
from core.access import AccessLevel, ToolAccess, classify, is_exposed
from core.config import get_settings, load_settings_or_exit
from core.context import RequestContextFilter, with_request_context
from core.http_auth import RequestBearerAuthMiddleware
from core.version import VERSION
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
    update_pull_request,
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
from tools.quality import QUALITY_TOOLS
from tools.projects.board_structure import BOARD_STRUCTURE_TOOLS
from tools.deletes import (
    delete_project_item,
    delete_issue,
    delete_issue_comment,
    delete_label,
    delete_milestone,
)
from tools.meta.server_info import server_info
from tools.pull_requests.listing import PULL_REQUEST_LISTING_TOOLS
from tools.repository_content import REPOSITORY_CONTENT_TOOLS
from tools.project_provisioning import (
    create_project,
    update_project,
    create_project_field,
    link_repository,
    list_projects,
)

# ── FastMCP Server Instance ──────────────────────────────────────────────────

# Sent in the MCP initialize result; clients such as LibreChat
# (`serverInstructions: true`) inject it into the model's system prompt so the
# model calls tools instead of answering from memory. MCP_SERVER_INSTRUCTIONS
# replaces it (parity with mcp-monday-projects).
DEFAULT_INSTRUCTIONS = """\
This server is the source of truth for the configured GitHub repository and its Projects V2 board.
ALWAYS call tools to answer questions about the repository, its documentation, policies, standards, ADRs, issues, pull requests, milestones, labels or the board; never answer from memory or invent repository data.
For "what does the documentation/governance say" questions: find files with search_repository_code or list_repository_directory(recursive=true, name_contains=...), read them with get_repository_file, and cite the returned URLs. Say clearly when the repository does not cover the topic.
For lists use paginated_issue_page or list_pull_requests (state=merged for merged PRs) and report total_count/has_more; say when results are truncated.
Prefer read tools. Before any write, describe the change and perform it only when the user asked for it. Files change only through create_branch + commit_files on an allowed branch followed by create_pull_request; never the default branch.
If a tool is missing, refused or fails, say so instead of guessing.
"""


def _instructions() -> str:
    custom = get_settings().server_instructions.strip()
    return custom or DEFAULT_INSTRUCTIONS


mcp = FastMCP("github-project-management", version=VERSION)

# ── Register Tools ───────────────────────────────────────────────────────────


async def _healthz(_request: Request) -> JSONResponse:
    """Return liveness without consulting configuration or external services."""
    return JSONResponse({"status": "ok"})


async def _readyz(_request: Request) -> JSONResponse:
    """Return readiness after validated settings have loaded."""
    return JSONResponse({"status": "ok"})


def create_http_app() -> Starlette:
    """Build the stateless Streamable HTTP ASGI app with health endpoints.

    The FastMCP-generated app owns its lifespan, including the stateless session
    manager. Health routes are inserted before the MCP route and never invoke
    authentication or GitHub, so probes remain local and dependency-free.
    """
    settings = get_settings()
    app = mcp.http_app(
        path=settings.http_path,
        transport="streamable-http",
        json_response=True,
        stateless_http=True,
    )
    if settings.auth_mode == "request":
        app.add_middleware(
            RequestBearerAuthMiddleware,
            protected_path=settings.http_path,
            client_key=settings.client_key,
            allowed_prefixes=settings.token_prefixes(),
        )
    app.routes.insert(0, Route("/readyz", endpoint=_readyz, methods=["GET"]))
    app.routes.insert(0, Route("/healthz", endpoint=_healthz, methods=["GET"]))
    return app


async def _serve_http() -> None:
    """Serve the configured stateless Streamable HTTP ASGI application."""
    settings = get_settings()
    app = create_http_app()
    config = uvicorn.Config(
        app,
        host=settings.http_host,
        port=settings.http_port,
        lifespan="on",
        ws="websockets-sansio",
        log_level="info",
    )
    await uvicorn.Server(config).serve()

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
    update_pull_request,
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

# Board structure: single-select options and views (issue #26).
_ALL_TOOLS.extend(BOARD_STRUCTURE_TOOLS)

# Response-quality and automation diagnostics.
_ALL_TOOLS.extend(QUALITY_TOOLS)

# Repository contents (read files/folders, code search, governed commits) and
# pull request listing/detail.
_ALL_TOOLS.extend(REPOSITORY_CONTENT_TOOLS)
_ALL_TOOLS.extend(PULL_REQUEST_LISTING_TOOLS)

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
    allowlist = get_settings().write_tool_allowlist_names()
    known = {_tool_name(fn) for fn in _ALL_TOOLS}
    unknown = sorted(allowlist - known)
    if unknown:
        raise ValueError(f"MCP_WRITE_TOOL_ALLOWLIST contains unknown tools: {', '.join(unknown)}")
    registered = 0
    hidden = 0
    for _fn in _ALL_TOOLS:
        tool_name = _tool_name(_fn)
        if is_exposed(tool_name, level) and _allowed_by_write_allowlist(tool_name, allowlist):
            # Each call runs in its own RequestContext (correlation ID in logs
            # and error envelopes), and accepts flat args or a legacy
            # {"params": {...}} wrapper on every tool (#4).
            mcp.add_tool(_build_tool(_fn, tool_name))
            registered += 1
        else:
            hidden += 1
    return {"level": level.value, "registered": registered, "hidden": hidden}


def _tool_name(fn) -> str:
    """FastMCP registers a tool under the function's __name__; the workflow
    complete_issue is imported under an alias, so resolve its real name."""
    return "complete_issue" if fn is complete_issue_workflow else fn.__name__


def _allowed_by_write_allowlist(tool_name: str, allowlist: frozenset[str]) -> bool:
    """MCP_WRITE_TOOL_ALLOWLIST: when set, only listed write/delete tools stay."""
    if not allowlist or classify(tool_name) is ToolAccess.READ:
        return True
    return tool_name in allowlist


def _build_tool(fn, tool_name: str) -> FunctionTool:
    """Wrap ``fn`` in the uniform argument convention and a request scope."""
    original = FunctionTool.from_function(fn, name=tool_name)
    normalized, schema = accept_flat_or_wrapped(fn, original.parameters, tool_name)
    tool = FunctionTool.from_function(
        with_request_context(normalized, tool_name),
        name=tool_name,
        description=original.description,
    )
    return tool.model_copy(update={"parameters": schema})


if __name__ == "__main__":
    # Run as `python server.py`: registration below reads settings at import
    # time, so validate them first and fail fast without a traceback (#3).
    load_settings_or_exit()

mcp.instructions = _instructions()
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
    """Validate authentication and run the configured MCP transport.

    Stdio remains the default and keeps the existing FastMCP runner. HTTP uses
    a stateless Streamable HTTP ASGI app with explicit health endpoints.
    """
    _configure_logging()
    settings = get_settings()
    if settings.auth_mode == "env":
        asyncio.run(_validate_auth_on_startup())
    if settings.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        asyncio.run(_serve_http())


if __name__ == "__main__":
    main()
