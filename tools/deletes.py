"""Permanent-delete MCP tools (issue #34).

These five tools perform IRREVERSIBLE deletions on GitHub. They are the only
tools classified ``ToolAccess.DELETE`` and are therefore registered ONLY when
``MCP_ACCESS_LEVEL=full``. In addition, every one of them refuses to act unless
called with ``confirm: true`` — the refusal states that the operation is
permanent and points the caller at the reversible alternative
(``close_issue`` / ``archive_project_item`` / ``move_to_trash`` / …).

Each tool also honours the scope lock: when ``GH_PROJECT_SCOPE_LOCK=true`` a
delete is refused before it runs if it names a target outside the configured
org/repo/project.

Delete surface and the API each uses (verified against the GitHub API):

    delete_project_item   GraphQL deleteProjectV2Item(projectId, itemId)
    delete_issue          GraphQL deleteIssue(issueId)  (no REST equivalent)
    delete_issue_comment  REST DELETE /repos/{o}/{r}/issues/comments/{id}
    delete_label          REST DELETE via `gh label delete --yes`
    delete_milestone      REST DELETE /repos/{o}/{r}/milestones/{number}

deleteIssue and deleteProjectV2Item are GraphQL because Projects V2 and issue
deletion are GraphQL-native; comments, labels, and milestones use the REST
surface already used by their create/close siblings for consistency.
"""

from __future__ import annotations

import json
import logging

from pydantic import BaseModel, Field

from clients.gh_cli_client import CLIError, GHCLIClient
from clients.cache_manager import CacheManager
from clients.graphql_client import GraphQLClient
from core.auth import resolve_token
from core.config import get_settings
from core.error_handling import build_error_response, handle_tool_error
from graphql.mutations import DELETE_ISSUE_MUTATION, DELETE_PROJECT_ITEM_MUTATION
from models.responses import ToolSuccess
from services.discovery_service import DiscoveryService

logger = logging.getLogger(__name__)


_CONFIRM_DESCRIPTION = (
    "Must be set to true to proceed. This deletion is PERMANENT and cannot be "
    "undone. Left false (default), the tool refuses and does nothing."
)


def _needs_confirmation(operation: str, alternative: str) -> dict:
    """Build the standard refusal returned when confirm is not true."""
    return build_error_response(
        error_type="validation",
        message=(
            f"{operation} is PERMANENT and cannot be undone. Refused because "
            "confirm=true was not supplied."
        ),
        suggestion=(
            f"If you truly intend a permanent delete, call again with "
            f"confirm=true. To keep the resource recoverable instead, use "
            f"{alternative}."
        ),
    )


# ── delete_project_item ──────────────────────────────────────────────────────


class DeleteProjectItemInput(BaseModel):
    """Input schema for the delete_project_item tool."""

    item_id: str = Field(
        min_length=1,
        description="Project item node ID to delete permanently (e.g. 'PVTI_...').",
    )
    confirm: bool = Field(default=False, description=_CONFIRM_DESCRIPTION)


async def delete_project_item(params: DeleteProjectItemInput) -> dict:
    """Permanently delete a card from the Project V2 board.

    This removes the item from the board entirely (deleteProjectV2Item). Unlike
    archive_project_item / move_to_trash, it is NOT recoverable.
    """
    try:
        if not params.confirm:
            return _needs_confirmation(
                "Deleting a project item",
                "archive_project_item (archives, restorable) or move_to_trash",
            )

        token = await resolve_token()
        graphql_client = GraphQLClient(token=token)
        discovery_service = DiscoveryService(
            graphql_client=graphql_client,
            cache_manager=CacheManager(),
        )
        metadata = await discovery_service.get_cached_or_discover()

        result = await graphql_client.execute_with_retry(
            DELETE_PROJECT_ITEM_MUTATION,
            {"projectId": metadata.project_id, "itemId": params.item_id},
            is_mutation=True,
        )
        deleted_id = (
            result.get("data", {})
            .get("deleteProjectV2Item", {})
            .get("deletedItemId")
        )
        return ToolSuccess(
            data={
                "item_id": params.item_id,
                "deleted_item_id": deleted_id,
                "deleted": True,
                "message": "Project item permanently deleted.",
            },
        ).model_dump()

    except Exception as exc:
        logger.error("Error in delete_project_item: %s", exc)
        return handle_tool_error(exc, context="Delete project item failed")


