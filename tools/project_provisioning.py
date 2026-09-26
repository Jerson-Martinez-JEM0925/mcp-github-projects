"""MCP tools for provisioning GitHub Projects V2 boards.

These tools CREATE and configure project boards, complementing the
existing tools that only manage pre-existing projects. They cover:

- create_project        — create a new Project V2 under a user or org owner
- update_project        — set visibility (public/private), description, README
- create_project_field  — add a custom field (single-select with options, or
                          a typed TEXT/NUMBER/DATE field)
- link_repository       — link a repository to the project
- list_projects         — list the owner's Project V2 boards

All write operations go through the GraphQL API (never the gh CLI), consistent
with the rest of the server. Tool arguments follow the `params: <Name>Input`
Pydantic convention and responses use the ToolSuccess / build_error_response
envelopes.
"""

from __future__ import annotations

import logging
from typing import Literal, Optional

from pydantic import BaseModel, Field

from auth import resolve_token
from clients.graphql_client import GraphQLClient
from config import get_settings
from core.access import enforce_scope
from error_handling import build_error_response, handle_tool_error
from graphql.mutations import (
    CREATE_PROJECT_MUTATION,
    CREATE_SINGLE_SELECT_FIELD_MUTATION,
    CREATE_TYPED_FIELD_MUTATION,
    LINK_REPOSITORY_MUTATION,
    UPDATE_PROJECT_MUTATION,
)
from graphql.queries import (
    REPO_ID_QUERY,
    extract_owner_id,
    get_owner_id_query,
)
from models.responses import ToolSuccess

logger = logging.getLogger(__name__)

_TYPED_FIELD_TYPES = {"TEXT", "NUMBER", "DATE"}


# ── Input schemas ────────────────────────────────────────────────────────────


class CreateProjectInput(BaseModel):
    """Input schema for the create_project tool."""

    title: str = Field(
        min_length=1,
        max_length=255,
        description="Title of the new Project V2 board.",
    )
    owner_login: Optional[str] = Field(
        default=None,
        description=(
            "Owner login (user or organization). Defaults to the configured "
            "GH_PROJECT_ORG_NAME when omitted."
        ),
    )
    owner_type: Optional[Literal["user", "organization"]] = Field(
        default=None,
        description=(
            "Owner type. Defaults to the configured GH_PROJECT_OWNER_TYPE "
            "when omitted."
        ),
    )
    public: bool = Field(
        default=False,
        description="Whether the board is publicly readable. Default False.",
    )
    short_description: Optional[str] = Field(
        default=None,
        max_length=255,
        description="Optional short description shown on the project header.",
    )


class UpdateProjectInput(BaseModel):
    """Input schema for the update_project tool."""

    project_id: str = Field(description="Project V2 node ID (e.g. 'PVT_...').")
    public: Optional[bool] = Field(
        default=None, description="Set the board public (True) or private (False)."
    )
    short_description: Optional[str] = Field(
        default=None, max_length=255, description="Short description text."
    )
    readme: Optional[str] = Field(
        default=None, description="Markdown README body for the project."
    )
    closed: Optional[bool] = Field(
        default=None, description="Close (True) or reopen (False) the project."
    )


class CreateProjectFieldInput(BaseModel):
    """Input schema for the create_project_field tool.

    For a single-select field, provide `options` (a non-empty list of option
    names) and leave `data_type` as SINGLE_SELECT. For a typed field, set
    `data_type` to TEXT, NUMBER, or DATE and omit `options`.
    """

    project_id: str = Field(description="Project V2 node ID (e.g. 'PVT_...').")
    name: str = Field(min_length=1, max_length=100, description="Field name.")
    data_type: Literal["SINGLE_SELECT", "TEXT", "NUMBER", "DATE"] = Field(
        default="SINGLE_SELECT",
        description="Field data type.",
    )
    options: Optional[list[str]] = Field(
        default=None,
        description=(
            "Option names for a SINGLE_SELECT field (required, non-empty for "
            "SINGLE_SELECT). Ignored for typed fields."
        ),
    )


class LinkRepositoryInput(BaseModel):
    """Input schema for the link_repository tool."""

    project_id: str = Field(description="Project V2 node ID (e.g. 'PVT_...').")
    owner_login: Optional[str] = Field(
        default=None,
        description="Repository owner. Defaults to configured GH_PROJECT_ORG_NAME.",
    )
    repo_name: Optional[str] = Field(
        default=None,
        description="Repository name. Defaults to configured GH_PROJECT_REPO_NAME.",
    )


