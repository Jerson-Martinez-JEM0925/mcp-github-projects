# MCP Tool Capabilities Matrix

This document defines the minimum permissions required for each MCP tool,
organized by capability domain. Use this to configure least-privilege tokens.

## Access levels & scope lock

Two server-enforced switches sit ABOVE the per-tool capability model below.
They narrow what a client can do regardless of what the token allows.

### `MCP_ACCESS_LEVEL` — which tools are registered

| Level | Registered tools | Permanent-delete tools |
|-------|------------------|------------------------|
| `read` | read-only tools only | not registered |
| `write` *(default)* | read + write (create/update/close/archive/move) | not registered |
| `full` | every tool | registered, each requires `confirm:true` |

A tool hidden at the active level is **not registered** — the client cannot see
or call it (`server_info.tool_exposure` reports how many are hidden, including
`hidden_delete_tools`). The variable is **`MCP_`-prefixed** (not `GH_PROJECT_`),
matching the write-policy variable convention in `mcp-monday-projects`.

The five permanent-delete tools (`delete_project_item`, `delete_issue`,
`delete_issue_comment`, `delete_label`, `delete_milestone`) are the only tools
classified *delete*. They appear ONLY at `full`, and each refuses unless called
with `confirm:true`, naming the reversible alternative in its refusal.

### `GH_PROJECT_SCOPE_LOCK` — target confinement

| Value | Effect |
|-------|--------|
| `false` *(default)* | The server is as wide as the token allows. |
| `true` | Every tool is confined to the configured `GH_PROJECT_ORG_NAME` / `GH_PROJECT_REPO_NAME` / `GH_PROJECT_PROJECT_NUMBER`. A call naming any other owner/repo/project is refused with a typed error naming `GH_PROJECT_SCOPE_LOCK`, before any mutation. Creating repositories / new projects is disabled while the lock is on. |

This is the parity concept with `mcp-monday-projects`' `MONDAY_WORKSPACE_ID`.
`server_info` reports both the level and the scope lock at runtime.

### Recommended presets

| Profile | `MCP_ACCESS_LEVEL` | `GH_PROJECT_SCOPE_LOCK` |
|---------|--------------------|-------------------------|
| Analyst / reporting | `read` | `true` |
| Day-to-day automation | `write` | `true` |
| Maintenance (incl. deletes) | `full` | `false` |

---

## Capability Definitions

| Capability | Description |
|------------|-------------|
| `issues.read` | Read issue titles, bodies, labels, assignees, milestones |
| `issues.write` | Create, edit, close, reopen issues; manage sub-issues |
| `comments.read` | Read issue/PR comments |
| `comments.write` | Create, edit, delete issue/PR comments |
| `projects.read` | Read project board items, fields, statuses |
| `projects.write` | Create/update/archive project items; modify field values |
| `planning.read` | Read sprint/milestone data for reports and summaries |
| `planning.write` | Create milestones, manage sprints, modify planning artifacts |
| `labels.read` | List repository labels |
| `labels.write` | Create and modify repository labels |
| `pull_requests.read` | Read PR details, linked issues, merge status |
| `pull_requests.write` | Link PRs, close issues on merge |
| `actions.read` | List workflows and runs, read check runs, commit statuses and job logs |
| `actions.write` | Re-run workflow runs and dispatch `workflow_dispatch` workflows |
| `repositories.write` | Create repositories for a user or organization |

---

## Token Scope Mapping

### Classic Personal Access Token (PAT)

| Capability | Required Scope |
|------------|---------------|
| `issues.read` | `repo` |
| `issues.write` | `repo` |
| `comments.read` | `repo` |
| `comments.write` | `repo` |
| `projects.read` | `read:project` |
| `projects.write` | `project` |
| `planning.read` | `repo` |
| `planning.write` | `repo` |
| `labels.read` | `repo` |
| `labels.write` | `repo` |
| `pull_requests.read` | `repo` |
| `pull_requests.write` | `repo` |
| `actions.read` | `repo` |
| `actions.write` | `repo`, `workflow` |

**Minimum classic PAT for full MCP access:** `repo`, `project`, `read:org`

### Fine-Grained Personal Access Token

| Capability | Repository Permission | Organization Permission |
|------------|----------------------|------------------------|
| `issues.read` | Issues: Read | — |
| `issues.write` | Issues: Read and write | — |
| `comments.read` | Issues: Read | — |
| `comments.write` | Issues: Read and write | — |
| `projects.read` | — | Projects: Read |
| `projects.write` | — | Projects: Read and write |
| `planning.read` | Issues: Read | — |
| `planning.write` | Issues: Read and write | — |
| `labels.read` | Issues: Read | — |
| `labels.write` | Issues: Read and write | — |
| `pull_requests.read` | Pull requests: Read | — |
| `pull_requests.write` | Pull requests: Read and write | — |
| `actions.read` | Actions: Read, Checks: Read, Commit statuses: Read, Pull requests: Read | — |
| `actions.write` | Actions: Read and write | — |

