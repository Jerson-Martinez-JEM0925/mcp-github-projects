"""MCP tools for managing GitHub issue dependencies.

These tools use GitHub's REST issue-dependencies endpoints instead of text
heuristics. A dependency is expressed from the blocked issue's perspective:
``blocked_by_issue_number`` is the issue that must be completed first.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Literal

from pydantic import BaseModel, Field

from clients.gh_cli_client import CLIError
from core.access import enforce_scope
from core.config import get_settings
from core.error_handling import build_error_response, handle_tool_error
from core.factory import get_service_factory
from core.hardening import redact_sensitive
from models.responses import ToolSuccess

logger = logging.getLogger(__name__)

_API_VERSION = "2026-03-10"
_DIRECTIONS = ("blocked_by", "blocking")


class _RepoTarget(BaseModel):
    """Optional repository override shared by dependency tools."""

    owner: str | None = Field(
        default=None,
        min_length=1,
        description="Repository owner. Defaults to GH_PROJECT_ORG_NAME (scope-lock checked).",
    )
    repo: str | None = Field(
        default=None,
        min_length=1,
        description="Repository name. Defaults to GH_PROJECT_REPO_NAME (scope-lock checked).",
    )


class ListIssueDependenciesInput(_RepoTarget):
    """Input schema for listing issue dependencies."""

    issue_number: int = Field(ge=1, description="Issue number whose dependencies are listed.")
    direction: Literal["blocked_by", "blocking", "both"] = Field(
        default="both",
        description="Relationship direction to list: blocked_by, blocking, or both.",
    )


class IssueDependencyInput(_RepoTarget):
    """Input schema for adding or removing one issue dependency."""

    issue_number: int = Field(
        ge=1,
        description="Issue number that is blocked (the dependent issue).",
    )
    blocked_by_issue_number: int = Field(
        ge=1,
        description="Issue number that blocks issue_number.",
    )


def _repo_path(target: _RepoTarget) -> str:
    """Return the REST repository path after enforcing the optional scope lock."""
    enforce_scope(owner=target.owner, repo=target.repo)
    settings = get_settings()
    return f"repos/{target.owner or settings.org_name}/{target.repo or settings.repo_name}"


def _api_header_args() -> list[str]:
    """Return the GitHub REST API version header arguments."""
    return ["--header", f"X-GitHub-Api-Version: {_API_VERSION}"]


def _decode(stdout: str) -> Any:
    """Decode a JSON response, treating an empty response as an empty object."""
    return json.loads(stdout) if stdout.strip() else {}


async def _get(path: str) -> Any:
    """GET one GitHub REST resource through the configured GH client."""
    result = await get_service_factory().gh().run(["api", *_api_header_args(), path])
    return _decode(result.stdout)


async def _post_dependency(path: str, issue_id: int) -> None:
    """POST one blocked-by relationship using the blocking issue REST id."""
    await get_service_factory().gh().run(
        [
            "api",
            *_api_header_args(),
            "--method",
            "POST",
            path,
            "-F",
            f"issue_id={issue_id}",
        ]
    )


async def _delete_dependency(path: str) -> None:
    """DELETE one blocked-by relationship."""
    await get_service_factory().gh().run(
        ["api", *_api_header_args(), "--method", "DELETE", path]
    )


def _issue_summary(issue: dict[str, Any]) -> dict[str, Any]:
    """Keep the stable public dependency shape independent of API additions."""
    return {
        "number": issue.get("number"),
        "title": issue.get("title"),
        "state": issue.get("state"),
        "url": issue.get("html_url"),
    }


def _dependency_list(data: Any) -> list[dict[str, Any]]:
    """Normalize GitHub's issue array into bounded, stable summaries."""
    if not isinstance(data, list):
        return []
    return [_issue_summary(item) for item in data if isinstance(item, dict)]


def _contains_issue(items: list[dict[str, Any]], issue_number: int) -> bool:
    """Return whether an issue-number relationship is already present."""
    return any(item.get("number") == issue_number for item in items)


def _cli_error(tool: str, exc: CLIError, suggestion: str) -> dict:
    """Map common REST statuses to the server's structured error envelope."""
    stderr = redact_sensitive(exc.stderr.strip())
    lowered = stderr.lower()
    if "401" in lowered or "403" in lowered or "bad credentials" in lowered:
        error_type = "authentication"
    elif "404" in lowered or "not found" in lowered:
        error_type = "not_found"
    elif "422" in lowered or "validation failed" in lowered:
        error_type = "validation"
    else:
        error_type = "internal"
    logger.error("CLI error in %s: %s", tool, stderr)
    return build_error_response(
        error_type=error_type,
        message=f"{tool} failed: {stderr[:500]}",
        suggestion=suggestion,
    )


_READ_HINT = (
    "Verify the repository and issue numbers, and that the token can read issues "
    "in the target repository."
)
_WRITE_HINT = (
    "Verify the repository and issue numbers, and that the token can write issue "
    "relationships in the target repository."
)


