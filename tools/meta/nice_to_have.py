"""MCP tools for nice-to-have automation features.

Exposes tools for:
- Getting project statistics (items per status, velocity)
- Getting sprint summary (milestone progress, burndown data)
- Linking pull requests to issues
- Bulk assigning issues to users/milestones
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date
from typing import Literal, Optional

from pydantic import BaseModel, Field

from clients.gh_cli_client import CLIError
from core.access import resolve_repository_target
from core.config import get_settings
from core.error_handling import build_error_response, handle_tool_error
from models.responses import ToolSuccess
from core.factory import get_service_factory
from services.field_defaults import compute_defaults

logger = logging.getLogger(__name__)


class GetProjectStatsInput(BaseModel):
    """Input schema for the get_project_stats tool."""

    pass


class GetSprintSummaryInput(BaseModel):
    """Input schema for the get_sprint_summary tool."""

    milestone_title: str = Field(description="Milestone title to get summary for")


class LinkPullRequestInput(BaseModel):
    """Input schema for the link_pull_request tool."""

    issue_number: int = Field(description="Issue number to link the PR to")
    pr_number: int = Field(description="Pull request number to link")
    owner: str | None = Field(
        default=None,
        description="Repository owner. Defaults to GH_PROJECT_ORG_NAME (scope-lock checked).",
    )
    repo: str | None = Field(
        default=None,
        description="Repository name. Defaults to GH_PROJECT_REPO_NAME (scope-lock checked).",
    )
    repository: str | None = Field(
        default=None,
        description="Repository as owner/name; cannot be combined with owner or repo.",
    )
    keyword: Literal["closes", "fixes", "resolves", "refs", "part_of"] = Field(
        default="closes", description="Reference keyword used when linking a PR to an issue."
    )


class PullRequestMetadataInput(BaseModel):
    """Metadata shared by pull-request creation and update."""

    owner: str | None = Field(
        default=None,
        description="Repository owner. Defaults to GH_PROJECT_ORG_NAME (scope-lock checked).",
    )
    repo: str | None = Field(
        default=None,
        description="Repository name. Defaults to GH_PROJECT_REPO_NAME (scope-lock checked).",
    )
    repository: str | None = Field(
        default=None,
        description="Repository as owner/name; cannot be combined with owner or repo.",
    )

    reviewers: list[str] | None = Field(
        default=None,
        min_length=1,
        max_length=15,
        description="GitHub usernames to request for review (the PR author is not allowed).",
    )
    team_reviewers: list[str] | None = Field(
        default=None,
        min_length=1,
        max_length=15,
        description="GitHub team slugs to request for review.",
    )
    labels: list[str] | None = Field(
        default=None,
        min_length=1,
        max_length=50,
        description="Repository labels to replace/set on the pull request.",
    )
    assignees: list[str] | None = Field(
        default=None,
        min_length=1,
        max_length=50,
        description="GitHub usernames to assign to the pull request.",
    )
    milestone: str | None = Field(
        default=None,
        min_length=1,
        max_length=256,
        description="Repository milestone title to assign.",
    )
    status: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        description="Project V2 Status option name.",
    )
    priority: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        description="Project V2 Priority option name.",
    )
    area: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        description="Project V2 Area option name.",
    )
    work_type: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        description="Project V2 Work Type option name.",
    )
    estimate: float | None = Field(
        default=None,
        ge=0.25,
        le=9999,
        description="Project estimate in hours (also subject to configured granularity).",
    )
    due_date: str | None = Field(
        default=None,
        description="Project due date in ISO 8601 format (YYYY-MM-DD).",
    )
    apply_defaults: bool = Field(
        default=False,
        description="Fill omitted Project V2 fields using configured defaults.",
    )


class CreatePullRequestInput(PullRequestMetadataInput):
    """Input schema for the create_pull_request tool."""

    title: str = Field(description="Pull request title")
    head: str = Field(description="Name of the branch where changes are implemented")
    base: str = Field(
        default="main",
        description="Branch to merge the changes into (default: main)",
    )
    body: Optional[str] = Field(default=None, description="Pull request body / description")
    draft: bool = Field(
        default=False,
        description="Open the pull request as a draft (default: false)",
    )
    link_to_issue: Optional[int] = Field(
        default=None,
        description="Optional issue number to link the new PR to via link_pull_request",
    )
    add_to_project: bool = Field(
        default=False,
        description=(
            "When true, add the new PR to the configured Project V2 board. "
            "Project fields or apply_defaults also cause it to be added."
        ),
    )


class UpdatePullRequestInput(PullRequestMetadataInput):
    """Input schema for the update_pull_request tool.

    ``pull_number`` is required; every other field is an optional update and
    at least one of them must be supplied (enforced in the tool body, so an
    empty update returns a structured validation error rather than a no-op
    REST call).
    """

    pull_number: int = Field(description="Number of the pull request to update")
    title: Optional[str] = Field(
        default=None,
        description="New pull request title",
        min_length=1,
        max_length=256,
    )
    body: Optional[str] = Field(
        default=None,
        description="New pull request body / description (supports multiline)",
        max_length=65536,
    )
    base: Optional[str] = Field(
        default=None,
        description="New base branch to retarget the pull request to",
        min_length=1,
    )
    state: Optional[str] = Field(
        default=None,
        description="New pull request state: 'open' or 'closed'",
    )
    maintainer_can_modify: Optional[bool] = Field(
        default=None,
        description="Whether maintainers can modify the pull request branch",
    )
    add_to_project: bool = Field(
        default=False,
        description="Add the PR to the configured Project V2 board when true.",
    )


class BulkAssignInput(BaseModel):
    """Input schema for the bulk_assign tool."""

    issue_numbers: list[int] = Field(description="List of issue numbers to assign")
    assignees: Optional[list[str]] = Field(default=None, description="GitHub usernames to assign")
    milestone: Optional[str] = Field(default=None, description="Milestone title to set on all issues")
    owner: Optional[str] = Field(
        default=None,
        description="Repository owner. Defaults to GH_PROJECT_ORG_NAME (scope-lock checked).",
    )
    repo: Optional[str] = Field(
        default=None,
        description="Repository name. Defaults to GH_PROJECT_REPO_NAME (scope-lock checked).",
    )
    repository: Optional[str] = Field(
        default=None,
        description="Repository as owner/name; cannot be combined with owner or repo.",
    )


def _validate_pull_request_response(payload: object, *, operation: str) -> dict:
    """Require the identifiers needed to reconcile a PR mutation safely."""
    if not isinstance(payload, dict):
        raise ValueError(f"GitHub returned a non-object response while {operation} a pull request")

    number = payload.get("number")
    url = payload.get("html_url")
    missing: list[str] = []
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        missing.append("number")
    if not isinstance(url, str) or not url.strip():
        missing.append("html_url")
    if missing:
        raise ValueError(
            f"GitHub returned an incomplete response while {operation} a pull request; "
            f"missing {', '.join(missing)}"
        )
    return payload


async def get_project_stats() -> dict:
    """Get statistics for the GitHub Project board.

    Returns sprint progress, issue counts, and milestone data.

    Returns:
        ToolSuccess with project statistics.
    """
    try:
        await get_service_factory().ensure_auth()
        settings = get_settings()
        repo = f"{settings.org_name}/{settings.repo_name}"
        gh_client = get_service_factory().gh()

        # Get milestones for sprint tracking
        ms_result = await gh_client.run([
            "api", f"repos/{repo}/milestones?state=open&per_page=100&sort=due_on",
        ])
        milestones = json.loads(ms_result.stdout)

        sprint_stats = []
        total_open = 0
        total_closed = 0
        for ms in milestones:
            open_count = ms.get("open_issues", 0)
            closed_count = ms.get("closed_issues", 0)
            total = open_count + closed_count
            total_open += open_count
            total_closed += closed_count
            sprint_stats.append({
                "title": ms.get("title"),
                "open_issues": open_count,
                "closed_issues": closed_count,
                "total": total,
                "progress_pct": round(closed_count / max(total, 1) * 100),
                "due_on": ms.get("due_on"),
            })

        return ToolSuccess(
            data={
                "total_open_issues": total_open,
                "total_closed_issues": total_closed,
                "overall_progress_pct": round(total_closed / max(total_open + total_closed, 1) * 100),
                "sprints": sprint_stats,
                "sprint_count": len(sprint_stats),
            },
        ).model_dump()

    except Exception as exc:
        logger.error("Error in get_project_stats: %s", exc)
        return handle_tool_error(exc, context="Get project stats failed")


async def get_sprint_summary(params: GetSprintSummaryInput) -> dict:
    """Get summary of a specific sprint/milestone.

    Returns issues, completion percentage, assignee distribution, days remaining.

    Args:
        params: Input containing milestone title.

    Returns:
        ToolSuccess with sprint summary data.
    """
    try:
        await get_service_factory().ensure_auth()
        settings = get_settings()
        repo = f"{settings.org_name}/{settings.repo_name}"
        gh_client = get_service_factory().gh()

        ms_result = await gh_client.run([
            "api", f"repos/{repo}/milestones?state=open&per_page=100",
        ])
        milestones = json.loads(ms_result.stdout)

        target = None
        for ms in milestones:
            if ms.get("title") == params.milestone_title:
                target = ms
                break

        if not target:
            return build_error_response(
                error_type="not_found",
                message=f"Milestone '{params.milestone_title}' not found.",
                suggestion="Use list_milestones to see available milestones.",
            )

        issues_result = await gh_client.run([
            "issue", "list", "--repo", repo, "--milestone", params.milestone_title,
            "--state", "all", "--limit", "100",
            "--json", "number,title,state,assignees,labels",
        ])
        issues = json.loads(issues_result.stdout)

        open_issues = [i for i in issues if i.get("state") == "OPEN"]
        closed_issues = [i for i in issues if i.get("state") == "CLOSED"]

        assignee_counts: dict[str, int] = {}
        for issue in issues:
            for assignee in issue.get("assignees", []):
                login = assignee.get("login", "unassigned")
                assignee_counts[login] = assignee_counts.get(login, 0) + 1

        due_on = target.get("due_on")
        days_remaining = None
        if due_on:
            from datetime import datetime, timezone
            due_date = datetime.fromisoformat(due_on.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            days_remaining = max(0, (due_date - now).days)

        total = len(issues)
        progress_pct = round(len(closed_issues) / max(total, 1) * 100)

        return ToolSuccess(
            data={
                "milestone": params.milestone_title,
                "due_on": due_on,
                "days_remaining": days_remaining,
                "total_issues": total,
                "open": len(open_issues),
                "closed": len(closed_issues),
                "progress_pct": progress_pct,
                "assignee_distribution": assignee_counts,
                "open_issues": [{"number": i["number"], "title": i["title"]} for i in open_issues],
            },
        ).model_dump()

    except CLIError as exc:
        logger.error("CLI error in get_sprint_summary: %s", exc)
        return build_error_response(
            error_type="internal",
            message=f"Failed to get sprint summary: {exc.stderr.strip()}",
            suggestion="Verify the milestone title is correct.",
        )
    except Exception as exc:
        logger.error("Error in get_sprint_summary: %s", exc)
        return handle_tool_error(exc, context="Get sprint summary failed")


async def link_pull_request(params: LinkPullRequestInput) -> dict:
    """Link a pull request to an issue for traceability.

    Adds closing reference to PR body and comments on the issue.

    Args:
        params: Input containing issue and PR numbers.

    Returns:
        ToolSuccess on success.
    """
    try:
        await get_service_factory().ensure_auth()
        repo = resolve_repository_target(params.repository, owner=params.owner, repo=params.repo)
        gh_client = get_service_factory().gh()

        pr_result = await gh_client.run([
            "pr", "view", str(params.pr_number), "--repo", repo,
            "--json", "title,url,body",
        ])
        pr_data = json.loads(pr_result.stdout)
        pr_url = pr_data.get("url", "")
        pr_body = pr_data.get("body", "")

        keyword_labels = {
            "closes": "Closes",
            "fixes": "Fixes",
            "resolves": "Resolves",
            "refs": "Refs",
            "part_of": "Part of",
        }
        keyword_label = keyword_labels[params.keyword]
        reference = f"{keyword_label} #{params.issue_number}"
        reference_pattern = re.compile(
            rf"^\s*(?:Closes|Fixes|Resolves|Refs|Part of)\s+#{params.issue_number}\s*$",
            re.IGNORECASE | re.MULTILINE,
        )
        if reference_pattern.search(pr_body):
            new_body = reference_pattern.sub(reference, pr_body, count=1)
        else:
            new_body = f"{pr_body}\n\n{reference}" if pr_body else reference
        if new_body != pr_body:
            await gh_client.run([
                "pr", "edit", str(params.pr_number), "--repo", repo,
                "--body", new_body,
            ])

        comment = f"🔗 Linked to PR #{params.pr_number}: {pr_url}"
        await gh_client.run([
            "issue", "comment", str(params.issue_number),
            "--repo", repo, "--body", comment,
        ])

        return ToolSuccess(
            data={
                "issue_number": params.issue_number,
                "pr_number": params.pr_number,
                "pr_url": pr_url,
                "message": f"PR #{params.pr_number} linked to issue #{params.issue_number}.",
            },
        ).model_dump()

    except CLIError as exc:
        logger.error("CLI error in link_pull_request: %s", exc)
        return build_error_response(
            error_type="internal",
            message=f"Failed to link PR: {exc.stderr.strip()}",
            suggestion="Verify both issue and PR numbers exist.",
        )
    except Exception as exc:
        logger.error("Error in link_pull_request: %s", exc)
        return handle_tool_error(exc, context="Link pull request failed")


def _requested_board_fields(params: PullRequestMetadataInput) -> dict[str, str | float]:
    """Collect explicitly supplied Project V2 fields in stable order."""
    return {
        name: value
        for name, value in (
            ("Status", params.status),
            ("Priority", params.priority),
            ("Area", params.area),
            ("Work Type", params.work_type),
            ("Estimate", params.estimate),
            ("Due date", params.due_date),
        )
        if value is not None
    }


async def _resolve_milestone_number(gh_client, repo: str, title: str) -> int:
    """Resolve a repository milestone title to the REST API's numeric ID."""
    result = await gh_client.run([
        "api",
        f"repos/{repo}/milestones?state=all&per_page=100",
    ])
    milestones = json.loads(result.stdout or "[]")
    for milestone in milestones if isinstance(milestones, list) else []:
        if isinstance(milestone, dict) and milestone.get("title") == title:
            number = milestone.get("number")
            if isinstance(number, int):
                return number
    raise ValueError(f"Milestone '{title}' was not found in {repo}.")