---

## Example Profiles

### Read-Only Analyst
- Classic: `repo` (read), `read:project`
- Fine-Grained: Issues(Read), Pull requests(Read), Org Projects(Read)
- Capabilities: `issues.read`, `comments.read`, `projects.read`, `planning.read`, `labels.read`, `pull_requests.read`

### Project Manager (full)
- Classic: `repo`, `project`, `read:org`
- Fine-Grained: Issues(Read+Write), Pull requests(Read+Write), Org Projects(Read+Write)
- Capabilities: All

### Issue Commenter
- Classic: `repo`
- Fine-Grained: Issues(Read+Write)
- Capabilities: `issues.read`, `comments.read`, `comments.write`

---

## Full Tool × Capability Matrix

### Discovery & Configuration

| Tool | Capabilities | Module |
|------|-------------|--------|
| `discover_ids` | `projects.read` | discover.py |

### Issue Read Operations

| Tool | Capabilities | Module |
|------|-------------|--------|
| `get_issue_detail` | `issues.read`, `comments.read` | advanced_operations.py |
| `list_sub_issues` | `issues.read` | advanced_operations.py |
| `search_issues` | `issues.read` | bulk_operations.py |
| `list_project_items` | `projects.read`, `issues.read` | list_items.py |
| `summarize_issue` | `issues.read` | capability_suite.py |
| `detect_issue_duplicates` | `issues.read` | capability_suite.py |
| `find_stale_issues` | `projects.read`, `issues.read` | capability_suite.py |
| `find_unassigned_issues` | `projects.read`, `issues.read` | capability_suite.py |
| `find_missing_issue_metadata` | `projects.read`, `issues.read` | capability_suite.py |
| `list_issue_comments` | `issues.read`, `comments.read` | capability_suite.py |
| `search_issue_comments` | `issues.read`, `comments.read` | capability_suite.py |

### Issue Write Operations

| Tool | Capabilities | Module |
|------|-------------|--------|
| `create_project_item` | `issues.write`, `projects.write` | create_item.py |
| `edit_issue` | `issues.write` | edit_issue.py |
| `close_issue` | `issues.write` | close.py |
| `reopen_issue` | `issues.write` | advanced_operations.py |
| `add_sub_issue` | `issues.write` | add_sub_issue.py |
| `remove_sub_issue` | `issues.write` | advanced_operations.py |
| `bulk_close_issues` | `issues.write` | bulk_operations.py |
| `bulk_update_items` | `issues.write`, `projects.write` | advanced_operations.py |
| `complete_issue` | `issues.write`, `projects.write`, `comments.write` | workflows.py |
| `auto_triage_issue` | `issues.write`, `projects.write` | capability_suite.py |

### Comment Operations

| Tool | Capabilities | Module |
|------|-------------|--------|
| `comment_issue` | `comments.write` | comment_issue.py |
| `comment_issue_progress` | `comments.write` | capability_suite.py |
| `comment_issue_plan` | `comments.write` | capability_suite.py |
| `comment_issue_blocker` | `comments.write` | capability_suite.py |
| `comment_issue_resolution` | `comments.write` | capability_suite.py |
| `edit_issue_comment` | `comments.write` | capability_suite.py |

### Project Board Operations

| Tool | Capabilities | Module |
|------|-------------|--------|
| `update_project_item_fields` | `projects.write` | update_fields.py |
| `move_to_status` | `projects.write` | advanced_operations.py |
| `move_to_done` | `projects.write` | archive.py |
| `move_to_trash` | `projects.write` | archive.py |
| `archive_project_item` | `projects.write` | archive.py |
| `project_sync_issue_metadata` | `projects.write`, `issues.write` | capability_suite.py |
| `project_set_default_status` | `projects.write` | capability_suite.py |
| `project_set_default_priority` | `projects.write` | capability_suite.py |
| `project_bulk_status_by_filter` | `projects.write` | capability_suite.py |
| `project_bulk_priority_by_filter` | `projects.write` | capability_suite.py |
| `project_bulk_due_date_by_filter` | `projects.write` | capability_suite.py |
| `project_import_markdown` | `projects.write`, `issues.write` | capability_suite.py |

### Project Report / Analytics (Read-Only)