async def list_issue_dependencies(params: ListIssueDependenciesInput) -> dict:
    """List issues that block or are blocked by one GitHub issue.

    Args:
        params: Issue number, relationship direction, and optional repository target.

    Returns:
        ToolSuccess containing normalized dependency summaries, or ToolError.
    """
    try:
        await get_service_factory().ensure_auth()
        base = _repo_path(params)
        directions = _DIRECTIONS if params.direction == "both" else (params.direction,)
        by_direction: dict[str, list[dict[str, Any]]] = {}
        for direction in directions:
            data = await _get(
                f"{base}/issues/{params.issue_number}/dependencies/{direction}"
            )
            by_direction[direction] = _dependency_list(data)
        dependencies = [
            item
            for direction in directions
            for item in by_direction[direction]
        ]
        return ToolSuccess(
            data={
                "issue_number": params.issue_number,
                "direction": params.direction,
                "blocked_by": by_direction.get("blocked_by", []),
                "blocking": by_direction.get("blocking", []),
                "dependencies": dependencies,
                "count": len(dependencies),
            }
        ).model_dump()
    except CLIError as exc:
        return _cli_error("list_issue_dependencies", exc, _READ_HINT)
    except Exception as exc:
        return handle_tool_error(exc, context="List issue dependencies failed")


async def add_issue_dependency(params: IssueDependencyInput) -> dict:
    """Add an issue dependency, returning success without mutation if present.

    Args:
        params: Blocked issue number, blocking issue number, and repository target.

    Returns:
        ToolSuccess with ``already_present`` or ToolError.
    """
    try:
        await get_service_factory().ensure_auth()
        base = _repo_path(params)
        blocked_by_path = (
            f"{base}/issues/{params.issue_number}/dependencies/blocked_by"
        )
        existing = _dependency_list(await _get(blocked_by_path))
        if _contains_issue(existing, params.blocked_by_issue_number):
            return ToolSuccess(
                data={
                    "issue_number": params.issue_number,
                    "blocked_by_issue_number": params.blocked_by_issue_number,
                    "already_present": True,
                    "message": "Issue dependency already exists; no change needed.",
                }
            ).model_dump()

        blocker = await _get(f"{base}/issues/{params.blocked_by_issue_number}")
        issue_id = blocker.get("id") if isinstance(blocker, dict) else None
        if not isinstance(issue_id, int):
            return build_error_response(
                error_type="not_found",
                message=(
                    f"Could not resolve REST id for issue #{params.blocked_by_issue_number}."
                ),
                suggestion=_READ_HINT,
            )
        await _post_dependency(blocked_by_path, issue_id)
        return ToolSuccess(
            data={
                "issue_number": params.issue_number,
                "blocked_by_issue_number": params.blocked_by_issue_number,
                "already_present": False,
                "message": "Issue dependency added successfully.",
            }
        ).model_dump()
    except CLIError as exc:
        return _cli_error("add_issue_dependency", exc, _WRITE_HINT)
    except Exception as exc:
        return handle_tool_error(exc, context="Add issue dependency failed")


async def remove_issue_dependency(params: IssueDependencyInput) -> dict:
    """Remove an issue dependency, returning success if it is already absent.

    Args:
        params: Blocked issue number, blocking issue number, and repository target.

    Returns:
        ToolSuccess with ``already_absent`` or ToolError.
    """
    try:
        await get_service_factory().ensure_auth()
        base = _repo_path(params)
        blocked_by_path = (
            f"{base}/issues/{params.issue_number}/dependencies/blocked_by"
        )
        existing = _dependency_list(await _get(blocked_by_path))
        if not _contains_issue(existing, params.blocked_by_issue_number):
            return ToolSuccess(
                data={
                    "issue_number": params.issue_number,
                    "blocked_by_issue_number": params.blocked_by_issue_number,
                    "already_absent": True,
                    "message": "Issue dependency is already absent; no change needed.",
                }
            ).model_dump()

        blocker = await _get(f"{base}/issues/{params.blocked_by_issue_number}")
        issue_id = blocker.get("id") if isinstance(blocker, dict) else None
        if not isinstance(issue_id, int):
            return build_error_response(
                error_type="not_found",
                message=(
                    f"Could not resolve REST id for issue #{params.blocked_by_issue_number}."
                ),
                suggestion=_READ_HINT,
            )
        await _delete_dependency(f"{blocked_by_path}/{issue_id}")
        return ToolSuccess(
            data={
                "issue_number": params.issue_number,
                "blocked_by_issue_number": params.blocked_by_issue_number,
                "already_absent": False,
                "message": "Issue dependency removed successfully.",
            }
        ).model_dump()
    except CLIError as exc:
        return _cli_error("remove_issue_dependency", exc, _WRITE_HINT)
    except Exception as exc:
        return handle_tool_error(exc, context="Remove issue dependency failed")


ISSUE_DEPENDENCY_TOOLS = [
    list_issue_dependencies,
    add_issue_dependency,
    remove_issue_dependency,
]

__all__ = ["ISSUE_DEPENDENCY_TOOLS", *(fn.__name__ for fn in ISSUE_DEPENDENCY_TOOLS)]