async def _apply_pull_request_metadata(
    *,
    repo: str,
    pull_number: int,
    gh_client,
    params: PullRequestMetadataInput,
    pull_request_data: dict | None = None,
) -> list[dict[str, str]]:
    """Apply GitHub and Project V2 metadata, reporting each partial outcome."""
    results: list[dict[str, str]] = []
    board_fields = _requested_board_fields(params)

    issue_fields: list[str] = []
    issue_args = [
        "api",
        "--method", "PATCH",
        f"repos/{repo}/issues/{pull_number}",
    ]
    if params.milestone is not None:
        try:
            milestone_number = await _resolve_milestone_number(
                gh_client, repo, params.milestone
            )
            issue_args.extend(["-F", f"milestone={milestone_number}"])
            issue_fields.append("milestone")
        except Exception as exc:
            results.append({
                "field": "milestone",
                "outcome": "failure",
                "reason": str(exc),
            })
    if params.labels is not None:
        for label in params.labels:
            issue_args.extend(["-f", f"labels[]={label}"])
        issue_fields.append("labels")
    if params.assignees is not None:
        for assignee in params.assignees:
            issue_args.extend(["-f", f"assignees[]={assignee}"])
        issue_fields.append("assignees")
    if issue_fields:
        try:
            await gh_client.run(issue_args)
            for field_name in issue_fields:
                results.append({
                    "field": field_name,
                    "outcome": "success",
                    "value": (
                        params.milestone
                        if field_name == "milestone"
                        else ", ".join(getattr(params, field_name) or [])
                    ),
                })
        except Exception as exc:
            for field_name in issue_fields:
                results.append({
                    "field": field_name,
                    "outcome": "failure",
                    "reason": str(exc),
                })

    reviewers = params.reviewers or []
    team_reviewers = params.team_reviewers or []
    if reviewers or team_reviewers:
        try:
            if len(reviewers) + len(team_reviewers) > 15:
                results.append({
                    "field": "reviewers",
                    "outcome": "failure",
                    "reason": "GitHub allows at most 15 requested users and teams per call.",
                })
            else:
                pr_data = pull_request_data or {}
                author = ((pr_data.get("user") or {}).get("login") or "").casefold()
                if not author:
                    pr_result = await gh_client.run([
                        "api", f"repos/{repo}/pulls/{pull_number}",
                    ])
                    pr_data = json.loads(pr_result.stdout or "{}")
                    author = ((pr_data.get("user") or {}).get("login") or "").casefold()
                if not author:
                    results.append({
                        "field": "reviewers",
                        "outcome": "failure",
                        "reason": (
                            "Could not determine the pull request author; "
                            "no review was requested."
                        ),
                    })
                elif any(login.casefold() == author for login in reviewers):
                    results.append({
                        "field": "reviewers",
                        "outcome": "failure",
                        "reason": (
                            f"Cannot request a review from the pull request author "
                            f"'{author}'."
                        ),
                    })
                else:
                    reviewer_args = [
                        "api",
                        "--method", "POST",
                        f"repos/{repo}/pulls/{pull_number}/requested_reviewers",
                    ]
                    for login in reviewers:
                        reviewer_args.extend(["-f", f"reviewers[]={login}"])
                    for team in team_reviewers:
                        reviewer_args.extend(["-f", f"team_reviewers[]={team}"])
                    await gh_client.run(reviewer_args)
                    if reviewers:
                        results.append({
                            "field": "reviewers",
                            "outcome": "success",
                            "value": ", ".join(reviewers),
                        })
                    if team_reviewers:
                        results.append({
                            "field": "team_reviewers",
                            "outcome": "success",
                            "value": ", ".join(team_reviewers),
                        })
        except Exception as exc:
            results.append({
                "field": "reviewers",
                "outcome": "failure",
                "reason": str(exc),
            })

    if params.apply_defaults or board_fields:
        try:
            settings = get_settings()
            graphql_client = await get_service_factory().graphql()
            discovery = await get_service_factory().discovery_service(graphql_client)
            project_service = await get_service_factory().project_service(
                graphql_client, gh_client
            )
            metadata = await discovery.get_cached_or_discover()
            item_id = await project_service.resolve_item_id(metadata, pull_number)
            if not item_id:
                content_id = await project_service.resolve_content_id(pull_number)
                item_id = await project_service.add_item_by_content_id(
                    metadata, content_id
                )

            effective_fields = dict(board_fields)
            if params.apply_defaults:
                effective_fields = compute_defaults(
                    metadata=metadata,
                    settings=settings,
                    provided=board_fields,
                    labels=params.labels,
                )
            for field_name, value in effective_fields.items():
                try:
                    if field_name == "Due date":
                        date.fromisoformat(str(value))
                    if field_name == "Estimate":
                        estimate = float(value)
                        granularity = settings.estimate_granularity
                        if (
                            estimate < settings.estimate_min
                            or estimate > settings.estimate_max
                            or abs(estimate / granularity - round(estimate / granularity)) > 1e-8
                        ):
                            raise ValueError(
                                f"Estimate must be between {settings.estimate_min} "
                                f"and {settings.estimate_max} hours in increments "
                                f"of {granularity}."
                            )
                    await project_service.update_field(
                        metadata, item_id, field_name, value
                    )
                    results.append({
                        "field": field_name,
                        "outcome": "success",
                        "value": str(value),
                    })
                except Exception as exc:
                    results.append({
                        "field": field_name,
                        "outcome": "failure",
                        "reason": str(exc),
                    })
        except Exception as exc:
            results.append({
                "field": "project_fields",
                "outcome": "failure",
                "reason": str(exc),
            })

    return results


