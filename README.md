# GitHub Projects V2 — MCP Server

[![CI](https://github.com/jersonmartinez/mcp-github-projects/actions/workflows/ci.yaml/badge.svg)](https://github.com/jersonmartinez/mcp-github-projects/actions/workflows/ci.yaml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/)
[![FastMCP](https://img.shields.io/badge/FastMCP-3.x-6E4AFF.svg)](https://github.com/jlowin/fastmcp)
[![Docker](https://img.shields.io/badge/docker-first-2496ED.svg?logo=docker&logoColor=white)](Dockerfile)

A Model Context Protocol (MCP) server that lets AI assistants and agents manage
**GitHub Projects V2** boards programmatically — issues, fields, milestones,
labels, sub-issues, and full sprint/planning workflows. Built with **Python 3.12**
and **FastMCP 3.x**, it speaks stdio JSON-RPC or optional stateless Streamable HTTP
and runs entirely inside a **standalone Docker container** — no host toolchain
required beyond Docker.

The server exposes **140 tools**: operational primitives, CI integration,
repository/project quality reports, and higher-level planning capabilities.

---

## Features

- **100+ MCP tools** covering the full GitHub Projects V2 surface: discovery,
  board operations, issue lifecycle, milestones, labels, sub-issues, and
  strategic planning.
- **GitHub Projects V2 native** — GraphQL v4 for field/board mutations, REST/`gh`
  for issue CRUD, with automatic delegation to the right API per operation.
- **Organization *and* user projects** via a single `GH_PROJECT_OWNER_TYPE` switch.
- **Access levels** — `MCP_ACCESS_LEVEL=read|write|full` decides which tools are
  even registered: `read` exposes read-only tools, `write` (default) adds
  create/update/close/archive, `full` also exposes permanent-delete tools (each
  gated behind `confirm:true`). See [docs/CAPABILITIES.md](docs/CAPABILITIES.md).
- **Scope lock** — `GH_PROJECT_SCOPE_LOCK=true` fences every tool to the
  configured org/repo/project; foreign targets are refused before any mutation.
- **Docker-first** — one image, zero host dependencies, launched on demand by the
  MCP client over stdio or optional stateless Streamable HTTP.
- **MCP-client agnostic** — works with any client that speaks MCP over stdio; no
  IDE lock-in.
- **Least-privilege ready** — every tool maps to a documented capability
  ([docs/CAPABILITIES.md](docs/CAPABILITIES.md)) so you can scope tokens tightly.
- **Hardened runtime** — bounded timeouts/retries, atomic owner-only metadata
  cache, target-namespaced isolation, and token redaction in all diagnostics.
- **Response-quality diagnostics** — bounded repository health, issue/PR lifecycle, CI conclusions, metadata consistency, closure readiness, pagination, and safe target diagnostics for automation workers.
- **Multi-target profiles** — manage several boards from one install via named
  `profiles/*.env` files.

---

## Quick Start

### 1. Build the image

```bash
docker build -t mcp-github-projects:latest .   # or: make build
```

Or use a released multi-arch image instead of building:
`docker pull ghcr.io/jersonmartinez/mcp-github-projects:1.2.0` (also tagged
`1.2` and `latest`; see [docs/RELEASING.md](docs/RELEASING.md)) and use that
name wherever the examples say `mcp-github-projects:latest`. The compatibility
contract is documented in [docs/STABILITY.md](docs/STABILITY.md).

### 2. Configure your target

Copy the template and fill in your token and board coordinates:

```bash
cp .env.example .env
```

```dotenv
# Authentication — a GitHub PAT (classic or fine-grained)
GITHUB_TOKEN=ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
# GH_TOKEN is also accepted as a fallback

# Target board
GH_PROJECT_ORG_NAME=my-org          # organization login or username
GH_PROJECT_REPO_NAME=my-repo        # repository within the owner
GH_PROJECT_PROJECT_NUMBER=1         # Project V2 number (from the board URL)
GH_PROJECT_OWNER_TYPE=organization  # 'organization' or 'user'
```

Token requirements:

- **Classic PAT** scopes: `repo`, `project`, `read:org`
- **Fine-grained PAT** permissions: Issues (RW), Projects (RW),
  Organization → Projects (RW), Organization → Members (R)

See [docs/SETUP.md](docs/SETUP.md) for token generation and rotation.

### 3. Verify the setup

```bash
make verify          # validates auth + scopes + target config inside Docker
```

### 4. Wire it into your MCP client

The server is launched on demand — one `docker run` per session, torn down with
`--rm` when the client disconnects:

```bash
docker run --rm -i --env-file .env mcp-github-projects:latest python server.py
```

On startup it prints to stderr and then waits for JSON-RPC on stdin:

```
github-project-management MCP server ready. Authentication validated successfully.
```

---

## MCP Client Configuration

The server works with **any MCP client** that supports the stdio transport. Add
an entry to your client's MCP server configuration:

```json
{
  "mcpServers": {
    "github-projects": {
      "command": "docker",
      "args": [
        "run", "--rm", "-i",
        "--env-file", "/absolute/path/to/.env",
        "mcp-github-projects:latest",
        "python", "server.py"
      ]
    }
  }
}
```

The config key (`github-projects`) is arbitrary — name it whatever your client
displays. Credentials are supplied through `--env-file`; nothing sensitive lives
in the JSON.

> **IDE-specific setup** (config file locations, per-client quirks) is documented
> in [docs/SETUP.md](docs/SETUP.md#ide-integration).

---

## Tool Catalog

The server registers **130 tools** at `MCP_ACCESS_LEVEL=full` (fewer at `read`/`write`). A category overview — the generated, per-tool catalog is [docs/TOOLS.md](docs/TOOLS.md):

| Category | What it covers | Representative tools |
|----------|----------------|----------------------|
| **Discovery & Board** | Resolve IDs, list/filter items, create items, move across columns | `discover_ids`, `list_project_items`, `create_project_item`, `move_to_status`, `move_to_done`, `move_to_trash`, `archive_project_item` |
| **Fields & Estimates** | Update Status/Priority/Due date, set story points | `update_project_item_fields`, `set_estimate` |
| **Issues** | Full issue lifecycle and sub-issues | `create_project_item`, `edit_issue`, `close_issue`, `reopen_issue`, `comment_issue`, `get_issue_detail`, `search_issues`, `add_sub_issue`, `remove_sub_issue`, `list_sub_issues` |
| **Bulk operations** | Batch updates across many items | `bulk_update_items`, `bulk_close_issues`, `bulk_assign` |
| **Milestones & Labels** | Create/close/list milestones; create/list labels | `create_milestone`, `close_milestone`, `list_milestones`, `create_label`, `list_labels` |
| **Repository provisioning** | Create a user or organization repository with explicit visibility | `create_repository` |
| **Planning & Workflows** | Sprints, standups, epics, triage, releases | `sprint_planning`, `create_epic`, `close_sprint`, `daily_standup`, `sprint_review`, `triage_new_issues`, `escalate_overdue`, `generate_release_notes`, `complete_issue` |
| **PR ↔ Issue lifecycle** | Verify acceptance, link PRs, gate closures | `verify_acceptance_criteria`, `get_pr_linked_issues`, `validate_issue_closure_readiness`, `close_issue_on_pr_merge` |
| **CI / GitHub Actions** | Read PR checks, runs, jobs and logs; re-run or dispatch workflows | `get_pr_checks`, `list_workflows`, `list_workflow_runs`, `get_workflow_run`, `get_job_logs`, `rerun_workflow_run`, `dispatch_workflow` |
| **Metrics** | Board and sprint statistics | `get_project_stats`, `get_sprint_summary` |
| **Diagnostics** | Report effective access level, scope lock, and target | `server_info` |
| **Board structure** | Plan/apply Status (single-select) options without losing item values; list/create views | `set_field_options`, `list_project_views`, `create_project_view` |
| **Permanent delete** *(access level `full`, each needs `confirm:true`)* | Irreversible removals | `delete_project_item`, `delete_issue`, `delete_issue_comment`, `delete_label`, `delete_milestone` |
| **Extended capability suite (~60)** | Reporting, roadmaps, changelogs, backlog ranking, risk registers, retrospectives | `project_health_report`, `project_export_markdown`, `plan_next_sprint`, `prioritize_backlog`, `generate_risk_register`, `build_roadmap_markdown`, `build_sprint_retrospective` |

Tools that could perform broad mutations return a `dry_run` plan by default. The
capability suite asserts its 60 unique additions at import time, CI verifies the
registered-tool count stays at 100+, and `tests/test_tool_docs.py` fails when
docs/TOOLS.md drifts from the registry or any tool lacks a description.

Full input/output reference: [docs/USAGE.md](docs/USAGE.md) ·
Parameter reference: [docs/PARAMETERS.md](docs/PARAMETERS.md) ·
Least-privilege capability matrix: [docs/CAPABILITIES.md](docs/CAPABILITIES.md)

---

## Architecture

Layered, dependency-inward design. Each layer depends only on the one below it:

```
MCP Client (stdio JSON-RPC)
        │
        ▼
server.py          FastMCP instance — registers every tool, validates auth on startup
        │
        ▼
tools/             MCP tool definitions (thin, declarative, dry_run-aware)
        │
        ▼
services/          Business logic & orchestration (project / issue / field / discovery)
        │
        ▼
clients/           GraphQL client + gh CLI client + metadata cache
        │
        ▼
graphql/           Query & mutation strings for the GitHub GraphQL v4 API
        │
        ▼
GitHub APIs        GraphQL v4 (fields, board, sub-issues) + REST v3 (issue CRUD)
```

Supporting modules at the root: `config.py` (validated Pydantic settings),
`auth.py` (token resolution + scope checks), `capabilities.py` (tool → permission
map), `profiles.py` (multi-target profiles), `hardening.py` and `error_handling.py`
(runtime safety), `models/` (Pydantic response/context models).

### API delegation

| Operation kind | Backend used |
|----------------|--------------|
| Issue CRUD, comments, project item-add, close | `gh` CLI |
| Field updates, archival, discovery, sub-issues | Custom GraphQL v4 |

---

## Configuration

All settings use the `GH_PROJECT_` prefix and are validated at startup by
`config.py`. Target fields are **mandatory** — the server refuses to start
without them.

| Variable | Required | Default | Description |
|----------|:--------:|---------|-------------|
| `GITHUB_TOKEN` | yes¹ | — | GitHub PAT (classic or fine-grained) |
| `GH_TOKEN` | — | — | Fallback token if `GITHUB_TOKEN` is unset |
| `GH_PROJECT_ORG_NAME` | **yes** | — | Owner: organization login or username |
| `GH_PROJECT_REPO_NAME` | **yes** | — | Repository within the owner |
| `GH_PROJECT_PROJECT_NUMBER` | **yes** | — | Project V2 number (1–100000) |
| `GH_PROJECT_OWNER_TYPE` | — | `organization` | `organization` or `user` |
| `MCP_ACCESS_LEVEL` | — | `write` | Which tools are exposed: `read`, `write`, or `full`. **Note: MCP_-prefixed, not GH_PROJECT_.** |
| `GH_PROJECT_SCOPE_LOCK` | — | `false` | Confine every tool to the configured org/repo/project |
| `GH_PROJECT_PROFILE` | — | — | Load `profiles/<name>.env` instead of root `.env` |
| `GH_PROJECT_TIMEOUT_SECONDS` | — | `10` | Per-call timeout (1–120) |
| `GH_PROJECT_RETRY_ATTEMPTS` | — | `1` | Read retries (0–5; mutations never retry) |
| `GH_PROJECT_CACHE_TTL_HOURS` | — | `24` | Metadata cache TTL (1–720) |
| `GH_PROJECT_MAX_ITEMS` | — | `200` | Max items per list/query (1–1000) |
| `GH_PROJECT_PAGE_SIZE` | — | `100` | Page size (1–100) |

¹ Token resolution order: `GITHUB_TOKEN` → `GH_TOKEN` → `gh auth token`.

The metadata cache is written atomically with owner-only permissions (`0600`),
namespaced per `owner/repo/project`, rejects future timestamps, and is never
reused across targets. See [docs/PARAMETERS.md](docs/PARAMETERS.md) for the full
range table and [docs/HARDENING_200.md](docs/HARDENING_200.md) for the runtime
hardening register.

### Access levels & scope lock

Two independent switches narrow what an MCP client can do — enforced by the
server, not by trust in the client.

**`MCP_ACCESS_LEVEL`** decides which tools are *registered* (a hidden tool is
invisible to the client, not merely refused):

| Level | Exposes | Permanent deletes |
|-------|---------|-------------------|
| `read` | read-only tools (discovery, listing, reporting) | hidden |
| `write` *(default)* | read + create / update / close / archive / move | hidden |
| `full` | everything | exposed, each requiring `confirm:true` |

The five permanent-delete tools (`delete_project_item`, `delete_issue`,
`delete_issue_comment`, `delete_label`, `delete_milestone`) exist ONLY at
`full`, and each refuses unless called with `confirm:true`, pointing you at the
reversible alternative (`close_issue`, `archive_project_item`, …).

> `MCP_ACCESS_LEVEL` is **`MCP_`-prefixed** (not `GH_PROJECT_`), matching the
> write-policy variable convention in the sibling `mcp-monday-projects` server.

**`GH_PROJECT_SCOPE_LOCK=true`** confines every tool to the configured
`GH_PROJECT_ORG_NAME` / `GH_PROJECT_REPO_NAME` / `GH_PROJECT_PROJECT_NUMBER`. A
call that targets any other owner/repo/project is refused with a typed error
naming the variable, *before* any mutation runs. Creating repositories or new
projects is disabled while the lock is on. (Parity concept with
`mcp-monday-projects`' `MONDAY_WORKSPACE_ID`.)

Call the `server_info` tool at any time to see the effective access level, scope
lock, and target — no credentials are ever included in its output.

---

## Development

Everything runs inside Docker — there are **no host Python dependencies**. The
`Makefile` is the entry point:

```bash
make help        # list all targets
make build       # build mcp-github-projects:latest
make rebuild     # build with --no-cache
make verify      # validate auth + scopes + target config
make test        # run unit tests inside the container
make syntax      # ast.parse every .py file
make tools       # count registered tools (must be >= 100)
make secrets     # scan the source tree for leaked credentials
make validate    # full CI mirror: build + syntax + test + tools + secrets
make shell       # interactive shell inside the container
make run         # start the server (stdio) via compose.yaml
make clean       # remove built images
```

> If `make` is unavailable, invoke targets directly, e.g.
> `docker run --rm --env-file .env mcp-github-projects:latest python3 scripts/verify_setup.py`.

Local validation before opening a PR (mirrors CI):

```bash
bash scripts/validate.sh            # full run (builds image + all checks)
bash scripts/validate.sh --quick    # reuse cached image, skip rebuild
bash scripts/validate.sh --fix      # auto-fix known issues (e.g. UTF-8 BOM)
```

Helper scripts under `scripts/`:

| Script | Purpose |
|--------|---------|
| `validate.sh` | Full CI mirror — run before every push/PR |
| `preflight.sh` | Prerequisite check (Docker, token, image, target config, scopes; `--fix` supported) |
| `scan_secrets.sh` | Token-pattern detection in tracked files |
| `smoke_build.sh` | Minimal build + tool count sanity check |
| `run_contract_tests.sh` | Multi-target contract suite |
| `count_tools.py` / `check_syntax.py` / `verify_setup.py` | Individual checks used by the Makefile |

Contributions follow [CONTRIBUTING.md](CONTRIBUTING.md); security reports go
through [SECURITY.md](SECURITY.md).

---

## Documentation

| Document | Purpose |
|----------|---------|
| [docs/SETUP.md](docs/SETUP.md) | Token generation, rotation, and per-IDE integration |
| [docs/TOOLS.md](docs/TOOLS.md) | Generated catalog: every tool, access tier, capabilities |
| [docs/USAGE.md](docs/USAGE.md) | Input/output examples per category |
| [docs/PARAMETERS.md](docs/PARAMETERS.md) | Full parameter and setting reference |
| [docs/CAPABILITIES.md](docs/CAPABILITIES.md) | Tool → permission matrix for least-privilege tokens |
| [docs/GRAPHQL_REFERENCE.md](docs/GRAPHQL_REFERENCE.md) | GraphQL queries/mutations used internally |
| [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) | Common errors and fixes |
| [docs/HARDENING_200.md](docs/HARDENING_200.md) | Runtime hardening register |
| [docs/RELEASING.md](docs/RELEASING.md) | Versioning, the release workflow and the GHCR image |


---

## License

Released under the [MIT License](LICENSE). See [CHANGELOG.md](CHANGELOG.md) for
release history; this project follows [Semantic Versioning](https://semver.org/).