class ListProjectsInput(BaseModel):
    """Input schema for the list_projects tool."""

    owner_login: Optional[str] = Field(
        default=None,
        description="Owner login. Defaults to configured GH_PROJECT_ORG_NAME.",
    )
    owner_type: Optional[Literal["user", "organization"]] = Field(
        default=None,
        description="Owner type. Defaults to configured GH_PROJECT_OWNER_TYPE.",
    )
    first: int = Field(default=20, ge=1, le=100, description="Max boards to return.")


# ── Helpers ──────────────────────────────────────────────────────────────────


async def _resolve_owner_id(
    client: GraphQLClient, owner_login: str, owner_type: str
) -> str | None:
    """Resolve the GraphQL node ID for a user/org owner login."""
    query = get_owner_id_query(owner_type)  # type: ignore[arg-type]
    result = await client.execute_with_retry(query, {"login": owner_login})
    return extract_owner_id(result, owner_type)  # type: ignore[arg-type]


# ── Tools ────────────────────────────────────────────────────────────────────


async def create_project(params: CreateProjectInput) -> dict:
    """Create a new GitHub Project V2 board under a user or organization.

    Resolves the owner node ID, creates the board, and optionally sets its
    visibility and short description in a follow-up update.

    Returns:
        ToolSuccess with the new project's id, number, title, url, public flag.
    """
    try:
        token = await resolve_token()
        settings = get_settings()
        if settings.scope_lock:
            return build_error_response(
                error_type="validation",
                message=(
                    "Creating a project is disabled while "
                    "GH_PROJECT_SCOPE_LOCK=true confines this server to "
                    f"project {settings.project_number} of "
                    f"{settings.org_name}/{settings.repo_name}."
                ),
                suggestion=(
                    "Set GH_PROJECT_SCOPE_LOCK=false to create projects, or "
                    "create the board outside this scoped server."
                ),
            )
        owner_login = params.owner_login or settings.org_name
        owner_type = params.owner_type or settings.owner_type
        client = GraphQLClient(token=token)

        owner_id = await _resolve_owner_id(client, owner_login, owner_type)
        if not owner_id:
            return build_error_response(
                error_type="not_found",
                message=f"Could not resolve owner '{owner_login}' ({owner_type}).",
                suggestion="Check the owner login and owner_type (user vs organization).",
            )

        created = await client.execute_with_retry(
            CREATE_PROJECT_MUTATION,
            {"ownerId": owner_id, "title": params.title},
            is_mutation=True,
        )
        project = created["data"]["createProjectV2"]["projectV2"]

        # Apply visibility / description if requested.
        if params.public or params.short_description:
            updated = await client.execute_with_retry(
                UPDATE_PROJECT_MUTATION,
                {
                    "projectId": project["id"],
                    "public": params.public if params.public else None,
                    "shortDescription": params.short_description,
                    "readme": None,
                    "closed": None,
                },
                is_mutation=True,
            )
            merged = updated["data"]["updateProjectV2"]["projectV2"]
            project.update({k: v for k, v in merged.items() if v is not None})

        return ToolSuccess(
            data={
                "id": project["id"],
                "number": project.get("number"),
                "title": project.get("title"),
                "url": project.get("url"),
                "public": project.get("public", params.public),
                "message": f"Project '{params.title}' created successfully.",
            }
        ).model_dump()

    except Exception as exc:  # noqa: BLE001 - normalized by handle_tool_error
        logger.error("Error in create_project: %s", exc)
        return handle_tool_error(exc, context="Create project failed")


async def update_project(params: UpdateProjectInput) -> dict:
    """Update a Project V2's visibility, description, README, or closed state."""
    try:
        token = await resolve_token()
        client = GraphQLClient(token=token)
        result = await client.execute_with_retry(
            UPDATE_PROJECT_MUTATION,
            {
                "projectId": params.project_id,
                "public": params.public,
                "shortDescription": params.short_description,
                "readme": params.readme,
                "closed": params.closed,
            },
            is_mutation=True,
        )
        project = result["data"]["updateProjectV2"]["projectV2"]
        return ToolSuccess(
            data={
                "id": project["id"],
                "number": project.get("number"),
                "title": project.get("title"),
                "url": project.get("url"),
                "public": project.get("public"),
                "short_description": project.get("shortDescription"),
                "message": "Project updated successfully.",
            }
        ).model_dump()
    except Exception as exc:  # noqa: BLE001
        logger.error("Error in update_project: %s", exc)
        return handle_tool_error(exc, context="Update project failed")