async def create_pull_request(params: CreatePullRequestInput) -> dict:
    """Open a pull request and optionally populate its GitHub/project metadata.

    Calls `POST /repos/{owner}/{repo}/pulls` (through `gh api`) using the
    scope-locked target. Optional follow-up operations request reviewers, set
    labels/assignees/milestone, add the PR to Project V2, and write board
    fields. Each follow-up is reported separately so partial metadata failure
    is never presented as complete success.

    Args:
        params: Pull request content and optional GitHub/Project V2 metadata.

    Returns:
        ToolSuccess with the created PR number and URL, optional project/link
        results, and per-field metadata outcomes.
    """
    try:
        await get_service_factory().ensure_auth()
        settings = get_settings()
        repo = resolve_repository_target(params.repository, owner=params.owner, repo=params.repo)
        gh_client = get_service_factory().gh()

        api_args = [
            "api",
            "--method", "POST",
            f"repos/{repo}/pulls",
            "-f", f"title={params.title}",
            "-f", f"head={params.head}",
            "-f", f"base={params.base}",
            "-F", f"draft={'true' if params.draft else 'false'}",
        ]
        if params.body:
            api_args.extend(["-f", f"body={params.body}"])

        create_result = await gh_client.run(api_args)
        pr_data = _validate_pull_request_response(
            json.loads(create_result.stdout), operation="creating"
        )
        pr_number = pr_data["number"]
        pr_url = pr_data["html_url"]

        data: dict = {
            "pr_number": pr_number,
            "pr_url": pr_url,
            "title": params.title,
            "head": params.head,
            "base": params.base,
            "draft": params.draft,
            "state": pr_data.get("state", "open"),
            "message": f"PR #{pr_number} created: {pr_url}",
        }

        if params.link_to_issue is not None:
            link_result = await link_pull_request(
                LinkPullRequestInput(
                    issue_number=params.link_to_issue,
                    pr_number=pr_number,
                )
            )
            data["link_result"] = link_result

        if params.add_to_project and pr_number is not None:
            # Deferred import to avoid a tools→tools import cycle at load time.
            from tools.projects.add_item import (
                AddItemToProjectInput,
                add_item_to_project,
            )

            add_result = await add_item_to_project(
                AddItemToProjectInput(issue_or_pr_number=pr_number)
            )
            if add_result.get("ok"):
                try:
                    graphql_client = await get_service_factory().graphql()
                    discovery = await get_service_factory().discovery_service(graphql_client)
                    project_service = await get_service_factory().project_service(
                        graphql_client, gh_client
                    )
                    metadata = await discovery.get_cached_or_discover()
                    verified_item_id = await project_service.resolve_item_id(
                        metadata, pr_number, repository=repo
                    )
                    if not verified_item_id:
                        content_id = await project_service.resolve_content_id(pr_number)
                        verified_item_id = await project_service.add_item_by_content_id(
                            metadata, content_id
                        )
                        verified_item_id = await project_service.resolve_item_id(
                            metadata, pr_number, repository=repo
                        ) or verified_item_id
                    add_result.setdefault("data", {})["verified_item_id"] = verified_item_id
                    add_result["data"]["verification"] = "confirmed" if verified_item_id else "not_found"
                    if not verified_item_id:
                        add_result["data"]["verification_error"] = (
                            "GitHub accepted the add request but the item was not visible on the project after retry."
                        )
                except Exception as verification_exc:
                    add_result.setdefault("data", {})["verification"] = "failed"
                    add_result["data"]["verification_error"] = str(verification_exc)
            data["add_to_project_result"] = add_result

        metadata_results = await _apply_pull_request_metadata(
            repo=repo,
            pull_number=pr_number,
            gh_client=gh_client,
            params=params,
            pull_request_data=pr_data,
        )
        if metadata_results:
            data["metadata_results"] = metadata_results
            data["metadata_status"] = (
                "complete"
                if all(result["outcome"] == "success" for result in metadata_results)
                else "partial"
            )

        return ToolSuccess(data=data).model_dump()

    except ValueError as exc:
        return build_error_response(
            error_type="internal",
            message=str(exc),
            suggestion=(
                "Do not retry blindly: reconcile the PR with list_pull_requests or "
                "get_pull_request_detail before repeating the mutation."
            ),
        )
    except CLIError as exc:
        logger.error("CLI error in create_pull_request: %s", exc)
        error_text = exc.stderr.strip()
        is_validation = "422" in error_text or "validation failed" in error_text.lower()
        return build_error_response(
            error_type="validation" if is_validation else "internal",
            message=f"Failed to create PR: {error_text}",
            suggestion=(
                "GitHub rejected the request; inspect the reported validation detail and reconcile the PR."
                if is_validation else
                "Verify the head branch exists and is pushed, the base branch is correct, and no PR already exists for this head branch."
            ),
        )
    except Exception as exc:
        logger.error("Error in create_pull_request: %s", exc)
        return handle_tool_error(exc, context="Create pull request failed")


