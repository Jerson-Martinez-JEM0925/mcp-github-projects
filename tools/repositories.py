"""Repository provisioning tools for GitHub.

The repository target is explicit because this operation creates a new
resource and must not silently reuse the configured project repository.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Literal

from pydantic import BaseModel, Field

from clients.gh_cli_client import CLIError, GHCLIClient
from core.access import enforce_scope
from core.auth import resolve_token
from core.config import get_settings
from core.error_handling import build_error_response, handle_tool_error
from models.responses import ToolSuccess

logger = logging.getLogger(__name__)

_REPOSITORY_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_OWNER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}$")


class CreateRepositoryInput(BaseModel):
    """Input schema for creating a GitHub repository."""

    name: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$",
        description="Repository name. Must start with an alphanumeric character.",
    )
    owner: str | None = Field(
        default=None,
        min_length=1,
        max_length=39,
        description="User or organization login. Defaults to the configured owner.",
    )
    owner_type: Literal["user", "organization"] = Field(
        default="user",
        description="Whether the repository belongs to a user or organization.",
    )
    description: str | None = Field(
        default=None,
        max_length=350,
        description="Optional repository description.",
    )
    visibility: Literal["public", "private", "internal"] = Field(
        default="public",
        description="Repository visibility. Internal is valid only for organizations.",
    )
    auto_init: bool = Field(
        default=True,
        description="Initialize the repository with an initial commit and README.",
    )
    gitignore_template: str | None = Field(
        default=None,
        max_length=100,
        description="Optional GitHub gitignore template name, such as Go.",
    )
    license_template: str | None = Field(
        default=None,
        max_length=100,
        description="Optional GitHub license template key, such as mit.",
    )
    has_issues: bool = Field(default=True)
    has_projects: bool = Field(default=True)
    has_wiki: bool = Field(default=True)
    has_discussions: bool = Field(default=False)


def _owner_login(params: CreateRepositoryInput) -> str:
    """Resolve and validate the repository owner login."""
    configured_owner = get_settings().org_name
    owner = (params.owner or configured_owner).strip()
    if not _OWNER_NAME.fullmatch(owner):
        raise ValueError("owner must be a valid GitHub user or organization login")
    return owner


def _endpoint(params: CreateRepositoryInput, owner: str) -> str:
    """Build the correct GitHub REST endpoint for the owner type."""
    if params.owner_type == "organization":
        return f"orgs/{owner}/repos"
    return "user/repos"


def _error_for_cli_failure(exc: CLIError, owner: str, name: str) -> dict:
    """Map common GitHub repository creation failures to safe MCP errors."""
    stderr = exc.stderr.strip()
    lowered = stderr.lower()
    if "already exists" in lowered or "name already exists" in lowered:
        return build_error_response(
            error_type="validation",
            message=f"Repository '{owner}/{name}' already exists.",
            suggestion="Choose another repository name or use the existing repository.",
        )
    if "already taken" in lowered or "unprocessable entity" in lowered:
        return build_error_response(
            error_type="validation",
            message=f"GitHub rejected repository '{owner}/{name}'.",
            suggestion=(
                "Verify the name, visibility, owner type, and token permissions; "
                "the repository name may already be reserved."
            ),
        )
    if "permission" in lowered or "forbidden" in lowered:
        return build_error_response(
            error_type="authentication",
            message=f"The token cannot create a repository for '{owner}'.",
            suggestion="Grant repository creation permission for the selected owner.",
        )
    return build_error_response(
        error_type="internal",
        message="GitHub repository creation failed.",
        suggestion="Check the owner, token permissions, and GitHub API status.",
    )


async def create_repository(params: CreateRepositoryInput) -> dict:
    """Create a GitHub repository through the authenticated GitHub API.

    The operation is intentionally explicit about owner type and visibility.
    The response contains repository metadata only; credentials and raw API
    error payloads are never returned.
    """
    try:
        await resolve_token()
        owner = _owner_login(params)
        settings = get_settings()
        if settings.scope_lock:
            return build_error_response(
                error_type="validation",
                message=(
                    "Creating a repository is disabled while "
                    "GH_PROJECT_SCOPE_LOCK=true confines this server to "
                    f"{settings.org_name}/{settings.repo_name} "
                    f"(project {settings.project_number})."
                ),
                suggestion=(
                    "Set GH_PROJECT_SCOPE_LOCK=false to create repositories, "
                    "or create the repository outside this scoped server."
                ),
            )
        enforce_scope(owner=owner)
        if params.visibility == "internal" and params.owner_type != "organization":
            return build_error_response(
                error_type="validation",
                message="Internal visibility is available only for organizations.",
                suggestion="Use public/private for a user repository or choose an organization owner.",
            )

        gh_client = GHCLIClient()
        args = [
            "api",
            "--method",
            "POST",
            _endpoint(params, owner),
            "-f",
            f"name={params.name}",
            "-f",
            f"visibility={params.visibility}",
            "-F",
            f"auto_init={'true' if params.auto_init else 'false'}",
            "-F",
            f"has_issues={'true' if params.has_issues else 'false'}",
            "-F",
            f"has_projects={'true' if params.has_projects else 'false'}",
            "-F",
            f"has_wiki={'true' if params.has_wiki else 'false'}",
            "-F",
            f"has_discussions={'true' if params.has_discussions else 'false'}",
        ]
        if params.description:
            args.extend(["-f", f"description={params.description}"])
        if params.gitignore_template:
            args.extend(["-f", f"gitignore_template={params.gitignore_template}"])
        if params.license_template:
            args.extend(["-f", f"license_template={params.license_template}"])

        result = await gh_client.run(args)
        repository = json.loads(result.stdout)
        data = {
            "name": repository.get("name", params.name),
            "full_name": repository.get("full_name", f"{owner}/{params.name}"),
            "owner": repository.get("owner", {}).get("login", owner),
            "owner_type": params.owner_type,
            "visibility": repository.get("visibility", params.visibility),
            "private": repository.get("private"),
            "default_branch": repository.get("default_branch"),
            "node_id": repository.get("node_id"),
            "repository_id": repository.get("id"),
            "html_url": repository.get("html_url"),
            "clone_url": repository.get("clone_url"),
            "message": f"Repository '{owner}/{params.name}' created successfully.",
        }
        return ToolSuccess(data=data).model_dump()

    except CLIError as exc:
        logger.error("CLI error in create_repository: %s", exc)
        return _error_for_cli_failure(exc, params.owner or get_settings().org_name, params.name)
    except ValueError as exc:
        return build_error_response(
            error_type="validation",
            message=str(exc),
            suggestion="Use a valid GitHub owner login and repository name.",
        )
    except Exception as exc:
        logger.error("Error in create_repository: %s", exc)
        return handle_tool_error(exc, context="Create repository failed")