async def create_project_field(params: CreateProjectFieldInput) -> dict:
    """Create a custom field on a Project V2 board.

    SINGLE_SELECT fields are created with their options in one mutation
    (GitHub requires at least one option). TEXT/NUMBER/DATE fields are
    created as typed fields.
    """
    try:
        token = await resolve_token()
        client = GraphQLClient(token=token)

        if params.data_type == "SINGLE_SELECT":
            if not params.options:
                return build_error_response(
                    error_type="validation",
                    message="SINGLE_SELECT fields require a non-empty 'options' list.",
                    suggestion="Provide at least one option name, e.g. options=['Bug','Feature'].",
                )
            # Each option needs a name, color, and description per the API.
            option_inputs = [
                {"name": name, "color": "GRAY", "description": ""}
                for name in params.options
            ]
            result = await client.execute_with_retry(
                CREATE_SINGLE_SELECT_FIELD_MUTATION,
                {
                    "projectId": params.project_id,
                    "name": params.name,
                    "options": option_inputs,
                },
                is_mutation=True,
            )
            field = result["data"]["createProjectV2Field"]["projectV2Field"]
            return ToolSuccess(
                data={
                    "id": field["id"],
                    "name": field["name"],
                    "data_type": field.get("dataType", "SINGLE_SELECT"),
                    "options": field.get("options", []),
                    "message": f"Single-select field '{params.name}' created.",
                }
            ).model_dump()

        # Typed field (TEXT / NUMBER / DATE)
        result = await client.execute_with_retry(
            CREATE_TYPED_FIELD_MUTATION,
            {
                "projectId": params.project_id,
                "name": params.name,
                "dataType": params.data_type,
            },
            is_mutation=True,
        )
        field = result["data"]["createProjectV2Field"]["projectV2Field"]
        return ToolSuccess(
            data={
                "id": field["id"],
                "name": field["name"],
                "data_type": field.get("dataType", params.data_type),
                "message": f"{params.data_type} field '{params.name}' created.",
            }
        ).model_dump()

    except Exception as exc:  # noqa: BLE001
        logger.error("Error in create_project_field: %s", exc)
        return handle_tool_error(exc, context="Create project field failed")


async def link_repository(params: LinkRepositoryInput) -> dict:
    """Link a repository to a Project V2 board so its issues can be added."""
    try:
        token = await resolve_token()
        settings = get_settings()
        owner = params.owner_login or settings.org_name
        repo = params.repo_name or settings.repo_name
        enforce_scope(owner=owner, repo=repo)
        client = GraphQLClient(token=token)

        repo_result = await client.execute_with_retry(
            REPO_ID_QUERY, {"owner": owner, "name": repo}
        )
        repo_node = repo_result.get("data", {}).get("repository")
        if not repo_node:
            return build_error_response(
                error_type="not_found",
                message=f"Repository '{owner}/{repo}' not found.",
                suggestion="Verify the owner and repository name.",
            )

        result = await client.execute_with_retry(
            LINK_REPOSITORY_MUTATION,
            {"projectId": params.project_id, "repositoryId": repo_node["id"]},
            is_mutation=True,
        )
        linked = result["data"]["linkProjectV2ToRepository"]["repository"]
        return ToolSuccess(
            data={
                "repository": linked.get("nameWithOwner"),
                "project_id": params.project_id,
                "message": f"Repository '{linked.get('nameWithOwner')}' linked to project.",
            }
        ).model_dump()
    except Exception as exc:  # noqa: BLE001
        logger.error("Error in link_repository: %s", exc)
        return handle_tool_error(exc, context="Link repository failed")


async def list_projects(params: ListProjectsInput) -> dict:
    """List Project V2 boards owned by a user or organization."""
    try:
        token = await resolve_token()
        settings = get_settings()
        owner = params.owner_login or settings.org_name
        owner_type = params.owner_type or settings.owner_type
        enforce_scope(owner=owner)
        client = GraphQLClient(token=token)

        field = "user" if owner_type == "user" else "organization"
        query = (
            f"query ListProjects($login: String!, $first: Int!) {{\n"
            f"  {field}(login: $login) {{\n"
            f"    projectsV2(first: $first) {{\n"
            f"      nodes {{ id number title url public closed shortDescription }}\n"
            f"    }}\n"
            f"  }}\n"
            f"}}"
        )
        result = await client.execute_with_retry(
            query, {"login": owner, "first": params.first}
        )
        node = result.get("data", {}).get(field, {}) or {}
        projects = (node.get("projectsV2", {}) or {}).get("nodes", []) or []
        return ToolSuccess(
            data={"projects": projects, "count": len(projects)}
        ).model_dump()
    except Exception as exc:  # noqa: BLE001
        logger.error("Error in list_projects: %s", exc)
        return handle_tool_error(exc, context="List projects failed")
