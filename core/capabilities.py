"""
MCP Tool Capabilities — programmatic mapping for permission enforcement.

Each tool is mapped to its minimum required capability set. The
validate_capabilities() helper checks whether a granted set covers a tool's
requirements, enabling future middleware to reject calls before they hit the
GitHub API.
"""

from __future__ import annotations

from enum import Enum
from typing import FrozenSet


class Capability(str, Enum):
    """Permission capabilities for MCP tools."""

    ISSUES_READ = "issues.read"
    ISSUES_WRITE = "issues.write"
    COMMENTS_READ = "comments.read"
    COMMENTS_WRITE = "comments.write"
    PROJECTS_READ = "projects.read"
    PROJECTS_WRITE = "projects.write"
    PLANNING_READ = "planning.read"
    PLANNING_WRITE = "planning.write"
    LABELS_READ = "labels.read"
    LABELS_WRITE = "labels.write"
    PULL_REQUESTS_READ = "pull_requests.read"
    PULL_REQUESTS_WRITE = "pull_requests.write"
    ACTIONS_READ = "actions.read"
    ACTIONS_WRITE = "actions.write"
    CONTENTS_READ = "contents.read"
    CONTENTS_WRITE = "contents.write"


# Shorthand aliases for readability
IR = Capability.ISSUES_READ
IW = Capability.ISSUES_WRITE
CR = Capability.COMMENTS_READ
CW = Capability.COMMENTS_WRITE
PR = Capability.PROJECTS_READ
PW = Capability.PROJECTS_WRITE
PLR = Capability.PLANNING_READ
PLW = Capability.PLANNING_WRITE
LR = Capability.LABELS_READ
LW = Capability.LABELS_WRITE
PRR = Capability.PULL_REQUESTS_READ
PRW = Capability.PULL_REQUESTS_WRITE
AR = Capability.ACTIONS_READ
AW = Capability.ACTIONS_WRITE
CTR = Capability.CONTENTS_READ
CTW = Capability.CONTENTS_WRITE

# ---------------------------------------------------------------------------
# Tool → Required Capabilities mapping
# Keys are the async function names as registered with the MCP server.
# ---------------------------------------------------------------------------