async def update_pull_request(params: UpdatePullRequestInput) -> dict:
    """Update a pull request's content and optional GitHub/project metadata.

    Calls `PATCH /repos/{owner}/{repo}/pulls/{pull_number}` (through
    `gh api`) using the scope-locked target. Only supplied content fields are
    sent. Optional metadata includes reviewer requests, labels, assignees,
    milestones, and Project V2 fields. Metadata-only updates read the PR
    instead of sending an empty patch. Partial follow-up results are explicit.

    Args:
        params: Pull request number and content/metadata fields to change.

    Returns:
        ToolSuccess with the PR number, canonical URL, updated fields, and
        per-field metadata outcomes; or a structured error envelope.
    """
    try:
        # Collect only the fields the caller actually supplied.
        updates: dict[str, object] = {}
        if params.title is not None:
            updates["title"] = params.title
        if params.body is not None:
            updates["body"] = params.body
        if params.base is not None:
            updates["base"] = params.base
        if params.state is not None:
            updates["state"] = params.state
        if params.maintainer_can_modify is not None:
            updates["maintainer_can_modify"] = params.maintainer_can_modify

        metadata_requested = any(
            getattr(params, name) is not None
            for name in (
                "reviewers",
                "team_reviewers",
                "labels",
                "assignees",
                "milestone",
                "status",
                "priority",
                "area",
                "work_type",
                "estimate",
                "due_date",
            )
        ) or params.apply_defaults or params.add_to_project

        if not updates and not metadata_requested:
            return build_error_response(
                error_type="validation",
                message="Provide at least one field to update.",
                suggestion=(
                    "Pass at least one PR field, reviewer, label, assignee, "
                    "milestone, Project V2 field, or apply_defaults."
                ),
            )

        if params.state is not None and params.state not in ("open", "closed"):
            return build_error_response(
                error_type="validation",
                message=f"Invalid state '{params.state}'.",
                suggestion="state must be either 'open' or 'closed'.",
            )

        await get_service_factory().ensure_auth()
        repo = resolve_repository_target(params.repository, owner=params.owner, repo=params.repo)
        gh_client = get_service_factory().gh()

        api_args = [
            "api",
            "--method", "PATCH",
            f"repos/{repo}/pulls/{params.pull_number}",
        ]
        for key in ("title", "body", "base", "state"):
            if key in updates:
                api_args.extend(["-f", f"{key}={updates[key]}"])
        if "maintainer_can_modify" in updates:
            api_args.extend([
                "-F",
                f"maintainer_can_modify={'true' if updates['maintainer_can_modify'] else 'false'}",
            ])

        if updates:
            patch_result = await gh_client.run(api_args)
        else:
            patch_result = await gh_client.run([
                "api", f"repos/{repo}/pulls/{params.pull_number}",
            ])
        pr_data = _validate_pull_request_response(
            json.loads(patch_result.stdout), operation="updating"
        )
        pr_number = pr_data["number"]
        pr_url = pr_data["html_url"]

        data: dict = {
            "pr_number": pr_number,
            "pr_url": pr_url,
            "title": pr_data.get("title"),
            "head": (pr_data.get("head") or {}).get("ref"),
            "base": (pr_data.get("base") or {}).get("ref"),
            "draft": pr_data.get("draft"),
            "state": pr_data.get("state"),
            "updated_fields": sorted(updates.keys()),
            "message": f"PR #{pr_number} updated: {pr_url}",
        }

        if params.add_to_project:
            from tools.projects.add_item import (
                AddItemToProjectInput,
                add_item_to_project,
            )

            data["add_to_project_result"] = await add_item_to_project(
                AddItemToProjectInput(issue_or_pr_number=pr_number)
            )

        metadata_results = await _apply_pull_request_metadata(
            repo=repo,
            pull_number=pr_number,
            gh_client=gh_client,
            params=params,
            pull_request_data=pr_data,
        )
        if metadata_results:
            data["metadata_results"] = metadata_results
            data["metadata_status"] = (
                "complete"
                if all(result["outcome"] == "success" for result in metadata_results)
                else "partial"
            )

        return ToolSuccess(data=data).model_dump()

    except ValueError as exc:
        return build_error_response(
            error_type="internal",
            message=str(exc),
            suggestion=(
                "Do not retry blindly: reconcile the PR with list_pull_requests or "
                "get_pull_request_detail before repeating the mutation."
            ),
        )
    except CLIError as exc:
        logger.error("CLI error in update_pull_request: %s", exc)
        error_text = exc.stderr.strip()
        is_validation = "422" in error_text or "validation failed" in error_text.lower()
        return build_error_response(
            error_type="validation" if is_validation else "internal",
            message=f"Failed to update PR: {error_text}",
            suggestion=(
                "GitHub rejected the request; inspect the reported validation detail and reconcile the PR."
                if is_validation else
                "Verify the pull request number exists, the base branch is valid, and the token has pull-request write scope."
            ),
        )
    except Exception as exc:
        logger.error("Error in update_pull_request: %s", exc)
        return handle_tool_error(exc, context="Update pull request failed")


