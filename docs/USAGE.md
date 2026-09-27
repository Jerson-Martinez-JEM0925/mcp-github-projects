# MCP GitHub Project Management — Usage Guide

Complete reference for all 105 tools exposed by the GitHub Project Management MCP server.

## Project Configuration

- **Organization**: (your configured org)
- **Repository**: (your configured repo)
- **Project number**: 1
- **Status values**: 📢 Proposal, 📌 To Do, 🛠 In Progress, ⏸ Pending, ✅ Done, 🗑️ Trash
- **Priority values**: Urgent, Important, Not urgent, Not important

---

## Tool Categories

| Category | Tools | Count |
|----------|-------|-------|
| Discovery & Board | discover_ids, list_project_items, create_project_item, update_project_item_fields, add_item_to_project, set_estimate, archive_project_item, move_to_done, move_to_trash, move_to_status, bulk_update_items | 11 |
| Issues | close_issue, reopen_issue, comment_issue, edit_issue, get_issue_detail, search_issues, bulk_close_issues, bulk_assign | 8 |
| Hierarchy | add_sub_issue, remove_sub_issue, list_sub_issues | 3 |
| Milestones | create_milestone, close_milestone, list_milestones | 3 |
| Labels | create_label, list_labels | 2 |
| Stats & Planning | get_project_stats, get_sprint_summary, sprint_planning, generate_release_notes, link_pull_request, create_pull_request | 6 |
| CI / Actions | list_workflows, list_workflow_runs, get_workflow_run, get_pr_checks, get_job_logs, rerun_workflow_run, dispatch_workflow | 7 |
| Workflows | complete_issue, daily_standup, sprint_review, triage_new_issues, escalate_overdue, handoff_issue, create_epic, close_sprint, blocked_report | 9 |

---

## 🔍 Discovery & Board

### discover_ids

Discovers project node ID, all field IDs, and option values. Results are cached for 24h.

```
Input:  { "force": false }
Output: { project_id, owner, project_number, fields: { Status: { id, options }, Priority: { id, options }, ... } }
```

### list_project_items

Lists items from the project board with optional filters.

```
Input:  { "status": "📌 To Do", "priority": "Urgent", "assignee": "jersonmartinez" }
Output: { items: [{ node_id, title, issue_number, status, priority, milestone, due_date, assignees, labels }], count }
```

**Filters** (all optional): status, priority, labels, assignee, milestone, due_date, due_date_op (before/after/exact)

### create_project_item

Creates a new GitHub issue and adds it to the project board. All board custom
fields can be set at creation, and any omitted board field is filled with a
configured default (`apply_defaults` is `true` by default) so an item never
lands with empty custom fields.

```
Input:  { "title": "New feature", "body": "Description", "status": "In Progress",
          "priority": "High", "area": "server", "work_type": "Feature",
          "estimate": 5, "assignees": ["jersonmartinez"], "labels": ["🚀 Feature"],
          "milestone": "Sprint 7", "due_date": "2026-07-20" }
Output: { issue_number, issue_url, item_node_id }
```

Accepted fields: `status`, `priority`, `area`, `work_type`, `estimate`,
`milestone`, `due_date`, `assignees`, `labels`, plus `apply_defaults`
(default `true`). Pass `apply_defaults: false` to set only the fields you
provided.

### update_project_item_fields

Updates one or more fields on a project item. Accepts **any board field** —
single-selects (`Status`, `Priority`, `Area`, `Work Type`), `NUMBER`
(`Estimate`), `DATE` (`Due date`), and `TEXT` — resolving single-select option
IDs by name at runtime from discovery. Issue-level fields (`body`,
`assignees`, `labels`) route through `gh issue edit`.

Address the item by node ID **or** by issue/PR number (resolved on the board,
owner-type aware — works on user- and organization-owned boards):

