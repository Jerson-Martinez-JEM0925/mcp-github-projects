# Changelog

All notable changes to the GitHub Project Management MCP Server will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.2.0] - 2026-09-28

### Added

- **Stable contract guard:** `tests/test_contracts.py` freezes the public tool
  name, description, input-schema, and output-schema digest.
- **Repository quality workflows:** PR title/branch checks, YAML and shell
  linting, security audits, declarative label sync, Dependabot, and optional
  documentation-to-Wiki synchronization.
- **Stability policy:** compatibility and deprecation rules are documented in
  `docs/STABILITY.md`.

### Changed

- Corrected public generated headings and comments to English while retaining
  Spanish heading parsing for existing issue bodies.
- Clarified in `docs/HARDENING_200.md` that completed architecture items are
  applied and remaining entries are backlog, not missing release functionality.

## [Unreleased]

### Fixed

- **`create_project_item` reported "Failed to create issue" for an issue that
  already existed.** `gh issue create --assignee` creates the issue and only
  then sets assignees; when the token may not assign (e.g. a fork contributor
  on the upstream repository) gh exited non-zero and a retry created a
  duplicate. Assignees are now applied with a separate `gh issue edit
  --add-assignee` call after creation, and a failure there is returned as a
  `warnings` entry on the successful response (and appended to later
  partial-failure messages) instead of an error.

## [1.1.1] - 2026-09-27

### Fixed