TOOL_CAPABILITIES: dict[str, FrozenSet[Capability]] = {
    # --- Discovery ---
    "discover_ids": frozenset({PR}),

    # --- Issue Read ---
    "get_issue_detail": frozenset({IR, CR}),
    "list_sub_issues": frozenset({IR}),
    "search_issues": frozenset({IR}),
    "list_project_items": frozenset({PR, IR}),
    "summarize_issue": frozenset({IR}),
    "detect_issue_duplicates": frozenset({IR}),
    "find_stale_issues": frozenset({PR, IR}),
    "find_unassigned_issues": frozenset({PR, IR}),
    "find_missing_issue_metadata": frozenset({PR, IR}),
    "list_issue_comments": frozenset({IR, CR}),
    "search_issue_comments": frozenset({IR, CR}),

    # --- Issue Write ---
    "create_project_item": frozenset({IW, PW}),
    "edit_issue": frozenset({IW}),
    "close_issue": frozenset({IW}),
    "reopen_issue": frozenset({IW}),
    "add_sub_issue": frozenset({IW}),
    "remove_sub_issue": frozenset({IW}),
    "bulk_close_issues": frozenset({IW}),
    "bulk_update_items": frozenset({IW, PW}),
    "complete_issue": frozenset({IW, PW, CW}),
    # auto_triage_issue only suggests labels (dry run): read-only (#37).
    "auto_triage_issue": frozenset({IR, LR}),

    # --- Comments ---
    "comment_issue": frozenset({CW}),
    "comment_issue_progress": frozenset({CW}),
    "comment_issue_plan": frozenset({CW}),
    "comment_issue_blocker": frozenset({CW}),
    "comment_issue_resolution": frozenset({CW}),
    "edit_issue_comment": frozenset({CW}),

    # --- Project Board ---
    "update_project_item_fields": frozenset({PW}),
    "add_item_to_project": frozenset({PW}),
    "move_to_status": frozenset({PW}),
    "move_to_done": frozenset({PW}),
    "move_to_trash": frozenset({PW}),
    "archive_project_item": frozenset({PW}),

    # --- Plan-only (dry run) board/issue helpers ---
    # These return a plan with "dry_run": true and never mutate anything, so
    # they only need read capabilities and are exposed at MCP_ACCESS_LEVEL=read
    # (docs audit, issue #37). Apply a plan with the real write tools.
    "project_sync_issue_metadata": frozenset({PR, IR}),
    "project_set_default_status": frozenset({PR}),
    "project_set_default_priority": frozenset({PR}),
    "project_bulk_status_by_filter": frozenset({PR}),
    "project_bulk_priority_by_filter": frozenset({PR}),
    "project_bulk_due_date_by_filter": frozenset({PR}),
    "project_import_markdown": frozenset(),

    # --- Project Reports (Read-Only) ---
    "get_project_stats": frozenset({PR, IR}),
    "get_sprint_summary": frozenset({PR, PLR}),
    "project_health_report": frozenset({PR, IR}),
    "project_status_distribution": frozenset({PR}),
    "project_priority_distribution": frozenset({PR}),
    "project_assignee_load": frozenset({PR, IR}),
    "project_due_date_risk": frozenset({PR}),
    "project_cycle_time": frozenset({PR}),
    "project_orphan_items": frozenset({PR}),
    "project_missing_fields": frozenset({PR}),
    "project_field_options_report": frozenset({PR}),
    "project_validate_board": frozenset({PR}),
    "project_export_markdown": frozenset({PR}),

    # --- Planning & Sprint ---
    "sprint_planning": frozenset({PLR, PR, IR}),
    "generate_release_notes": frozenset({PLR, IR}),
    "plan_next_sprint": frozenset({PLR, PR}),
    "prioritize_backlog": frozenset({PR, IR}),
    "generate_daily_plan": frozenset({PLR, PR}),
    "generate_weekly_plan": frozenset({PLR, PR}),
    "generate_risk_register": frozenset({PLR, PR}),
    "generate_dependency_report": frozenset({PR, IR}),
    "generate_release_checklist": frozenset({PLR, PR}),
    "generate_changelog_from_issues": frozenset({PLR, IR}),
    "generate_project_brief": frozenset({PR, IR}),
    "generate_stakeholder_update": frozenset({PLR, PR}),
    "detect_scope_creep": frozenset({PLR, PR}),
    "detect_blocked_work": frozenset({PR, IR}),
    "recommend_wip_moves": frozenset({PR, IR}),
    "recommend_sprint_assignment": frozenset({PLR, PR}),
    "close_sprint": frozenset({PLW, PW, IW}),

    # --- Labels & Milestones ---
    "list_labels": frozenset({LR}),
    "create_label": frozenset({LW}),
    "list_milestones": frozenset({PLR}),
    "create_milestone": frozenset({PLW}),
    "close_milestone": frozenset({PLW}),
    "set_estimate": frozenset({PW}),
    "suggest_issue_labels": frozenset({IR, LR}),

    # --- PR / Issue Lifecycle ---
    "verify_acceptance_criteria": frozenset({IR, PRR}),
    "get_pr_linked_issues": frozenset({PRR, IR}),
    "validate_issue_closure_readiness": frozenset({IR, PRR, CR}),
    "close_issue_on_pr_merge": frozenset({IW, PRR, CW}),
    "sync_closed_items_to_done": frozenset({PR, PW}),
    "link_pull_request": frozenset({PRW, IW}),
    "create_pull_request": frozenset({PRW, IW}),
    "update_pull_request": frozenset({PRW, IW}),

    # --- GitHub Actions / checks (issue #32) ---
    "list_workflows": frozenset({AR}),
    "list_workflow_runs": frozenset({AR}),
    "get_workflow_run": frozenset({AR}),
    "get_pr_checks": frozenset({AR, PRR}),
    "get_job_logs": frozenset({AR}),
    "rerun_workflow_run": frozenset({AW}),
    "dispatch_workflow": frozenset({AW}),

    # --- Response quality and automation diagnostics ---
    "repository_health_summary": frozenset({IR}),
    "pull_request_lifecycle_summary": frozenset({PRR}),
    "issue_activity_digest": frozenset({IR, CR}),
    "workflow_run_diagnostic_summary": frozenset({AR}),
    "check_conclusion_summary": frozenset({AR, PRR}),
    "label_milestone_consistency_report": frozenset({IR, LR, PLR}),
    "issue_metadata_consistency_report": frozenset({IR}),
    "issue_closure_readiness_report": frozenset({IR, PRR}),
    "paginated_issue_page": frozenset({IR}),
    "response_diagnostics": frozenset(),

    # --- Repository contents and pull request listing ---
    "list_repository_directory": frozenset({CTR}),
    "get_repository_file": frozenset({CTR}),
    "search_repository_code": frozenset({CTR}),
    "list_pull_requests": frozenset({PRR}),
    "get_pull_request_detail": frozenset({PRR}),

    # --- Governed commits (branch + path fenced) ---
    "create_branch": frozenset({CTW}),
    "commit_files": frozenset({CTW}),

    # --- Permanent delete (issue #34) — exposed only at MCP_ACCESS_LEVEL=full ---
    "delete_project_item": frozenset({PW}),
    "delete_issue": frozenset({IW}),
    "delete_issue_comment": frozenset({CW}),
    "delete_label": frozenset({LW}),
    "delete_milestone": frozenset({PLW}),

    # --- Diagnostics ---
    "server_info": frozenset(),

    # --- Repository & Project provisioning ---
    "create_repository": frozenset({IW}),
    "create_project": frozenset({PW}),
    "update_project": frozenset({PW}),
    "create_project_field": frozenset({PW}),
    "link_repository": frozenset({PW}),
    "list_projects": frozenset({PR}),

    # --- Board structure (issue #26) ---
    "set_field_options": frozenset({PW}),
    "list_project_views": frozenset({PR}),
    "create_project_view": frozenset({PW}),

    # --- Workflow Orchestration ---
    "daily_standup": frozenset({PR, IR, CW}),
    "sprint_review": frozenset({PR, PLR, IR}),
    "triage_new_issues": frozenset({IR, IW, PW}),
    "escalate_overdue": frozenset({PR, IR, CW}),
    "handoff_issue": frozenset({IW, CW, PW}),
    "create_epic": frozenset({IW, PW, CW}),
    "blocked_report": frozenset({PR, IR}),
    "bulk_assign": frozenset({IW}),

    # --- Text Processing (no API calls) ---
    "validate_issue_markdown": frozenset(),
    "format_issue_markdown": frozenset(),
    "add_issue_acceptance_criteria": frozenset(),
    "normalize_issue_title": frozenset(),
    "suggest_issue_assignee": frozenset({IR}),
    "build_issue_template": frozenset(),
    "build_closure_comment": frozenset(),
    "build_dependency_comment": frozenset(),
    "build_issue_bundle": frozenset({IR}),
    "build_status_update_comment": frozenset(),
    "build_sprint_retrospective": frozenset({PLR, PR}),
    "build_roadmap_markdown": frozenset({PLR, PR}),
    "build_issue_creation_bundle": frozenset(),
    "build_issue_review_checklist": frozenset({IR}),
    "build_comment_digest": frozenset({IR, CR}),
    "build_automation_decision": frozenset(),
}