```
# By item node ID
Input:  { "item_id": "PVTI_...", "fields": { "Status": "In Progress",
          "Priority": "Urgent", "Area": "server", "Estimate": 3,
          "Due date": "2026-07-15", "Work Type": "Bug" } }

# By issue/PR number (resolved on the board)
Input:  { "issue_number": 42, "fields": { "Priority": "High" } }

# Fill any omitted board fields with configured defaults
Input:  { "issue_number": 42, "fields": {}, "apply_defaults": true,
          "labels": ["bug"] }

Output: { item_id, results: [{ field, outcome, value }], status }
```

Extra options: `apply_defaults` (fill omitted board fields from config
defaults), `labels` (used to infer the `Work Type` default), and `enforce`
(override `GH_PROJECT_ENFORCE_FIELDS` for this call — error if any board field
is still unset after defaults).

#### Supported field types

| Board field type | Example fields | Value format |
|------------------|----------------|--------------|
| `SINGLE_SELECT` | Status, Priority, Area, Work Type | option **name** (e.g. `"High"`) — resolved to option id |
| `NUMBER` | Estimate | numeric (e.g. `3` or `3.5`) |
| `DATE` | Due date | ISO 8601 `YYYY-MM-DD` |
| `TEXT` | any text field | string |
| Milestone | Milestone | milestone title |
| Issue fields | body, assignees, labels | string / list |

#### Field defaults (`apply_defaults` / creation)