| Tool | Capabilities | Module |
|------|-------------|--------|
| `get_project_stats` | `projects.read`, `issues.read` | nice_to_have.py |
| `get_sprint_summary` | `projects.read`, `planning.read` | nice_to_have.py |
| `project_health_report` | `projects.read`, `issues.read` | capability_suite.py |
| `project_status_distribution` | `projects.read` | capability_suite.py |
| `project_priority_distribution` | `projects.read` | capability_suite.py |
| `project_assignee_load` | `projects.read`, `issues.read` | capability_suite.py |
| `project_due_date_risk` | `projects.read` | capability_suite.py |
| `project_cycle_time` | `projects.read` | capability_suite.py |
| `project_orphan_items` | `projects.read` | capability_suite.py |
| `project_missing_fields` | `projects.read` | capability_suite.py |
| `project_field_options_report` | `projects.read` | capability_suite.py |
| `project_validate_board` | `projects.read` | capability_suite.py |
| `project_export_markdown` | `projects.read` | capability_suite.py |

### Planning & Sprint Operations

| Tool | Capabilities | Module |
|------|-------------|--------|
| `sprint_planning` | `planning.read`, `projects.read`, `issues.read` | planning.py |
| `generate_release_notes` | `planning.read`, `issues.read` | planning.py |
| `plan_next_sprint` | `planning.read`, `projects.read` | capability_suite.py |
| `prioritize_backlog` | `projects.read`, `issues.read` | capability_suite.py |
| `generate_daily_plan` | `planning.read`, `projects.read` | capability_suite.py |
| `generate_weekly_plan` | `planning.read`, `projects.read` | capability_suite.py |
| `generate_risk_register` | `planning.read`, `projects.read` | capability_suite.py |
| `generate_dependency_report` | `projects.read`, `issues.read` | capability_suite.py |
| `generate_release_checklist` | `planning.read`, `projects.read` | capability_suite.py |
| `generate_changelog_from_issues` | `planning.read`, `issues.read` | capability_suite.py |
| `generate_project_brief` | `projects.read`, `issues.read` | capability_suite.py |
| `generate_stakeholder_update` | `planning.read`, `projects.read` | capability_suite.py |
| `detect_scope_creep` | `planning.read`, `projects.read` | capability_suite.py |
| `detect_blocked_work` | `projects.read`, `issues.read` | capability_suite.py |
| `recommend_wip_moves` | `projects.read`, `issues.read` | capability_suite.py |
| `recommend_sprint_assignment` | `planning.read`, `projects.read` | capability_suite.py |
| `close_sprint` | `planning.write`, `projects.write`, `issues.write` | workflows.py |

### Label & Milestone Operations

| Tool | Capabilities | Module |
|------|-------------|--------|
| `list_labels` | `labels.read` | labels.py |
| `create_label` | `labels.write` | labels.py |
| `list_milestones` | `planning.read` | milestones.py |
| `create_milestone` | `planning.write` | milestones.py |
| `close_milestone` | `planning.write` | milestones.py |
| `set_estimate` | `projects.write` | estimate.py |
| `suggest_issue_labels` | `issues.read`, `labels.read` | capability_suite.py |

### PR / Issue Lifecycle

| Tool | Capabilities | Module |
|------|-------------|--------|
| `verify_acceptance_criteria` | `issues.read`, `pull_requests.read` | pr_issue_lifecycle.py |
| `get_pr_linked_issues` | `pull_requests.read`, `issues.read` | pr_issue_lifecycle.py |
| `validate_issue_closure_readiness` | `issues.read`, `pull_requests.read`, `comments.read` | pr_issue_lifecycle.py |
| `close_issue_on_pr_merge` | `issues.write`, `pull_requests.read`, `comments.write` | pr_issue_lifecycle.py |
| `link_pull_request` | `pull_requests.write`, `issues.write` | nice_to_have.py |
| `create_pull_request` | `pull_requests.write`, `issues.write` | nice_to_have.py |

### GitHub Actions & Checks

| Tool | Capabilities | Module |
|------|-------------|--------|
| `list_workflows` | `actions.read` | ci/actions.py |
| `list_workflow_runs` | `actions.read` | ci/actions.py |
| `get_workflow_run` | `actions.read` | ci/actions.py |
| `get_pr_checks` | `actions.read`, `pull_requests.read` | ci/actions.py |
| `get_job_logs` | `actions.read` | ci/actions.py |
| `rerun_workflow_run` | `actions.write` | ci/actions.py |
| `dispatch_workflow` | `actions.write` | ci/actions.py |

### Workflow Orchestration

