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
| `pull_requests.write` | Create and update PRs, link PRs, close issues on merge |
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

## Tool × Capability Matrix

The per-tool list (access tier and required capabilities for all tools) is
generated from `core/capabilities.py` into [TOOLS.md](TOOLS.md), and CI fails
when it drifts (`tests/test_tool_docs.py`). It is not repeated here, so it
cannot go stale. Two classification rules worth knowing:

- A tool is **write** when it needs any `*.write` capability, **read**
  otherwise; permanent deletes are a separate **delete** tier (`full` only).
- Plan-only helpers that return `"dry_run": true` and never mutate
  (`project_set_default_*`, `project_bulk_*_by_filter`,
  `project_sync_issue_metadata`, `project_import_markdown`,
  `auto_triage_issue`) are **read** tools, so they stay available at
  `MCP_ACCESS_LEVEL=read`.

### Board structure (Status columns and views)

The public Project V2 API now supports editing single-select options
(`updateProjectV2Field.singleSelectOptions`) and creating/updating views
(`createProjectV2View`, `updateProjectV2View`) — verified by schema
introspection on 2026-09-27. The server exposes them as:

| Tool | Access | Safety |
|------|--------|--------|
| `set_field_options` | write | `dry_run: true` by default; existing options are sent back with their id (items keep their value, even on rename); unlisted options are **kept** unless `remove_missing: true`, which additionally requires `confirm: true` because items holding a removed option lose it. |
| `list_project_views` | read | — |
| `create_project_view` | write | Creates a board/table/roadmap view, optionally with a filter. Views have no delete tool here. |

Native workflows (built-in automations) are still not writable through the
public API beyond `deleteProjectV2Workflow`, so they are not exposed.

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