When a board field is omitted and defaults apply, these values are used
(all overridable via env — see [SETUP](SETUP.md#board-field-defaults)):

| Field | Default | Env var |
|-------|---------|---------|
| Status | first board option | — |
| Priority | `Medium` | `GH_PROJECT_DEFAULT_PRIORITY` |
| Area | none (unless configured & valid) | `GH_PROJECT_DEFAULT_AREA` |
| Estimate | `3` | `GH_PROJECT_DEFAULT_ESTIMATE` |
| Due date | today + 7 days | `GH_PROJECT_DEFAULT_DUE_DAYS` |
| Work Type | `Bug` if a `bug` label else `Feature` | `GH_PROJECT_DEFAULT_WORK_TYPE` |

Only fields that actually exist on the board are set; an invalid configured
single-select option is skipped rather than sent.

### add_item_to_project

Adds an existing issue or PR to the configured project board via
`addProjectV2ItemById` (owner-type aware; idempotent — an item already on the
board returns its existing id). Returns the project item node id, which you can
pass straight to `update_project_item_fields`.

```
Input:  { "issue_or_pr_number": 42 }
Output: { issue_or_pr_number, item_id }
```

### set_estimate

Sets the time estimate (in hours) on a project item.

```
Input:  { "item_id": "PVTI_...", "value": 4.0 }
Output: { item_id, estimate, field_id }
```

### archive_project_item

Removes an item from the active project board (archives it).

```
Input:  { "item_id": "PVTI_..." }
Output: { item_id, status: "archived" }
```

### move_to_done / move_to_trash

Shortcuts to move items to Done or Trash status.

```
Input:  { "item_id": "PVTI_..." }
Output: { item_id, status: "Done" }
```

### move_to_status

Moves a project item to ANY status column.

```
Input:  { "item_id": "PVTI_...", "status": "🛠 In Progress" }
Output: { item_id, status, message }
```

**Valid statuses**: 📢 Proposal, 📌 To Do, 🛠 In Progress, ⏸ Pending, ✅ Done, 🗑️ Trash

### bulk_update_items

Updates fields on multiple project items at once.

```
Input:  { "item_ids": ["PVTI_...", "PVTI_..."], "fields": { "Priority": "Urgent", "Due date": "2026-08-01" } }
Output: { results: [{ item_id, fields: [{ field, status }] }], total, fully_updated }
```

---

## 📋 Issues

### close_issue

Closes a GitHub issue (reason: completed).

```
Input:  { "issue_number": 258 }
Output: { issue_number, state: "closed" }
```

### reopen_issue

Reopens a previously closed issue.

```
Input:  { "issue_number": 258 }
Output: { issue_number, state: "open" }
```

### comment_issue

Adds a markdown comment to an issue.

```
Input:  { "issue_number": 258, "body": "Working on this now. ETA: 2 hours." }
Output: { issue_number, comment_url }
```

### edit_issue

Edits issue properties: title, body, milestone, labels, assignees.

```
Input:  { "issue_number": 258, "milestone": "Sprint 3 - Jul 21-27", "add_labels": ["🔒 Security"], "add_assignees": ["ffactib"] }
Output: { issue_number, updated_fields }
```

**Fields** (all optional): title, body, milestone, add_labels, remove_labels, add_assignees, remove_assignees

### get_issue_detail

Gets complete issue details including sub-issues.

```
Input:  { "issue_number": 257 }
Output: { number, title, body, state, labels, assignees, milestone, sub_issues: [{ number, title, state }], sub_issues_count }
```

### search_issues

Searches issues by text, labels, milestone, assignee, state.

```
Input:  { "query": "admin", "labels": ["🎨 FrontEnd"], "state": "open", "limit": 20 }
Output: { issues: [{ number, title, state, labels, milestone, assignees }], count }
```

### bulk_close_issues

Closes multiple issues at once with optional comment.

```
Input:  { "issue_numbers": [80, 81, 82], "reason": "completed", "comment": "✅ Implemented in current version." }
Output: { results: [{ issue, status }], total, closed }
```

### bulk_assign

Assigns multiple issues to users and/or milestone.

```
Input:  { "issue_numbers": [258, 259, 260], "assignees": ["ffactib"], "milestone": "Sprint 2 - Jul 14-20" }
Output: { results, total, updated }
```

---

## 🌳 Hierarchy

### add_sub_issue

Links a child issue as a sub-issue of a parent (formal GitHub sub-issue relationship).

```
Input:  { "parent_issue_number": 257, "child_issue_number": 258 }
Output: { parent_issue, child_issue, message }
```

### remove_sub_issue

Removes the sub-issue relationship between parent and child.

```
Input:  { "parent_issue_number": 257, "child_issue_number": 258 }
Output: { parent_issue, child_issue, message }
```

### list_sub_issues

Lists all sub-issues of a parent issue.

```
Input:  { "issue_number": 257 }
Output: { parent_issue, sub_issues: [{ number, title, state, assignees }], count }
```

---

## 📅 Milestones

### create_milestone

Creates a new milestone with optional description and due date.

```
Input:  { "title": "Sprint 13 - Sep 28", "description": "Post-launch fixes", "due_date": "2026-09-28" }
Output: { number, title, html_url }
```

### close_milestone

Closes an open milestone by title.

```
Input:  { "title": "Sprint 1 - Jul 7-13" }
Output: { number, title, state: "closed" }
```

### list_milestones

Lists milestones filtered by state.

```
Input:  { "state": "open" }
Output: { milestones: [{ number, title, state, due_on, open_issues, closed_issues }], count }
```

---

## 🏷️ Labels

### create_label

Creates or updates a label with name and hex color.

```
Input:  { "name": "🔥 Hotfix", "color": "d93f0b", "description": "Critical production fix" }
Output: { name, color, message }
```

### list_labels

Lists all labels in the repository.

```
Input:  { "limit": 50 }
Output: { labels: [{ name, color, description }], count }
```

---

## 📊 Stats & Planning

### get_project_stats

Gets overall project statistics: issues per sprint, progress percentage.

```
Input:  {}
Output: { total_open_issues, total_closed_issues, overall_progress_pct, sprints: [{ title, open_issues, closed_issues, progress_pct, due_on }] }
```

### get_sprint_summary

Detailed summary of a specific sprint: issues, progress, assignee distribution, days remaining.

```
Input:  { "milestone_title": "Sprint 2 - Jul 14-20" }
Output: { milestone, due_on, days_remaining, total_issues, open, closed, progress_pct, assignee_distribution, open_issues }
```

### sprint_planning

Auto-distributes unassigned sprint issues between team members.

```
Input:  { "milestone_title": "Sprint 2 - Jul 14-20", "team_members": ["jersonmartinez", "ffactib"], "strategy": "backend_frontend" }
Output: { milestone, strategy, total_issues, already_assigned, unassigned, distribution: { "user": { already_assigned, newly_planned, total, new_issues } }, next_step }
```

**Strategies**:
- `balanced`: Equal number per person (load-aware)
- `backend_frontend`: Backend labels → member[0], Frontend labels → member[1]
- `round_robin`: Alternate assignment

### generate_release_notes

Generates formatted markdown release notes from closed issues in a milestone.

```
Input:  { "milestone_title": "Sprint 1 - Jul 7-13", "version": "v2.1.0", "include_contributors": true, "group_by_label": true }
Output: { version, milestone, issues_count, contributors, categories_used, markdown }
```

### link_pull_request

Links a PR to an issue (adds closing reference + comment).

```
Input:  { "issue_number": 258, "pr_number": 45 }
Output: { issue_number, pr_number, pr_url, message }
```

### create_pull_request

Opens a new pull request via the GitHub REST API (`POST /repos/{owner}/{repo}/pulls`).
Owner/repo come from the `GH_PROJECT_*` env context. Optionally links the new PR to an
issue by reusing `link_pull_request` (pass `link_to_issue`).

```
Input:  { "title": "Add create_pull_request tool", "head": "feat/create-pull-request-tool", "base": "main", "body": "Closes #7", "draft": false, "link_to_issue": 7 }
Output: { pr_number, pr_url, title, head, base, draft, state, message, link_result? }
```

### sync_closed_items_to_done

Reconciles the board: moves every item whose linked issue/PR is **CLOSED** (issue or PR)
or **MERGED** (PR) to the Done column, unless it is already in a terminal column
(`✅ Done` / `🗑️ Trash`) — or already at `done_status` itself. GitHub does not
auto-advance a card to Done when its PR is merged, so cards linger in *In Progress*
— this tool fixes that in bulk. **Idempotent**: items already in a terminal column
(including the destination `done_status`, which is always treated as terminal) are
never candidates. The scan is **exhaustive** — it pages the entire board past
`GH_PROJECT_MAX_ITEMS`, so boards with more than 200 cards are fully reconciled in
one call (including `issue_or_pr_number`, which filters the full board). Owner-type
(org/user) is handled by the underlying `list_all_items`. Supports a dry-run
preview and scoping to a single number.

```
Input:  {}                                   // scan the whole board, apply
        { "dry_run": true }                  // preview only, no mutations
        { "issue_or_pr_number": 658 }        // reconcile just one item
        { "done_status": "✅ Done",
          "keep_statuses": ["✅ Done", "🗑️ Trash"] }   // overrides (defaults shown)
Output: { dry_run, done_status, scanned, skipped_open, errors,
          moved_count, moved:[{number,type,content_state,from_status,item_id}] }
          // dry-run returns would_move_count / would_move instead of moved_*
```

---

## 🚀 Workflows

### complete_issue

Completes an issue lifecycle: adds completion comment, closes it, project auto-moves to Done.

```
Input:  { "issue_number": 258, "summary": "Fixed navigation. Tested on all browsers." }
Output: { issue_number, status: "completed", comment, message }
```

### daily_standup

Generates a daily standup report: what was done recently, what's in progress, what's blocked.

```
Input:  { "username": "jersonmartinez" }  // or {} for all team
Output: { date, done_recently: [{ number, title }], done_count, in_progress: [...], in_progress_count, blocked: [...], blocked_count }
```

### sprint_review

Sprint retrospective data: velocity, completion %, per-person breakdown, pending items.

```
Input:  { "milestone_title": "Sprint 1 - Jul 7-13" }
Output: { milestone, total_issues, completed, pending, progress_pct, velocity, per_person: { "user": { completed, pending } }, pending_issues }
```

### triage_new_issues

Finds untriaged issues (missing milestone, assignee, or labels).

```
Input:  {}
Output: { untriaged_count, issues: [{ number, title, problems: ["no_milestone", "no_assignee"] }] }
```

### escalate_overdue

Finds issues past their due date that are still open.

```
Input:  {}
Output: { overdue_count, issues: [{ number, title, milestone, days_overdue, assignees }] }
```

### handoff_issue

Reassigns an issue from one person to another with a context comment.

```
Input:  { "issue_number": 267, "from_user": "jersonmartinez", "to_user": "ffactib", "context": "Backend done, needs frontend UI now." }
Output: { issue_number, from, to, message }
```

### create_epic

Creates a parent (epic) issue and links its sub-issues in a single call. Sub-issues
can be **created** from `sub_tasks` titles and/or **linked from existing** issues via
`link_existing`.

Project, field, and option IDs are resolved **at runtime** from the `GH_PROJECT_*`
context (via the discovery service) — there are no hardcoded board IDs, so the tool
works against any board. When `status` is omitted it defaults to the board's **first
Status option**; pass `status`/`priority` by option name (validated against the board's
own field options).

Parameters:
- `title` (str, required) — epic title.
- `body` (str, required) — epic description.
- `sub_tasks` (list[str], optional, max 20) — titles for NEW sub-issues to create.
- `link_existing` (list[int], optional) — existing issue numbers to link as sub-issues (no new issues created).
- `milestone`, `assignee`, `labels` (optional) — applied to created issues.
- `status` (str, optional) — Status option name; defaults to the board's first Status option.
- `priority` (str, optional) — Priority option name (must match the board's Priority options).

```
Input:  { "title": "🏔️ [Epic] OCR System", "body": "Implement ticket scanning...", "sub_tasks": ["Backend OCR endpoint", "Frontend camera UI"], "link_existing": [42, 43], "milestone": "Sprint 4 - Jul 28 - Ago 3", "assignee": "jersonmartinez", "labels": ["📱 Mobile"], "priority": "High" }
Output: { parent_issue, parent_url, sub_issues: [{ number, title, created }], sub_issues_count, linked_existing: [{ number, linked }], linked_existing_count, status_applied, message }
```

### close_sprint

Closes a sprint: closes the milestone, moves pending issues to the next sprint, generates summary.

```
Input:  { "milestone_title": "Sprint 1 - Jul 7-13", "next_milestone": "Sprint 2 - Jul 14-20" }
Output: { milestone, total, completed, pending_moved, moved_to, completion_pct, moved_issues, message }
```

### blocked_report

Reports issues that are blocked or pending, with dependency information.

```
Input:  {}
Output: { blocked_count, issues: [{ number, title, assignees, milestone, dependency: 123 }] }
```

---

## ✅ CI / GitHub Actions

Verify a PR's checks and recover a workflow run without leaving the MCP. All
tools default to the configured repository; `owner`/`repo` overrides are
refused when `GH_PROJECT_SCOPE_LOCK=true`. Token needs Actions/Checks read
(and Actions write + classic `workflow` scope for the two write tools).

| Tool | Access | Purpose |
|------|--------|---------|
| `get_pr_checks` | read | Every check run + commit status on the PR head: `overall` = `passed` / `failed` / `pending` / `none`. **`none` (no checks) is a blocker, not green.** |
| `list_workflows` | read | Workflows with id, file name and state (`limit` ≤ 100). |
| `list_workflow_runs` | read | Recent runs, filtered by `workflow`, `branch`, `event`, `status`, `head_sha` (`limit` ≤ 100). |
| `get_workflow_run` | read | One run plus its jobs and the names of the failed steps. |
| `get_job_logs` | read | Last `tail_lines` (≤ 2000) of a job log, terminal escapes stripped, secrets redacted. |
| `rerun_workflow_run` | write | Re-run failed jobs (default) or the whole run (`failed_only: false`). |
| `dispatch_workflow` | write | Trigger a `workflow_dispatch` run on `ref`, with up to 10 string `inputs`. |

```bash
make call TOOL=get_pr_checks ARGS='{"pr_number": 40}'
make call TOOL=list_workflow_runs ARGS='{"branch": "feat/x", "status": "failure", "limit": 5}'
make call TOOL=get_workflow_run ARGS='{"run_id": 123456789}'
make call TOOL=get_job_logs ARGS='{"job_id": 987654321, "tail_lines": 100}'
```

---

## 🔐 Access control & diagnostics

Two server-side switches govern the whole surface (set in the environment /
`.env`, not per call):

- **`MCP_ACCESS_LEVEL`** = `read` | `write` (default) | `full` — which tools are
  registered. Delete tools appear only at `full`.
- **`GH_PROJECT_SCOPE_LOCK`** = `true` | `false` (default) — confine every tool
  to the configured org/repo/project.

### server_info

Returns the effective access level, scope lock, tool-exposure counts, and the
configured target. Credential-free. No arguments.

```
Input:  {}
Output: { access_level, access_level_meaning, tool_exposure: { classified_total,
          exposed, hidden, hidden_delete_tools }, exposed_tools: [...],
          scope_lock: { enabled, variable, confined_to, meaning }, target }
```

### Permanent-delete tools (access level `full`, each requires `confirm: true`)

Each tool is **irreversible** and refuses without `confirm: true`, pointing you
at the reversible alternative. They are not even registered below `full`.

```
delete_project_item   Input: { "item_id": "PVTI_...", "confirm": true }
                      Output: { item_id, deleted_item_id, deleted: true }
                      Reversible instead: archive_project_item / move_to_trash

delete_issue          Input: { "issue_number": 42, "confirm": true }
                      Output: { issue_number, repository, deleted: true }
                      Reversible instead: close_issue

delete_issue_comment  Input: { "comment_id": 1234567, "confirm": true }
                      Output: { comment_id, deleted: true }

delete_label          Input: { "name": "wontfix", "confirm": true }
                      Output: { name, deleted: true }

delete_milestone      Input: { "title": "Sprint 1", "confirm": true }
                      Output: { title, number, deleted: true }
                      Reversible instead: close_milestone
```

Without `confirm: true` every one of these returns
`{ ok: false, error_type: "validation", message: "... is PERMANENT ...",
suggestion: "... call again with confirm=true ... or use <alternative>" }` and
makes no API call.

Under `GH_PROJECT_SCOPE_LOCK=true`, tools that accept an owner/repo/project
override (`list_projects`, `link_repository`, `create_project`,
`create_repository`) refuse a foreign target with a `validation` error naming
`GH_PROJECT_SCOPE_LOCK`, before any API call.

---

## Error Handling

All tools return either a success or error response:

**Success**: `{ "ok": true, "data": { ... } }`

**Error**: `{ "ok": false, "error_type": "validation|not_found|internal", "message": "...", "suggestion": "..." }`

Error types:
- `validation`: Invalid input (wrong status value, missing required field)
- `not_found`: Issue/milestone/item doesn't exist
- `internal`: GitHub API error, network issue, or unexpected failure

---

## Authentication

The server resolves a GitHub token in this order:
1. `GITHUB_TOKEN` environment variable
2. `GH_TOKEN` environment variable
3. `gh auth token` (GitHub CLI authenticated session)

**Required permissions** (Fine-grained PAT):
- Repository: Read & Write (issues, pull requests)
- Organization: Read & Write (projects, members)

**Note**: Fine-grained tokens (`github_pat_*`) skip scope validation automatically.

---

## Architecture

```
Kiro IDE → docker compose exec backend → python -m app.mcp.github_project.server → stdio MCP protocol
```

The server runs inside the backend Docker container, communicating with Kiro via stdin/stdout using the MCP stdio transport protocol.

### File Structure

```
mcp-github-projects/
├── server.py                    ← Entry point (registers every tool)
├── __main__.py                  ← `python -m` shim
├── core/                        ← Cross-cutting infrastructure
│   ├── auth.py                  ← Token resolution + scope validation
│   ├── config.py                ← Pydantic settings (env prefix: GH_PROJECT_)
│   ├── error_handling.py        ← Unified error response builder
│   ├── exceptions.py            ← Custom exception types
│   ├── hardening.py             ← Input parsing, redaction, atomic writes
│   ├── profiles.py              ← Multi-target profile system
│   └── capabilities.py          ← Tool → permission mapping
├── clients/
│   ├── gh_cli_client.py         ← Async wrapper for `gh` CLI
│   ├── graphql_client.py        ← GraphQL API client
│   └── cache_manager.py         ← File-based cache with TTL
├── models/
│   └── responses.py             ← ToolSuccess/ToolError response models
├── services/
│   ├── discovery_service.py     ← Project metadata discovery
│   ├── project_service.py       ← Field updates via GraphQL
│   ├── issue_service.py         ← Issue operations via CLI
│   ├── field_service.py         ← Field value resolution/validation
│   └── owner_type_resolver.py   ← user vs organization auto-detection
├── tools/                       ← MCP tools, grouped by category
│   ├── discovery/               ← discover_ids, list_project_items
│   ├── issues/                  ← create/edit/close/reopen, comment, sub-issues, detail
│   ├── pull_requests/           ← acceptance criteria, linked issues, closure readiness
│   ├── projects/                ← move_to_status/done/trash, archive, update_fields
│   ├── fields/                  ← set_estimate, labels, milestones
│   ├── planning/                ← sprint_planning, release notes, epics, standup, review
│   ├── bulk/                    ← bulk_close, search_issues
│   └── meta/                    ← stats, sprint_summary, link/create_pr, bulk_assign,
│                                  capability_suite (60 tools)
└── docs/
    ├── architecture/PROJECT_STRUCTURE.md ← Layout & conventions (authoritative)
    ├── USAGE.md                 ← This file
    ├── SETUP.md                 ← Installation & configuration
    ├── PARAMETERS.md            ← Environment variables reference
    ├── GRAPHQL_REFERENCE.md     ← GraphQL queries used
    └── TROUBLESHOOTING.md       ← Common issues & fixes
```

> Historical flat module paths (`config.py`, `tools/workflows.py`, …) remain as
> thin backward-compatibility shims that alias the new homes. See
> [architecture/PROJECT_STRUCTURE.md](architecture/PROJECT_STRUCTURE.md).

---

## CLI client

`scripts/mcp_call.py` is a minimal stdio client for calling a single tool
without writing a full MCP client. It launches the server (`python server.py`)
as a subprocess, performs the FastMCP handshake, calls one tool, prints the
JSON response, and exits.

### Via `make call` (recommended)

Runs the client inside the Docker image with your `.env` for token and project
context:

```bash
make call TOOL=list_labels
make call TOOL=get_issue_detail ARGS='{"issue_number": 1}'
make call TOOL=get_project_stats
```

### Directly

```bash
python scripts/mcp_call.py <TOOL> [JSON_ARGS] [--wrap] [--raw]
```

| Argument / flag | Meaning |
|-----------------|---------|
| `TOOL` | Tool name (e.g. `list_labels`). |
| `JSON_ARGS` | Arguments as a JSON object, passed flat. Default `{}`. |
| `--wrap` | Nest the arguments under the legacy `params` key (only to exercise that path; `--flat` is accepted as a no-op). |
| `--raw` | Print the full JSON-RPC envelope instead of just the tool result. |

### Argument convention

Every tool takes **flat** arguments, e.g. `{"issue_number": 1, "body": "…"}`.
For backward compatibility a legacy `{"params": {...}}` wrapper is accepted on
every tool and unwrapped; if both are sent, the flat value wins. A malformed
call returns an error naming the missing/invalid field and the expected shape:

```
Invalid arguments for get_issue_detail: issue_number: Field required.
Pass arguments flat, e.g. {"issue_number": …}; a legacy {"params": {...}} wrapper is also accepted.
```

### Authentication & context

- Token resolved from `GITHUB_TOKEN`, then `GH_TOKEN`. It is passed through to
  the server process and is **never printed or logged** by the client.
- The server also needs `GH_PROJECT_ORG_NAME`, `GH_PROJECT_REPO_NAME`,
  `GH_PROJECT_PROJECT_NUMBER` in the environment — supplied by your `.env` when
  using `make call`. `GH_PROJECT_OWNER_TYPE` is optional and defaults to
  `auto`: user-owned boards are detected automatically, so you only set
  `GH_PROJECT_OWNER_TYPE=user` (or `organization`) when you want to skip
  detection.

### Exit codes

- `0` — tool returned successfully.
- non-zero — the tool reported an error (`isError: true`) or no response was
  received (check token / project-context env).