- **`sync_closed_items_to_done` was not idempotent on boards whose columns carry
  no emoji** (issue #44). Terminal columns (`keep_statuses` + `done_status`)
  were compared by exact name, so with the default `✅ Done` / `🗑️ Trash` a
  bare `Done` column made every finished card a candidate on every run. They
  are now compared emoji- and case-insensitively, the same rule `update_field`
  uses to resolve an option.
- **`edit_issue` hid which resource was not found** (issue #47). Every
  "not found" from `gh issue edit` became "Issue #N or referenced resource not
  found", even when the issue existed and a label was missing. The message now
  carries gh's own reason (e.g. `'chore' not found`).

## [1.1.0] - 2026-09-27

### Added

- **Release workflow**: a `vX.Y.Z` tag verifies the tag against
  `core/version.py` and a CHANGELOG section (`scripts/release_notes.py`),
  runs the tests, builds `linux/amd64` + `linux/arm64`, pushes
  `ghcr.io/jersonmartinez/mcp-github-projects` (`X.Y.Z`, `X.Y`, `latest`) and
  publishes the GitHub release. `core/version.py` is the single source of the
  version; `server_info` reports it and the MCP handshake advertises it. See
  `docs/RELEASING.md`.

- **Documentation drift guard + invoke-every-tool test** (issue #36).
  `docs/TOOLS.md` is generated from the live registry by
  `scripts/gen_tool_docs.py` (name, access tier, capabilities, summary);
  `tests/test_tool_docs.py` fails when it is stale, when a tool is missing
  from it or has no description, and invokes **every** registered tool through
  a real FastMCP client with schema-derived arguments against injected fakes,
  requiring a structured `{"ok": ...}` envelope from each.

- **Board structure tools** (issue #26): `set_field_options` (plan by default;
  preserves option ids so items keep their values, keeps unlisted options
  unless `remove_missing` + `confirm`), `list_project_views`,
  `create_project_view`. The public API gained `updateProjectV2Field`
  single-select options and `createProjectV2View` (verified by introspection),
  so the earlier "Status columns cannot be provisioned" limit no longer holds.

- `GH_PROJECT_BACKEND_ASSIGNEE` / `GH_PROJECT_FRONTEND_ASSIGNEE` settings for
  `suggest_issue_assignee`.

- **GitHub Actions / checks tools** (issue #32, epic #5) in `tools/ci/actions.py`:
  read — `list_workflows`, `list_workflow_runs`, `get_workflow_run` (jobs +
  failed steps), `get_pr_checks` (check runs + commit statuses on the PR head,
  `overall` = passed/failed/pending/**none**), `get_job_logs` (bounded tail,
  ANSI escapes stripped, secrets redacted); write — `rerun_workflow_run`
  (failed jobs or whole run) and `dispatch_workflow` (`workflow_dispatch` with
  ≤ 10 string inputs). New capabilities `actions.read` / `actions.write`
  classify them for `MCP_ACCESS_LEVEL`; `owner`/`repo` overrides honour
  `GH_PROJECT_SCOPE_LOCK`. Lets the MCP verify a PR's CI without `gh pr
  checks`. Tool count: 119 → 126 at `full`.

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

- `sync_closed_items_to_done` — board-reconciliation tool that moves items whose
  linked issue/PR is CLOSED or MERGED to the Done column (skips items already in a
  terminal column). Supports `dry_run` preview and `issue_or_pr_number` scoping.
  GitHub does not auto-advance a card to Done when its PR merges, so cards
  otherwise linger in *In Progress*; this tool reconciles them in bulk.
  Tool count 104 → 105.

### Changed

- **`make build` now tags `mcp-github-projects:latest`**, the name every
  README/SETUP example and client configuration uses (and the GHCR package
  name). The Makefile alone used `github-project-mcp:latest`; retag an old
  local image with `docker tag github-project-mcp:latest mcp-github-projects:latest`.

- **Docs audit** (issue #37): removed leftovers from the project this server
  was extracted from (a `profiles/factib.env` hint that pointed to a missing
  file, a `factib_backend` container, an `app.mcp.github_project` module path,
  an IDE-specific architecture diagram, personal logins and dated sprint names
  in examples); USAGE now defers the full list to TOOLS.md and documents that
  Status/Priority values are read from the board; CAPABILITIES replaces its
  hand-maintained (and already drifted) per-tool matrix with the generated
  one; GRAPHQL_REFERENCE lists the new operations; README drops a link to a
  wiki that does not exist; AGENTS no longer names pointer files that are not
  in the repo; the unused `scripts/check_sync.sh` (compared against an
  embedded copy in another repository) is removed.

- **Uniform tool-argument convention** (issue #4). Every tool now accepts
  flat arguments (`{"issue_number": 1}`) and still unwraps a legacy
  `{"params": {...}}` wrapper, so clients no longer need to know per tool
  which shape to send. The advertised input schema is the flat one (real
  fields and `required` list) plus an optional deprecated `params` object.
  Validation still runs against each tool's own models (types, constraints,
  model validators unchanged); failures name the field and the expected
  shape. Applied centrally in `core/arguments.py` at registration.
  `scripts/mcp_call.py` sends flat args by default (`--wrap` for the legacy
  form; `--flat` is a no-op).

- **Architecture: HARDENING_200 items 6-8** (issue #35, epic #33), no change to
  any tool name or input schema (byte-identical `tools/list` at every access
  level):
  - `core/factory.py::ServiceFactory` is now the single construction point for
    `GraphQLClient`, `GHCLIClient`, `CacheManager` and the services. It replaces
    69 inline constructions in 23 tool modules; a test fails if a tool builds
    one inline again. Tests inject fakes with `use_service_factory(...)`.
  - `core/protocols.py` adds the `GraphQLExecutor` and `GHCLIRunner`
    protocols; services and helpers are typed against them, and a contract test
    checks the real clients still match.
  - `core/context.py` runs every tool call in a `RequestContext`. Its
    `correlation_id` prefixes each stderr log line of the call and is returned
    as the new `ToolError.correlation_id` field.

- `server.py` registration is now gated by `MCP_ACCESS_LEVEL`: only tools
  exposed at the configured level are registered. `scripts/count_tools.py`
  seeds a dummy target + `MCP_ACCESS_LEVEL=full` so the structural count stays
  deterministic and env-independent (verifies the full 119-tool surface).

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

### Fixed

- **Status option matching** — `move_to_done`, `move_to_trash` and
  `move_to_status` now resolve a bare name against an emoji-prefixed option
  (`Done` → `✅ Done`) when the match is unique; before, boards with emoji
  options rejected `move_to_done` outright.

- **60 capability-suite tools advertised an empty description** to MCP
  clients; each now has an accurate one-line summary (written against the
  implementation, e.g. `find_stale_issues` = no assignee or no update
  timestamp).

- **`suggest_issue_assignee` hardcoded two personal logins**; it now reports
  the area and suggests only a configured login.

- **Plan-only helpers were classified as write** (`project_set_default_*`,
  `project_bulk_*_by_filter`, `project_sync_issue_metadata`,
  `project_import_markdown`, `auto_triage_issue`): they never mutate, so they
  are read tools and available at `MCP_ACCESS_LEVEL=read`.

- **Clean fail-fast on misconfiguration** (issue #3). Starting the server with
  missing project context or an invalid setting no longer ends in a pydantic
  traceback: both `python server.py` and `python -m` print one
  `Configuration error: …` line to stderr and exit with code `2` (distinct
  from `1`, auth failure). Covered by subprocess tests in
  `tests/test_fail_fast.py`.
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
