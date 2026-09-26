"""MCP workflow tools for high-level project management operations.

These tools chain multiple operations into single calls for
efficient project management workflows.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field

from clients.gh_cli_client import CLIError
from core.protocols import GHCLIRunner
from core.config import get_settings
from core.error_handling import build_error_response, handle_tool_error
from models.responses import ToolSuccess
from services.project_service import ProjectService
from core.factory import get_service_factory

logger = logging.getLogger(__name__)


class CompleteIssueInput(BaseModel):
    """Input for complete_issue workflow."""
    issue_number: int = Field(description="Issue number to mark as complete")
    summary: Optional[str] = Field(default=None, description="Optional completion summary comment")


class DailyStandupInput(BaseModel):
    """Input for daily_standup workflow."""
    username: Optional[str] = Field(default=None, description="Filter by username (default: all team)")


class SprintReviewInput(BaseModel):
    """Input for sprint_review workflow."""
    milestone_title: str = Field(description="Sprint milestone to review")


class TriageNewIssuesInput(BaseModel):
    """Input for triage_new_issues workflow."""
    pass


class EscalateOverdueInput(BaseModel):
    """Input for escalate_overdue workflow."""
    pass


class HandoffIssueInput(BaseModel):
    """Input for handoff_issue workflow."""
    issue_number: int = Field(description="Issue to hand off")
    from_user: str = Field(description="Current assignee username")
    to_user: str = Field(description="New assignee username")
    context: Optional[str] = Field(default=None, description="Handoff context/notes")


class CreateEpicInput(BaseModel):
    """Input for create_epic workflow."""
    title: str = Field(description="Epic title")
    body: str = Field(description="Epic description")
    sub_tasks: list[str] = Field(
        default_factory=list,
        max_length=20,
        description="List of sub-task titles to create as NEW sub-issues (maximum 20)",
    )
    link_existing: Optional[list[int]] = Field(
        default=None,
        description="Existing issue numbers to link as sub-issues of the epic (no new issues created)",
    )
    milestone: Optional[str] = Field(default=None, description="Milestone to assign")
    assignee: Optional[str] = Field(default=None, description="Default assignee for all")
    labels: Optional[list[str]] = Field(default=None, description="Labels for all issues")
    status: Optional[str] = Field(
        default=None,
        description=(
            "Project board Status option to set on created items. Must match an option "
            "of the board's Status field. Defaults to the board's first Status option."
        ),
    )
    priority: Optional[str] = Field(
        default=None,
        description="Project board Priority option name (must match the board's Priority field options)",
    )


class CloseSprintInput(BaseModel):
    """Input for close_sprint workflow."""
    milestone_title: str = Field(description="Sprint milestone to close")
    next_milestone: Optional[str] = Field(default=None, description="Next sprint to move pending issues to")


class BlockedReportInput(BaseModel):
    """Input for blocked_report workflow."""
    pass


async def complete_issue(params: CompleteIssueInput) -> dict:
    """Complete an issue: comment summary, close it, project auto-moves to Done.

    Args:
        params: Issue number and optional summary.
    Returns:
        ToolSuccess with completion details.
    """
    try:
        await get_service_factory().ensure_auth()
        settings = get_settings()
        repo = f"{settings.org_name}/{settings.repo_name}"
        gh = get_service_factory().gh()

        comment = params.summary or "✅ Issue completed."
        await gh.run(["issue", "comment", str(params.issue_number), "--repo", repo, "--body", comment])
        await gh.run(["issue", "close", str(params.issue_number), "--repo", repo, "--reason", "completed"])

        return ToolSuccess(data={
            "issue_number": params.issue_number, "status": "completed",
            "message": f"Issue #{params.issue_number} completed and closed.",
        }).model_dump()
    except CLIError as exc:
        return build_error_response("internal", f"Complete failed: {exc.stderr.strip()}", "Verify issue exists.")
    except Exception as exc:
        return handle_tool_error(exc, context="Complete issue workflow failed")


async def daily_standup(params: DailyStandupInput) -> dict:
    """Generate daily standup: done recently, in progress, blocked.

    Args:
        params: Optional username filter.
    Returns:
        ToolSuccess with standup data.
    """
    try:
        await get_service_factory().ensure_auth()
        settings = get_settings()
        repo = f"{settings.org_name}/{settings.repo_name}"
        gh = get_service_factory().gh()

        closed_args = ["issue", "list", "--repo", repo, "--state", "closed",
                       "--limit", "20", "--json", "number,title,assignees,closedAt"]
        if params.username:
            closed_args.extend(["--assignee", params.username])
        closed_result = await gh.run(closed_args)
        all_closed = json.loads(closed_result.stdout)

        now = datetime.now(timezone.utc)
        recent_done = []
        for issue in all_closed:
            if issue.get("closedAt"):
                closed_at = datetime.fromisoformat(issue["closedAt"].replace("Z", "+00:00"))
                if (now - closed_at).total_seconds() < 172800:
                    recent_done.append({"number": issue["number"], "title": issue["title"],
                                        "assignees": [a["login"] for a in issue.get("assignees", [])]})

        open_args = ["issue", "list", "--repo", repo, "--state", "open",
                     "--limit", "50", "--json", "number,title,assignees,labels"]
        if params.username:
            open_args.extend(["--assignee", params.username])
        open_result = await gh.run(open_args)
        all_open = json.loads(open_result.stdout)

        in_progress = [{"number": i["number"], "title": i["title"],
                        "assignees": [a["login"] for a in i.get("assignees", [])]}
                       for i in all_open if i.get("assignees")]

        blocked = [{"number": i["number"], "title": i["title"]}
                   for i in all_open
                   if any("pending" in l.get("name", "").lower() or "blocked" in l.get("name", "").lower()
                          for l in i.get("labels", []))]

        return ToolSuccess(data={
            "date": now.strftime("%Y-%m-%d"),
            "done_recently": recent_done, "done_count": len(recent_done),
            "in_progress": in_progress[:15], "in_progress_count": len(in_progress),
            "blocked": blocked, "blocked_count": len(blocked),
        }).model_dump()
    except Exception as exc:
        return handle_tool_error(exc, context="Daily standup failed")


async def sprint_review(params: SprintReviewInput) -> dict:
    """Sprint review: velocity, per-person stats, pending list.

    Args:
        params: Milestone title.
    Returns:
        ToolSuccess with review data.
    """
    try:
        await get_service_factory().ensure_auth()
        settings = get_settings()
        repo = f"{settings.org_name}/{settings.repo_name}"
        gh = get_service_factory().gh()

        result = await gh.run(["issue", "list", "--repo", repo, "--milestone", params.milestone_title,
                               "--state", "all", "--limit", "100", "--json", "number,title,state,assignees,labels"])
        issues = json.loads(result.stdout)

        completed = [i for i in issues if i.get("state") == "CLOSED"]
        pending = [i for i in issues if i.get("state") == "OPEN"]

        person_stats: dict[str, dict] = {}
        for issue in issues:
            for assignee in issue.get("assignees", []):
                login = assignee.get("login", "unassigned")
                if login not in person_stats:
                    person_stats[login] = {"completed": 0, "pending": 0}
                if issue.get("state") == "CLOSED":
                    person_stats[login]["completed"] += 1
                else:
                    person_stats[login]["pending"] += 1

        total = len(issues)
        progress = round(len(completed) / max(total, 1) * 100)

        return ToolSuccess(data={
            "milestone": params.milestone_title, "total_issues": total,
            "completed": len(completed), "pending": len(pending),
            "progress_pct": progress, "velocity": len(completed),
            "per_person": person_stats,
            "pending_issues": [{"number": i["number"], "title": i["title"]} for i in pending],
        }).model_dump()
    except Exception as exc:
        return handle_tool_error(exc, context="Sprint review failed")


async def triage_new_issues() -> dict:
    """Find untriaged issues (no milestone, no assignee, no labels).

    Returns:
        ToolSuccess with untriaged issues.
    """
    try:
        await get_service_factory().ensure_auth()
        settings = get_settings()
        repo = f"{settings.org_name}/{settings.repo_name}"
        gh = get_service_factory().gh()

        result = await gh.run(["issue", "list", "--repo", repo, "--state", "open",
                               "--limit", "100", "--json", "number,title,labels,milestone,assignees"])
        issues = json.loads(result.stdout)

        untriaged = []
        for issue in issues:
            problems = []
            if not issue.get("milestone"):
                problems.append("no_milestone")
            if not issue.get("assignees"):
                problems.append("no_assignee")
            if not issue.get("labels"):
                problems.append("no_labels")
            if problems:
                untriaged.append({"number": issue["number"], "title": issue["title"], "problems": problems})

        return ToolSuccess(data={
            "untriaged_count": len(untriaged), "issues": untriaged[:30],
            "message": f"Found {len(untriaged)} issues needing triage.",
        }).model_dump()
    except Exception as exc:
        return handle_tool_error(exc, context="Triage failed")


async def escalate_overdue() -> dict:
    """Find issues past their due date that are still open.

    Returns:
        ToolSuccess with overdue issues.
    """
    try:
        await get_service_factory().ensure_auth()
        settings = get_settings()
        repo = f"{settings.org_name}/{settings.repo_name}"
        gh = get_service_factory().gh()

        ms_result = await gh.run(["api", f"repos/{repo}/milestones?state=open&per_page=50"])
        milestones = json.loads(ms_result.stdout)

        now = datetime.now(timezone.utc)
        overdue: list[dict] = []

        for ms in milestones:
            due_on = ms.get("due_on")
            if not due_on:
                continue
            due_date = datetime.fromisoformat(due_on.replace("Z", "+00:00"))
            if due_date < now and ms.get("open_issues", 0) > 0:
                issues_result = await gh.run(["issue", "list", "--repo", repo,
                                              "--milestone", ms["title"], "--state", "open",
                                              "--limit", "50", "--json", "number,title,assignees"])
                issues = json.loads(issues_result.stdout)
                days = (now - due_date).days
                for issue in issues:
                    overdue.append({
                        "number": issue["number"], "title": issue["title"],
                        "milestone": ms["title"], "days_overdue": days,
                        "assignees": [a["login"] for a in issue.get("assignees", [])],
                    })

        return ToolSuccess(data={
            "overdue_count": len(overdue), "issues": overdue,
            "message": f"Found {len(overdue)} overdue issues.",
        }).model_dump()
    except Exception as exc:
        return handle_tool_error(exc, context="Escalate overdue failed")


async def handoff_issue(params: HandoffIssueInput) -> dict:
    """Hand off issue from one person to another with context comment.

    Args:
        params: Issue, from/to users, context.
    Returns:
        ToolSuccess on success.
    """
    try:
        await get_service_factory().ensure_auth()
        settings = get_settings()
        repo = f"{settings.org_name}/{settings.repo_name}"
        gh = get_service_factory().gh()

        await gh.run(["issue", "edit", str(params.issue_number), "--repo", repo,
                      "--remove-assignee", params.from_user, "--add-assignee", params.to_user])

        context = params.context or "No additional context."
        comment = f"🔄 **Handoff:** @{params.from_user} → @{params.to_user}\n\n**Context:** {context}"
        await gh.run(["issue", "comment", str(params.issue_number), "--repo", repo, "--body", comment])

        return ToolSuccess(data={
            "issue_number": params.issue_number, "from": params.from_user, "to": params.to_user,
            "message": f"Issue #{params.issue_number} handed off to @{params.to_user}.",
        }).model_dump()
    except CLIError as exc:
        return build_error_response("internal", f"Handoff failed: {exc.stderr.strip()}", "Verify usernames.")
    except Exception as exc:
        return handle_tool_error(exc, context="Handoff failed")


# ── Project Board Field Defaults (resolved at runtime) ───────────────────────
#
# Project/field/option IDs are NOT hardcoded: they are discovered at runtime
# from the GH_PROJECT_* context via DiscoveryService (mirroring
# create_project_item / discover.py), so create_epic works against any board.


def _default_status(metadata) -> str | None:
    """Return a sensible default Status option name for the board.

    Uses the first option of the board's Status single-select field, so the
    epic lands in the board's initial column without assuming any
    project-specific label. Returns None if the board has no Status field
    or no options (in which case Status is simply left unset).

    Args:
        metadata: Discovered ProjectMetadata for the target board.

    Returns:
        The first Status option name, or None.
    """
    status_field = metadata.fields.get("Status")
    if status_field and status_field.options:
        return status_field.options[0].name
    return None


async def _set_item_fields(
    project_service: ProjectService,
    metadata,
    item_id: str,
    *,
    status: str | None,
    priority: str | None,
) -> None:
    """Set Status/Priority on a project item using runtime-resolved field IDs.

    Field and option IDs are resolved from the discovered board metadata by
    ProjectService.update_field (single-select values are matched by option
    name). Failures are logged and swallowed so a single unsettable field
    never aborts the whole epic creation.

    Args:
        project_service: Project service bound to the discovered board.
        metadata: Discovered ProjectMetadata.
        item_id: Project item node ID to update.
        status: Status option name to set (optional).
        priority: Priority option name to set (optional).
    """
    if status:
        try:
            await project_service.update_field(
                metadata=metadata, item_id=item_id,
                field_name="Status", value=status,
            )
        except Exception as exc:
            logger.warning("Failed to set Status on item %s: %s", item_id, exc)

    if priority:
        try:
            await project_service.update_field(
                metadata=metadata, item_id=item_id,
                field_name="Priority", value=priority,
            )
        except Exception as exc:
            logger.warning("Failed to set Priority on item %s: %s", item_id, exc)


async def _link_sub_issue(gh: GHCLIRunner, parent_node_id: str, sub_node_id: str) -> None:
    """Link a child issue as a sub-issue of the parent via the GraphQL API.

    Args:
        gh: Authenticated GH CLI client.
        parent_node_id: Parent (epic) issue node ID.
        sub_node_id: Child issue node ID.
    """
    if not (parent_node_id and sub_node_id):
        return
    mutation = """
    mutation AddSubIssue($issueId: ID!, $subIssueId: ID!) {
      addSubIssue(input: {issueId: $issueId, subIssueId: $subIssueId}) {
        issue { number }
      }
    }
    """
    try:
        await gh.api_graphql(
            mutation,
            {"issueId": parent_node_id, "subIssueId": sub_node_id},
        )
    except Exception as exc:
        logger.warning("Failed to link sub-issue: %s", exc)


async def create_epic(params: CreateEpicInput) -> dict:
    """Create an epic (parent issue) and link its sub-issues in one call.

    Sub-issues can be created from ``sub_tasks`` titles and/or linked from
    ``link_existing`` issue numbers. All project/field/option IDs are resolved
    at runtime from the GH_PROJECT_* context (via DiscoveryService), so the
    tool works against any board — no hardcoded board IDs or status labels.

    Args:
        params: Epic details, sub-task titles, and/or existing issue numbers.
    Returns:
        ToolSuccess with created/linked issue numbers.
    """
    try:
        settings = get_settings()
        repo = f"{settings.org_name}/{settings.repo_name}"
        gh = get_service_factory().gh()

        graphql_client = await get_service_factory().graphql()
        discovery_service = await get_service_factory().discovery_service(graphql_client)
        project_service = await get_service_factory().project_service(graphql_client, gh)

        # Resolve the board metadata (project id, field ids, option ids) once.
        try:
            metadata = await discovery_service.get_cached_or_discover()
        except Exception as exc:
            logger.warning("Discovery failed in create_epic (fields will be skipped): %s", exc)
            metadata = None

        # Choose the Status option: caller value, else the board's first option.
        status_to_set = params.status
        if status_to_set is None and metadata is not None:
            status_to_set = _default_status(metadata)

        # ── Create the parent epic issue ─────────────────────────────────────
        parent_args = ["issue", "create", "--repo", repo, "--title", params.title, "--body", params.body]
        if params.milestone:
            parent_args.extend(["--milestone", params.milestone])
        if params.assignee:
            parent_args.extend(["--assignee", params.assignee])
        if params.labels:
            for label in params.labels:
                parent_args.extend(["--label", label])

        parent_result = await gh.run(parent_args)
        parent_url = parent_result.stdout.strip()
        parent_number = int(parent_url.rstrip("/").split("/")[-1])

        # Add the epic to the board and set its fields at runtime.
        if metadata is not None:
            try:
                parent_item_id = await project_service.add_item(
                    metadata=metadata, issue_number=parent_number,
                )
                if parent_item_id:
                    await _set_item_fields(
                        project_service, metadata, parent_item_id,
                        status=status_to_set, priority=params.priority,
                    )
            except Exception as exc:
                logger.warning(
                    "Could not add/set board fields for epic #%d: %s", parent_number, exc
                )

        parent_node = await gh.run([
            "api", f"repos/{repo}/issues/{parent_number}", "--jq", ".node_id",
        ])
        parent_node_id = parent_node.stdout.strip()

        # ── Create NEW sub-issues from titles ────────────────────────────────
        sub_issues: list[dict] = []
        for task_title in params.sub_tasks:
            sub_args = ["issue", "create", "--repo", repo, "--title", task_title,
                        "--body", f"Parent: #{parent_number}"]
            if params.milestone:
                sub_args.extend(["--milestone", params.milestone])
            if params.assignee:
                sub_args.extend(["--assignee", params.assignee])
            if params.labels:
                for label in params.labels:
                    sub_args.extend(["--label", label])

            sub_result = await gh.run(sub_args)
            sub_url = sub_result.stdout.strip()
            sub_number = int(sub_url.rstrip("/").split("/")[-1])

            if metadata is not None:
                try:
                    sub_item_id = await project_service.add_item(
                        metadata=metadata, issue_number=sub_number,
                    )
                    if sub_item_id:
                        await _set_item_fields(
                            project_service, metadata, sub_item_id,
                            status=status_to_set, priority=params.priority,
                        )
                except Exception as exc:
                    logger.warning(
                        "Could not add/set board fields for sub-issue #%d: %s", sub_number, exc
                    )

            sub_node = await gh.run([
                "api", f"repos/{repo}/issues/{sub_number}", "--jq", ".node_id",
            ])
            await _link_sub_issue(gh, parent_node_id, sub_node.stdout.strip())
            sub_issues.append({"number": sub_number, "title": task_title, "created": True})

        # ── Link EXISTING issues as sub-issues ───────────────────────────────
        linked_existing: list[dict] = []
        for existing_number in (params.link_existing or []):
            try:
                existing_node = await gh.run([
                    "api", f"repos/{repo}/issues/{existing_number}", "--jq", ".node_id",
                ])
                await _link_sub_issue(gh, parent_node_id, existing_node.stdout.strip())
                linked_existing.append({"number": existing_number, "linked": True})
            except CLIError as exc:
                logger.warning("Could not link existing issue #%d: %s", existing_number, exc.stderr)
                linked_existing.append({"number": existing_number, "linked": False})

        return ToolSuccess(data={
            "parent_issue": parent_number, "parent_url": parent_url,
            "sub_issues": sub_issues, "sub_issues_count": len(sub_issues),
            "linked_existing": linked_existing, "linked_existing_count": len(linked_existing),
            "status_applied": status_to_set,
            "message": (
                f"Epic #{parent_number} created with {len(sub_issues)} new sub-task(s) "
                f"and {len(linked_existing)} linked existing issue(s)."
            ),
        }).model_dump()
    except CLIError as exc:
        return build_error_response("internal", f"Create epic failed: {exc.stderr.strip()}", "Check inputs.")
    except Exception as exc:
        return handle_tool_error(exc, context="Create epic failed")


async def close_sprint(params: CloseSprintInput) -> dict:
    """Close sprint: close milestone, move pending to next, generate summary.

    Args:
        params: Sprint to close, optional next sprint.
    Returns:
        ToolSuccess with closure details.
    """
    try:
        await get_service_factory().ensure_auth()
        settings = get_settings()
        repo = f"{settings.org_name}/{settings.repo_name}"
        gh = get_service_factory().gh()

        issues_result = await gh.run(["issue", "list", "--repo", repo, "--milestone", params.milestone_title,
                                      "--state", "all", "--limit", "100", "--json", "number,title,state"])
        issues = json.loads(issues_result.stdout)

        completed = [i for i in issues if i.get("state") == "CLOSED"]
        pending = [i for i in issues if i.get("state") == "OPEN"]

        moved: list[dict] = []
        if params.next_milestone and pending:
            for issue in pending:
                try:
                    await gh.run(["issue", "edit", str(issue["number"]), "--repo", repo,
                                  "--milestone", params.next_milestone])
                    moved.append({"number": issue["number"], "title": issue["title"]})
                except CLIError:
                    pass

        ms_result = await gh.run(["api", f"repos/{repo}/milestones?state=open&per_page=50"])
        for ms in json.loads(ms_result.stdout):
            if ms.get("title") == params.milestone_title:
                await gh.run(["api", f"repos/{repo}/milestones/{ms['number']}",
                              "--method", "PATCH", "-f", "state=closed"])
                break

        progress = round(len(completed) / max(len(issues), 1) * 100)
        return ToolSuccess(data={
            "milestone": params.milestone_title, "total": len(issues),
            "completed": len(completed), "pending_moved": len(moved),
            "moved_to": params.next_milestone, "completion_pct": progress,
            "moved_issues": moved,
            "message": f"Sprint closed. {len(completed)}/{len(issues)} done ({progress}%). {len(moved)} moved.",
        }).model_dump()
    except Exception as exc:
        return handle_tool_error(exc, context="Close sprint failed")


async def blocked_report() -> dict:
    """Report blocked/pending issues with dependency info.

    Returns:
        ToolSuccess with blocked issues.
    """
    try:
        import re
        await get_service_factory().ensure_auth()
        settings = get_settings()
        repo = f"{settings.org_name}/{settings.repo_name}"
        gh = get_service_factory().gh()

        result = await gh.run(["issue", "list", "--repo", repo, "--state", "open",
                               "--limit", "100", "--json", "number,title,body,assignees,labels,milestone"])
        issues = json.loads(result.stdout)

        blocked: list[dict] = []
        for issue in issues:
            body = issue.get("body", "") or ""
            labels = [l.get("name", "") for l in issue.get("labels", [])]
            is_blocked = (any("blocked" in l.lower() or "pending" in l.lower() for l in labels)
                          or "depends on" in body.lower() or "blocked by" in body.lower())
            if is_blocked:
                dep = None
                match = re.search(r"(?:depends on|blocked by) #(\d+)", body, re.IGNORECASE)
                if match:
                    dep = int(match.group(1))
                blocked.append({
                    "number": issue["number"], "title": issue["title"],
                    "assignees": [a["login"] for a in issue.get("assignees", [])],
                    "milestone": issue.get("milestone", {}).get("title") if issue.get("milestone") else None,
                    "dependency": dep,
                })

        return ToolSuccess(data={
            "blocked_count": len(blocked), "issues": blocked,
            "message": f"Found {len(blocked)} blocked/pending issues.",
        }).model_dump()
    except Exception as exc:
        return handle_tool_error(exc, context="Blocked report failed")
