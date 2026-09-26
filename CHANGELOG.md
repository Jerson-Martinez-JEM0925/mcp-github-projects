# Changelog

All notable changes to the GitHub Project Management MCP Server will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Access levels (`MCP_ACCESS_LEVEL`)** — `read` | `write` | `full`, default
  `write` (issue #34, epic #33). Governs which tools the server registers:
  `read` exposes only read-only tools, `write` keeps today's create/update/
  close/archive surface (nothing removed), and `full` additionally exposes
  five permanent-delete tools. Every registered tool is classified read/write/
  delete in `core/access.py` (derived from `core/capabilities.py`, the single
  source of truth), and a test asserts every registered tool is classified.
  The variable is `MCP_`-prefixed (parity with mcp-monday-projects).
- **Permanent-delete tools** — `delete_project_item` (deleteProjectV2Item),
  `delete_issue` (deleteIssue), `delete_issue_comment`, `delete_label`,
  `delete_milestone`. Exposed ONLY at `MCP_ACCESS_LEVEL=full`; each refuses
  unless called with `confirm:true`, stating the deletion is permanent and
  pointing to the reversible alternative (`close_issue`, `archive_project_item`,
  `close_milestone`, …). Tool count at `full`: 114 → 119.
- **Scope lock (`GH_PROJECT_SCOPE_LOCK`)** — `true` | `false`, default `false`
  (parity with mcp-monday-projects' `MONDAY_WORKSPACE_ID`). When `true`, every
  tool is confined to the configured `GH_PROJECT_ORG_NAME` /
  `GH_PROJECT_REPO_NAME` / `GH_PROJECT_PROJECT_NUMBER`; a call targeting any
  other owner/repo/project is refused with a typed `ScopeLockError` naming the
  variable, before any mutation. Repository/project creation is disabled while
  the lock is on.
- **`server_info` diagnostics tool** — reports the effective access level,
  scope lock, tool-exposure counts, and configured target (no credentials).
- **`.env.example`** gains boxed *Access level* and *Scope lock* sections plus a
  commented *Access level examples* block with read-only / write / full presets.

### Changed

- `server.py` registration is now gated by `MCP_ACCESS_LEVEL`: only tools
  exposed at the configured level are registered. `scripts/count_tools.py`
  seeds a dummy target + `MCP_ACCESS_LEVEL=full` so the structural count stays
  deterministic and env-independent (verifies the full 119-tool surface).

### Added (earlier)

- `sync_closed_items_to_done` — board-reconciliation tool that moves items whose
  linked issue/PR is CLOSED or MERGED to the Done column (skips items already in a
  terminal column). Supports `dry_run` preview and `issue_or_pr_number` scoping.
  GitHub does not auto-advance a card to Done when its PR merges, so cards
  otherwise linger in *In Progress*; this tool reconciles them in bulk.
  Tool count 104 → 105.

### Changed (earlier)

- `sync_closed_items_to_done` now scans the board **exhaustively** — it pages the
  entire board past `GH_PROJECT_MAX_ITEMS` (via the new
  `ProjectService.list_all_items`), so boards with more than 200 cards are fully
  reconciled in a single call, including the `issue_or_pr_number` scope. The
  previous `scan_capped`/`max_items` response fields are removed (no longer
  meaningful). `_fetch_all_items` gains an `exhaustive` flag with a large safety
  ceiling.
- `_ITEMS_FRAGMENT` (list-items GraphQL) now selects the content `state` and a
  `PullRequest` block, and `ProjectItem` gains a `content_state` field
  (OPEN/CLOSED/MERGED). PR-typed items are now parsed instead of skipped.

## [1.0.0] - 2026-08-20

### Added

- Initial stable release with 100 registered FastMCP tools
- 40 core operational tools for GitHub Project V2 management
- 60 capability suite tools (issue quality, comments, reporting, planning, strategy, roadmaps)
- Standalone Docker image with Python 3.12 and stdio transport
- CI/CD pipeline (`mcp-ci.yaml`) with build, syntax check, tests, and tool count verification
- Comprehensive documentation in `mcp/docs/`
- Support for environment-based configuration (`GH_PROJECT_ORG_NAME`, `GH_PROJECT_REPO_NAME`, `GH_PROJECT_PROJECT_NUMBER`)
- Dry-run mode for mutation-capable tools
- GraphQL and `gh` CLI dual-client architecture