# ── delete_issue ─────────────────────────────────────────────────────────────


class DeleteIssueInput(BaseModel):
    """Input schema for the delete_issue tool."""

    issue_number: int = Field(
        ge=1, description="GitHub issue number to delete permanently."
    )
    confirm: bool = Field(default=False, description=_CONFIRM_DESCRIPTION)


async def delete_issue(params: DeleteIssueInput) -> dict:
    """Permanently delete a GitHub issue (deleteIssue GraphQL mutation).

    Issue deletion is GraphQL-only and IRREVERSIBLE. Prefer close_issue, which
    is recoverable (an issue can be reopened).
    """
    try:
        if not params.confirm:
            return _needs_confirmation(
                "Deleting an issue",
                "close_issue (closes, reopenable)",
            )

        await resolve_token()
        settings = get_settings()
        repo = f"{settings.org_name}/{settings.repo_name}"
        gh_client = GHCLIClient()

        # Resolve the issue's node ID (REST exposes node_id directly).
        try:
            view = await gh_client.run([
                "api", f"repos/{repo}/issues/{params.issue_number}",
                "--jq", ".node_id",
            ])
        except CLIError as exc:
            if "not found" in exc.stderr.lower() or "404" in exc.stderr:
                return build_error_response(
                    error_type="not_found",
                    message=(
                        f"Issue #{params.issue_number} was not found in "
                        f"'{repo}'."
                    ),
                    suggestion="Verify the issue number with list_project_items.",
                )
            raise
        node_id = view.stdout.strip()
        if not node_id:
            return build_error_response(
                error_type="not_found",
                message=f"Could not resolve a node ID for issue #{params.issue_number}.",
                suggestion="Verify the issue number is correct.",
            )

        token = await resolve_token()
        graphql_client = GraphQLClient(token=token)
        result = await graphql_client.execute_with_retry(
            DELETE_ISSUE_MUTATION,
            {"issueId": node_id},
            is_mutation=True,
        )
        repository = (
            result.get("data", {})
            .get("deleteIssue", {})
            .get("repository", {})
            or {}
        )
        return ToolSuccess(
            data={
                "issue_number": params.issue_number,
                "repository": repository.get("nameWithOwner", repo),
                "deleted": True,
                "message": f"Issue #{params.issue_number} permanently deleted.",
            },
        ).model_dump()

    except Exception as exc:
        logger.error("Error in delete_issue: %s", exc)
        return handle_tool_error(exc, context="Delete issue failed")


# ── delete_issue_comment ─────────────────────────────────────────────────────


class DeleteIssueCommentInput(BaseModel):
    """Input schema for the delete_issue_comment tool."""

    comment_id: int = Field(
        ge=1,
        description=(
            "Numeric ID of the issue comment to delete permanently (the "
            "REST comment id, not the issue number)."
        ),
    )
    confirm: bool = Field(default=False, description=_CONFIRM_DESCRIPTION)


async def delete_issue_comment(params: DeleteIssueCommentInput) -> dict:
    """Permanently delete an issue comment (REST DELETE comments/{id}).

    There is no recoverable alternative for a comment; the confirm gate is the
    only safeguard.
    """
    try:
        if not params.confirm:
            return _needs_confirmation(
                "Deleting an issue comment",
                "editing the comment via edit_issue_comment instead of deleting it",
            )

        await resolve_token()
        settings = get_settings()
        repo = f"{settings.org_name}/{settings.repo_name}"
        gh_client = GHCLIClient()

        try:
            await gh_client.run([
                "api", "--method", "DELETE",
                f"repos/{repo}/issues/comments/{params.comment_id}",
            ])
        except CLIError as exc:
            if "not found" in exc.stderr.lower() or "404" in exc.stderr:
                return build_error_response(
                    error_type="not_found",
                    message=(
                        f"Comment {params.comment_id} was not found in "
                        f"'{repo}'."
                    ),
                    suggestion="Verify the numeric comment ID is correct.",
                )
            raise

        return ToolSuccess(
            data={
                "comment_id": params.comment_id,
                "deleted": True,
                "message": f"Comment {params.comment_id} permanently deleted.",
            },
        ).model_dump()

    except Exception as exc:
        logger.error("Error in delete_issue_comment: %s", exc)
        return handle_tool_error(exc, context="Delete issue comment failed")