async def bulk_assign(params: BulkAssignInput) -> dict:
    """Assign multiple issues to users and/or milestone at once.

    Args:
        params: Input containing issue numbers, optional assignees and milestone.

    Returns:
        ToolSuccess with results per issue.
    """
    try:
        await get_service_factory().ensure_auth()
        repo = resolve_repository_target(params.repository, owner=params.owner, repo=params.repo)
        gh_client = get_service_factory().gh()

        if not params.assignees and not params.milestone:
            return build_error_response(
                error_type="validation",
                message="Provide at least assignees or milestone to set.",
                suggestion="Include assignees list and/or milestone title.",
            )

        results: list[dict] = []

        for issue_num in params.issue_numbers:
            try:
                args = ["issue", "edit", str(issue_num), "--repo", repo]

                if params.assignees:
                    for assignee in params.assignees:
                        args.extend(["--add-assignee", assignee])

                if params.milestone:
                    args.extend(["--milestone", params.milestone])

                await gh_client.run(args)
                results.append({"issue": issue_num, "status": "updated"})
            except CLIError as exc:
                results.append({"issue": issue_num, "status": "failed", "error": exc.stderr.strip()})

        updated_count = sum(1 for r in results if r["status"] == "updated")

        return ToolSuccess(
            data={
                "results": results,
                "total": len(params.issue_numbers),
                "updated": updated_count,
                "message": f"Updated {updated_count}/{len(params.issue_numbers)} issues.",
            },
        ).model_dump()

    except Exception as exc:
        logger.error("Error in bulk_assign: %s", exc)
        return handle_tool_error(exc, context="Bulk assign failed")