| Tool | Capabilities | Module |
|------|-------------|--------|
| `daily_standup` | `projects.read`, `issues.read`, `comments.write` | workflows.py |
| `sprint_review` | `projects.read`, `planning.read`, `issues.read` | workflows.py |
| `triage_new_issues` | `issues.read`, `issues.write`, `projects.write` | workflows.py |
| `escalate_overdue` | `projects.read`, `issues.read`, `comments.write` | workflows.py |
| `handoff_issue` | `issues.write`, `comments.write`, `projects.write` | workflows.py |
| `create_epic` | `issues.write`, `projects.write`, `comments.write` | workflows.py |
| `blocked_report` | `projects.read`, `issues.read` | workflows.py |
| `bulk_assign` | `issues.write` | nice_to_have.py |

### Diagnostics

| Tool | Capabilities | Module |
|------|-------------|--------|
| `server_info` | _(none — reads local settings only)_ | server_info.py |

### Project Provisioning

| Tool | Capabilities | Module |
|------|-------------|--------|
| `create_project` | `projects.write` | project_provisioning.py |
| `update_project` | `projects.write` | project_provisioning.py |
| `create_project_field` | `projects.write` | project_provisioning.py |
| `link_repository` | `projects.write` | project_provisioning.py |
| `list_projects` | `projects.read` | project_provisioning.py |

### Permanent Delete (access level `full` only — each requires `confirm:true`)

These are **irreversible**. Registered ONLY when `MCP_ACCESS_LEVEL=full`.

| Tool | Capabilities | API | Reversible alternative |
|------|-------------|-----|------------------------|
| `delete_project_item` | `projects.write` | GraphQL `deleteProjectV2Item` | `archive_project_item` / `move_to_trash` |
| `delete_issue` | `issues.write` | GraphQL `deleteIssue` | `close_issue` |
| `delete_issue_comment` | `comments.write` | REST `DELETE issues/comments/{id}` | edit via `edit_issue_comment` |
| `delete_label` | `labels.write` | REST `gh label delete` | — (leave unused) |
| `delete_milestone` | `planning.write` | REST `DELETE milestones/{n}` | `close_milestone` |

### Text Processing (Local / No API)

| Tool | Capabilities | Module |
|------|-------------|--------|
| `validate_issue_markdown` | _(none — local text processing)_ | capability_suite.py |
| `format_issue_markdown` | _(none — local text processing)_ | capability_suite.py |
| `add_issue_acceptance_criteria` | _(none — local text processing)_ | capability_suite.py |
| `normalize_issue_title` | _(none — local text processing)_ | capability_suite.py |
| `suggest_issue_assignee` | `issues.read` | capability_suite.py |
| `build_issue_template` | _(none — local text processing)_ | capability_suite.py |
| `build_closure_comment` | _(none — local text processing)_ | capability_suite.py |
| `build_dependency_comment` | _(none — local text processing)_ | capability_suite.py |
| `build_issue_bundle` | `issues.read` | capability_suite.py |
| `build_status_update_comment` | _(none — local text processing)_ | capability_suite.py |
| `build_sprint_retrospective` | `planning.read`, `projects.read` | capability_suite.py |
| `build_roadmap_markdown` | `planning.read`, `projects.read` | capability_suite.py |
| `build_issue_creation_bundle` | _(none — local text processing)_ | capability_suite.py |
| `build_issue_review_checklist` | `issues.read` | capability_suite.py |
| `build_comment_digest` | `issues.read`, `comments.read` | capability_suite.py |
| `build_automation_decision` | _(none — local text processing)_ | capability_suite.py |

---

## Notes

1. Tools marked "none" perform local text transformation and never call the GitHub API.
2. The `discover_ids` tool is required on first use to resolve project/field IDs; it caches results.
3. Bulk operations (`bulk_close_issues`, `bulk_update_items`, `bulk_assign`) combine multiple write calls — enforce the same permissions as single-item equivalents.
4. Workflow tools (`complete_issue`, `create_epic`, etc.) aggregate multiple operations and require the union of all sub-operation capabilities.


## Repository Provisioning

| Tool | Capabilities | Module |
|------|--------------|--------|
| `create_repository` | `repositories.write` | repositories.py |

`create_repository` creates a repository for an explicitly selected user or
organization owner. The caller must provide or inherit the owner, choose an
explicit visibility (`public`, `private`, or organization-only `internal`),
and may request an initial README, GitHub gitignore template, or license.
Duplicate names and invalid visibility combinations return classified
validation errors. Raw GitHub responses and credentials are never exposed.

For least privilege, repository creation should be performed with a token that
has repository administration permission for the selected owner. Do not grant
that permission to read-only automation tokens.