# ---------------------------------------------------------------------------
# Predefined profiles — named sets of capabilities for common use cases
# ---------------------------------------------------------------------------

PROFILES: dict[str, FrozenSet[Capability]] = {
    "read_only": frozenset({IR, CR, PR, PLR, LR, PRR, CTR}),
    "commenter": frozenset({IR, CR, CW}),
    "project_manager": frozenset(Capability),
    "developer": frozenset({IR, IW, CR, CW, PR, PW, PLR, LR, PRR, PRW, CTR, CTW}),
}


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def validate_capabilities(
    tool_name: str,
    granted: FrozenSet[Capability] | set[Capability],
) -> tuple[bool, FrozenSet[Capability]]:
    """
    Check whether `granted` capabilities cover the requirements for `tool_name`.

    Returns:
        (is_allowed, missing) — `is_allowed` is True when all requirements are
        met; `missing` contains the unsatisfied capabilities (empty on success).

    Raises:
        KeyError: if `tool_name` is not in the capabilities registry.
    """
    required = TOOL_CAPABILITIES[tool_name]
    missing = required - frozenset(granted)
    return (len(missing) == 0, missing)


def get_tools_for_capabilities(
    granted: FrozenSet[Capability] | set[Capability],
) -> list[str]:
    """Return all tool names whose requirements are fully covered by `granted`."""
    granted_fs = frozenset(granted)
    return [
        name
        for name, required in TOOL_CAPABILITIES.items()
        if required <= granted_fs
    ]


def get_required_capabilities(tool_name: str) -> FrozenSet[Capability]:
    """Return the capability set required by a tool. Raises KeyError if unknown."""
    return TOOL_CAPABILITIES[tool_name]