# ── delete_label ─────────────────────────────────────────────────────────────


class DeleteLabelInput(BaseModel):
    """Input schema for the delete_label tool."""

    name: str = Field(
        min_length=1,
        description="Exact label name to delete permanently (removed from all issues).",
    )
    confirm: bool = Field(default=False, description=_CONFIRM_DESCRIPTION)


async def delete_label(params: DeleteLabelInput) -> dict:
    """Permanently delete a repository label (gh label delete --yes).

    Deleting a label removes it from every issue/PR that carried it. This is
    irreversible; there is no archive for labels.
    """
    try:
        if not params.confirm:
            return _needs_confirmation(
                "Deleting a label",
                "leaving the label in place (it can simply go unused)",
            )

        await resolve_token()
        settings = get_settings()
        repo = f"{settings.org_name}/{settings.repo_name}"
        gh_client = GHCLIClient()

        try:
            await gh_client.run([
                "label", "delete", params.name, "--repo", repo, "--yes",
            ])
        except CLIError as exc:
            if "not found" in exc.stderr.lower() or "404" in exc.stderr:
                return build_error_response(
                    error_type="not_found",
                    message=f"Label '{params.name}' was not found in '{repo}'.",
                    suggestion="Run list_labels to see existing label names.",
                )
            raise

        return ToolSuccess(
            data={
                "name": params.name,
                "deleted": True,
                "message": f"Label '{params.name}' permanently deleted.",
            },
        ).model_dump()

    except Exception as exc:
        logger.error("Error in delete_label: %s", exc)
        return handle_tool_error(exc, context="Delete label failed")


# ── delete_milestone ─────────────────────────────────────────────────────────


class DeleteMilestoneInput(BaseModel):
    """Input schema for the delete_milestone tool."""

    title: str = Field(
        min_length=1,
        description="Exact milestone title to delete permanently.",
    )
    confirm: bool = Field(default=False, description=_CONFIRM_DESCRIPTION)


async def delete_milestone(params: DeleteMilestoneInput) -> dict:
    """Permanently delete a milestone (REST DELETE milestones/{number}).

    Deleting a milestone unlinks it from its issues (the issues remain). Prefer
    close_milestone, which keeps the milestone and its history.
    """
    try:
        if not params.confirm:
            return _needs_confirmation(
                "Deleting a milestone",
                "close_milestone (keeps the milestone and its history)",
            )

        await resolve_token()
        settings = get_settings()
        repo = f"{settings.org_name}/{settings.repo_name}"
        gh_client = GHCLIClient()

        # Resolve the milestone number by title (matches close_milestone).
        list_result = await gh_client.run([
            "api", f"repos/{repo}/milestones?state=all&per_page=100",
        ])
        milestones = json.loads(list_result.stdout)
        target = next(
            (m for m in milestones if m.get("title") == params.title), None
        )
        if not target:
            return build_error_response(
                error_type="not_found",
                message=f"Milestone '{params.title}' was not found in '{repo}'.",
                suggestion="Run list_milestones (state='all') to see titles.",
            )

        await gh_client.run([
            "api", "--method", "DELETE",
            f"repos/{repo}/milestones/{target['number']}",
        ])

        return ToolSuccess(
            data={
                "title": params.title,
                "number": target["number"],
                "deleted": True,
                "message": f"Milestone '{params.title}' permanently deleted.",
            },
        ).model_dump()

    except Exception as exc:
        logger.error("Error in delete_milestone: %s", exc)
        return handle_tool_error(exc, context="Delete milestone failed")
