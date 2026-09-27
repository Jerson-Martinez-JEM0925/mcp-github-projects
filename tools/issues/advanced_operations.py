"""MCP tools for advanced issue and project operations.

Exposes tools for:
- Moving items to any status
- Bulk updating project items
- Getting issue details with sub-issues
- Listing sub-issues of a parent
- Removing sub-issues
- Reopening closed issues
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from pydantic import BaseModel, Field

from clients.gh_cli_client import CLIError
from core.config import get_settings
from core.error_handling import build_error_response, handle_tool_error
from models.responses import ToolSuccess
from core.factory import get_service_factory

logger = logging.getLogger(__name__)


class MoveToStatusInput(BaseModel):
    """Input schema for the move_to_status tool."""

    item_id: str = Field(description="Project item node ID (PVTI_...)")
    status: str = Field(description="Target status: '📢 Proposal', '📌 To Do', '🛠 In Progress', '⏸ Pending', '✅ Done', '🗑️ Trash'")


class BulkUpdateItemsInput(BaseModel):
    """Input schema for the bulk_update_items tool."""

    item_ids: list[str] = Field(
        max_length=25,
        description="List of project item node IDs to update (maximum 25)",
    )
    fields: dict = Field(description="Fields to set on all items (e.g., {\"Priority\": \"Urgent\", \"Due date\": \"2026-08-01\"})")


class GetIssueDetailInput(BaseModel):
    """Input schema for the get_issue_detail tool."""

    issue_number: int = Field(description="GitHub issue number")


class ListSubIssuesInput(BaseModel):
    """Input schema for the list_sub_issues tool."""

    issue_number: int = Field(description="Parent issue number")


class RemoveSubIssueInput(BaseModel):
    """Input schema for the remove_sub_issue tool."""

    parent_issue_number: int = Field(description="Parent issue number")
    child_issue_number: int = Field(description="Child issue number to remove")


class ReopenIssueInput(BaseModel):
    """Input schema for the reopen_issue tool."""

    issue_number: int = Field(description="GitHub issue number to reopen")


async def move_to_status(params: MoveToStatusInput) -> dict:
    """Move a project item to any status column.

    Unlike move_to_done/move_to_trash which are fixed, this tool
    allows moving to any valid status including 'To Do', 'In Progress', etc.

    Args:
        params: Input containing item_id and target status.

    Returns:
        ToolSuccess on success, or ToolError on failure.
    """

    try:
        graphql_client = await get_service_factory().graphql()
        gh_client = get_service_factory().gh()
        discovery = await get_service_factory().discovery_service(graphql_client)
        project_service = await get_service_factory().project_service(graphql_client, gh_client)
        metadata = await discovery.get_cached_or_discover()

        await project_service.update_field(
            metadata=metadata,
            item_id=params.item_id,
            field_name="Status",
            value=params.status,
        )

        return ToolSuccess(
            data={
                "item_id": params.item_id,
                "status": params.status,
                "message": f"Item moved to '{params.status}' successfully.",
            },
        ).model_dump()

    except Exception as exc:
        logger.error("Error in move_to_status: %s", exc)
        return handle_tool_error(exc, context="Move to status failed")


async def bulk_update_items(params: BulkUpdateItemsInput) -> dict:
    """Update fields on multiple project items at once.

    Applies the same field values to all specified items.

    Args:
        params: Input containing item_ids and fields dict.

    Returns:
        ToolSuccess with results per item.
    """

    try:
        graphql_client = await get_service_factory().graphql()
        gh_client = get_service_factory().gh()
        discovery = await get_service_factory().discovery_service(graphql_client)
        project_service = await get_service_factory().project_service(graphql_client, gh_client)
        metadata = await discovery.get_cached_or_discover()

        results: list[dict] = []

        for item_id in params.item_ids:
            item_results: list[dict] = []
            for field_name, value in params.fields.items():
                try:
                    await project_service.update_field(
                        metadata=metadata,
                        item_id=item_id,
                        field_name=field_name,
                        value=value,
                    )
                    item_results.append({"field": field_name, "status": "success"})
                except Exception as field_exc:
                    item_results.append({"field": field_name, "status": "failed", "error": str(field_exc)})

            results.append({"item_id": item_id, "fields": item_results})

        success_count = sum(1 for r in results if all(f["status"] == "success" for f in r["fields"]))

        return ToolSuccess(
            data={
                "results": results,
                "total": len(params.item_ids),
                "fully_updated": success_count,
                "message": f"Updated {success_count}/{len(params.item_ids)} items successfully.",
            },
        ).model_dump()

    except Exception as exc:
        logger.error("Error in bulk_update_items: %s", exc)
        return handle_tool_error(exc, context="Bulk update items failed")


async def get_issue_detail(params: GetIssueDetailInput) -> dict:
    """Get complete details of a GitHub issue including sub-issues.

    Args:
        params: Input containing the issue number.

    Returns:
        ToolSuccess with full issue details.
    """
    try:
        await get_service_factory().ensure_auth()
        settings = get_settings()
        repo = f"{settings.org_name}/{settings.repo_name}"
        gh_client = get_service_factory().gh()

        result = await gh_client.run([
            "issue", "view", str(params.issue_number), "--repo", repo,
            "--json", "number,title,body,state,labels,assignees,milestone,createdAt,updatedAt,closedAt,comments",
        ])
        issue = json.loads(result.stdout)

        # Get sub-issues via GraphQL
        node_id_result = await gh_client.run([
            "api", f"repos/{repo}/issues/{params.issue_number}", "--jq", ".node_id",
        ])
        node_id = node_id_result.stdout.strip()

        sub_issues: list[dict] = []
        if node_id:
            try:
                query = """
                query GetIssueSubIssues($nodeId: ID!) {
                  node(id: $nodeId) {
                    ... on Issue {
                      subIssues(first: 50) {
                        totalCount
                        nodes { number title state }
                      }
                    }
                  }
                }
                """
                gql_result = await gh_client.api_graphql(query, {"nodeId": node_id})
                if gql_result.get("data", {}).get("node", {}).get("subIssues"):
                    sub_issues = gql_result["data"]["node"]["subIssues"].get("nodes", [])
            except Exception:
                pass

        return ToolSuccess(
            data={
                "number": issue.get("number"),
                "title": issue.get("title"),
                "body": issue.get("body", ""),
                "state": issue.get("state"),
                "labels": [l.get("name") for l in issue.get("labels", [])],
                "assignees": [a.get("login") for a in issue.get("assignees", [])],
                "milestone": issue.get("milestone", {}).get("title") if issue.get("milestone") else None,
                "created_at": issue.get("createdAt"),
                "updated_at": issue.get("updatedAt"),
                "closed_at": issue.get("closedAt"),
                "comments_count": len(issue.get("comments", [])),
                "sub_issues": sub_issues,
                "sub_issues_count": len(sub_issues),
            },
        ).model_dump()

    except CLIError as exc:
        logger.error("CLI error in get_issue_detail: %s", exc)
        return build_error_response(
            error_type="not_found",
            message=f"Issue #{params.issue_number} not found.",
            suggestion="Verify the issue number exists.",
        )
    except Exception as exc:
        logger.error("Error in get_issue_detail: %s", exc)
        return handle_tool_error(exc, context="Get issue detail failed")


async def list_sub_issues(params: ListSubIssuesInput) -> dict:
    """List all sub-issues of a parent issue.

    Args:
        params: Input containing the parent issue number.

    Returns:
        ToolSuccess with list of sub-issues.
    """
    try:
        await get_service_factory().ensure_auth()
        settings = get_settings()
        repo = f"{settings.org_name}/{settings.repo_name}"
        gh_client = get_service_factory().gh()

        node_result = await gh_client.run([
            "api", f"repos/{repo}/issues/{params.issue_number}", "--jq", ".node_id",
        ])
        node_id = node_result.stdout.strip()

        if not node_id:
            return build_error_response(
                error_type="not_found",
                message=f"Issue #{params.issue_number} not found.",
                suggestion="Verify the issue number.",
            )

        query = """
        query ListSubIssues($nodeId: ID!) {
          node(id: $nodeId) {
            ... on Issue {
              subIssues(first: 50) {
                totalCount
                nodes {
                  number
                  title
                  state
                  assignees(first: 5) { nodes { login } }
                }
              }
            }
          }
        }
        """
        gql_result = await gh_client.api_graphql(query, {"nodeId": node_id})

        sub_issues: list[dict] = []
        if gql_result.get("data", {}).get("node", {}).get("subIssues"):
            for node in gql_result["data"]["node"]["subIssues"]["nodes"]:
                sub_issues.append({
                    "number": node.get("number"),
                    "title": node.get("title"),
                    "state": node.get("state"),
                    "assignees": [a["login"] for a in node.get("assignees", {}).get("nodes", [])],
                })

        return ToolSuccess(
            data={
                "parent_issue": params.issue_number,
                "sub_issues": sub_issues,
                "count": len(sub_issues),
            },
        ).model_dump()

    except CLIError as exc:
        logger.error("CLI error in list_sub_issues: %s", exc)
        return build_error_response(
            error_type="internal",
            message=f"Failed to list sub-issues: {exc.stderr.strip()}",
            suggestion="Verify the issue number and token permissions.",
        )
    except Exception as exc:
        logger.error("Error in list_sub_issues: %s", exc)
        return handle_tool_error(exc, context="List sub-issues failed")


async def remove_sub_issue(params: RemoveSubIssueInput) -> dict:
    """Remove a sub-issue from a parent issue.

    Args:
        params: Input containing parent and child issue numbers.

    Returns:
        ToolSuccess on success, or ToolError on failure.
    """
    try:
        await get_service_factory().ensure_auth()
        settings = get_settings()
        repo = f"{settings.org_name}/{settings.repo_name}"
        gh_client = get_service_factory().gh()

        parent_result = await gh_client.run([
            "api", f"repos/{repo}/issues/{params.parent_issue_number}", "--jq", ".node_id",
        ])
        parent_node_id = parent_result.stdout.strip()

        child_result = await gh_client.run([
            "api", f"repos/{repo}/issues/{params.child_issue_number}", "--jq", ".node_id",
        ])
        child_node_id = child_result.stdout.strip()

        if not parent_node_id or not child_node_id:
            return build_error_response(
                error_type="not_found",
                message="Could not resolve issue node IDs.",
                suggestion="Verify both issue numbers exist.",
            )

        mutation = """
        mutation RemoveSubIssue($issueId: ID!, $subIssueId: ID!) {
          removeSubIssue(input: {issueId: $issueId, subIssueId: $subIssueId}) {
            issue { number }
            subIssue { number }
          }
        }
        """
        gql_result = await gh_client.api_graphql(
            mutation,
            {"issueId": parent_node_id, "subIssueId": child_node_id},
        )

        if "errors" in gql_result:
            error_msg = gql_result["errors"][0].get("message", "Unknown error")
            return build_error_response(
                error_type="validation",
                message=f"Failed to remove sub-issue: {error_msg}",
                suggestion="Verify the child issue is actually a sub-issue of the parent.",
            )

        return ToolSuccess(
            data={
                "parent_issue": params.parent_issue_number,
                "child_issue": params.child_issue_number,
                "message": f"Issue #{params.child_issue_number} removed as sub-issue of #{params.parent_issue_number}.",
            },
        ).model_dump()

    except CLIError as exc:
        logger.error("CLI error in remove_sub_issue: %s", exc)
        return build_error_response(
            error_type="internal",
            message=f"Failed to remove sub-issue: {exc.stderr.strip()}",
            suggestion="Verify both issue numbers exist and the relationship exists.",
        )
    except Exception as exc:
        logger.error("Error in remove_sub_issue: %s", exc)
        return handle_tool_error(exc, context="Remove sub-issue failed")


async def reopen_issue(params: ReopenIssueInput) -> dict:
    """Reopen a closed GitHub issue.

    Args:
        params: Input containing the issue number to reopen.

    Returns:
        ToolSuccess on success, or ToolError on failure.
    """
    try:
        await get_service_factory().ensure_auth()
        settings = get_settings()
        repo = f"{settings.org_name}/{settings.repo_name}"
        gh_client = get_service_factory().gh()

        await gh_client.run([
            "issue", "reopen", str(params.issue_number), "--repo", repo,
        ])

        return ToolSuccess(
            data={
                "issue_number": params.issue_number,
                "state": "open",
                "message": f"Issue #{params.issue_number} reopened successfully.",
            },
        ).model_dump()

    except CLIError as exc:
        logger.error("CLI error in reopen_issue: %s", exc)
        stderr = exc.stderr.strip().lower()
        if "already open" in stderr:
            return ToolSuccess(
                data={
                    "issue_number": params.issue_number,
                    "state": "open",
                    "message": "Issue is already open. No change needed.",
                },
            ).model_dump()
        return build_error_response(
            error_type="internal",
            message=f"Failed to reopen issue: {exc.stderr.strip()}",
            suggestion="Verify the issue number exists and is currently closed.",
        )
    except Exception as exc:
        logger.error("Error in reopen_issue: %s", exc)
        return handle_tool_error(exc, context="Reopen issue failed")
