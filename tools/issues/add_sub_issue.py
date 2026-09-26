"""MCP tool for adding sub-issues to a parent issue.

Exposes the `add_sub_issue` tool function that links a child issue
as a sub-issue of a parent issue using GitHub's GraphQL API.
"""
from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from clients.gh_cli_client import CLIError
from core.config import get_settings
from core.error_handling import build_error_response, handle_tool_error
from models.responses import ToolSuccess
from core.factory import get_service_factory

logger = logging.getLogger(__name__)


class AddSubIssueInput(BaseModel):
    """Input schema for the add_sub_issue tool."""

    parent_issue_number: int = Field(description="Parent issue number")
    child_issue_number: int = Field(description="Child issue number to add as sub-issue")


async def add_sub_issue(params: AddSubIssueInput) -> dict:
    """Add a child issue as a sub-issue of a parent issue.

    Uses GitHub's GraphQL addSubIssue mutation to create a formal
    parent-child relationship between issues.

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

        # Get node IDs for both issues
        parent_result = await gh_client.run([
            "api", f"repos/{repo}/issues/{params.parent_issue_number}",
            "--jq", ".node_id",
        ])
        parent_node_id = parent_result.stdout.strip()

        child_result = await gh_client.run([
            "api", f"repos/{repo}/issues/{params.child_issue_number}",
            "--jq", ".node_id",
        ])
        child_node_id = child_result.stdout.strip()

        if not parent_node_id or not child_node_id:
            return build_error_response(
                error_type="not_found",
                message="Could not resolve issue node IDs.",
                suggestion="Verify both issue numbers exist in the repository.",
            )

        # Execute addSubIssue mutation
        mutation = """
        mutation AddSubIssue($issueId: ID!, $subIssueId: ID!) {
          addSubIssue(input: {issueId: $issueId, subIssueId: $subIssueId}) {
            issue { number }
            subIssue { number }
          }
        }
        """

        graphql_result = await gh_client.api_graphql(
            mutation,
            {"issueId": parent_node_id, "subIssueId": child_node_id},
        )

        # Check for errors in response
        if "errors" in graphql_result:
            error_msg = graphql_result["errors"][0].get("message", "Unknown error")
            if "duplicate" in error_msg.lower():
                return ToolSuccess(
                    data={
                        "parent_issue": params.parent_issue_number,
                        "child_issue": params.child_issue_number,
                        "message": "Sub-issue already linked (no change needed).",
                    },
                ).model_dump()
            return build_error_response(
                error_type="validation",
                message=f"Failed to add sub-issue: {error_msg}",
                suggestion="Ensure the child issue doesn't already have a parent and both issues exist.",
            )

        return ToolSuccess(
            data={
                "parent_issue": params.parent_issue_number,
                "child_issue": params.child_issue_number,
                "message": f"Issue #{params.child_issue_number} added as sub-issue of #{params.parent_issue_number}.",
            },
        ).model_dump()

    except CLIError as exc:
        logger.error("CLI error in add_sub_issue: %s", exc)
        return build_error_response(
            error_type="internal",
            message=f"Failed to add sub-issue: {exc.stderr.strip()}",
            suggestion="Verify both issue numbers exist and the token has write permissions.",
        )
    except Exception as exc:
        logger.error("Error in add_sub_issue: %s", exc)
        return handle_tool_error(exc, context="Add sub-issue failed")
