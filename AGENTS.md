# AGENTS.md

Vendor-neutral guide for AI coding agents (and humans) working in this
repository. This file is the **single source of truth** for how to build,
test, extend, and contribute to the project. If an MCP client needs its own
pointer file (for example `.cursor/rules/*.mdc` or
`.github/copilot-instructions.md`), it must only point here — when they
disagree, AGENTS.md wins. This repository intentionally does not create or
maintain `CLAUDE.md`.

---

## 1. Project Overview

This repository is a **Model Context Protocol (MCP) server for GitHub Projects
V2**. It exposes 100+ tools for managing issues, project-board fields,
milestones, labels, sub-issues, sprints, epics, and PR/issue lifecycle
automation. It is built on **FastMCP**, communicates over the **stdio**
transport, and runs as a **Docker container**.

- Language: **Python 3.12+**, fully type-hinted.
- Transport: FastMCP over stdio (`server.py` → `mcp.run(transport="stdio")`).
- Data access: GitHub GraphQL API v4 (Projects V2) with a `gh` CLI fallback
  for REST-only operations.
- Runtime: **Docker only** — there are no supported host-Python workflows.
- Maintainership: single maintainer, public OSS (MIT).

---

## 2. Flat Repository Layout

The project uses a **flat package layout** (no nested `src/` or vendor prefix).
Imports are top-level (`from tools.close import close_issue`,
`from models.responses import ToolSuccess`).

```
.
├── server.py            # FastMCP entry point; registers every tool
├── config.py            # Settings (env-driven): org/repo/project, timeouts, scopes
├── auth.py              # Token resolution + scope validation
├── capabilities.py      # Capability enum + profile capability enforcement
├── profiles.py          # Multi-target profile registry (user vs org projects)
├── error_handling.py    # build_error_response(), handle_tool_error()
├── exceptions.py        # Typed exceptions
├── hardening.py         # Input/rate-limit hardening helpers
├── tools/               # One module per tool (or tool family) — see §4
│   ├── __init__.py      # Re-exports the core tool subset
│   ├── comment_issue.py # Canonical single-tool example
│   ├── capability_suite.py  # Extended 60-tool suite (dynamic registration)
│   └── ...
├── services/            # Domain services (issue_service, project_service, ...)
├── clients/             # graphql_client, gh_cli_client, cache_manager
├── graphql/             # queries.py, mutations.py (GraphQL documents)
├── models/              # Pydantic models — responses.py, context.py, items.py
├── scripts/             # Docker-invoked CI/validation scripts (Python + bash)
├── tests/               # pytest suite
├── docs/                # SETUP, USAGE, PARAMETERS, CAPABILITIES, TROUBLESHOOTING
├── compose.yaml         # Single `mcp` service
├── Dockerfile
└── Makefile             # The ONLY supported command surface (Docker-backed)
```

---

## 3. Build & Test (Docker-only)

**All commands run inside Docker.** Do not run `python`, `pip`, or `pytest` on
the host — the supported surface is the `Makefile`, whose targets shell into the
container. First-time setup:

```bash
cp .env.example .env      # then edit: set GITHUB_TOKEN (or GH_TOKEN) + target
make build                # build the image
make verify               # validate auth + scopes + target configuration
```

Everyday targets:

| Target          | Purpose                                                        |
|-----------------|----------------------------------------------------------------|
| `make build`    | Build the MCP Docker image                                     |
| `make rebuild`  | Force rebuild without cache                                    |
| `make verify`   | Validate token, scopes, and target config against GitHub       |
| `make test`     | Run the unit test suite inside Docker                          |
| `make tools`    | Count registered MCP tools (must be **≥ 100**)                 |
| `make syntax`   | `ast.parse` every `.py` file                                   |
| `make secrets`  | Scan the tree for leaked credentials                           |
| `make preflight`| Check host prerequisites (Docker, token, image)               |
| `make validate` | **Full CI mirror**: build + syntax + test + tools + secrets    |
| `make shell`    | Interactive shell inside the container                         |
| `make run`      | Start the server in stdio mode via compose                     |
| `make clean`    | Remove built images                                            |

**Run `make validate` and get a clean pass before proposing any PR.** It is the
local mirror of the CI pipeline; a green `make validate` is the contract for
"ready to review".

---

## 4. Tool-Authoring Pattern

Every MCP tool follows the same shape. Use `tools/comment_issue.py` as the
canonical reference. A tool is:

1. **A module in `tools/`** — one file per tool or tightly-related tool family.
2. **A Pydantic input model** named `<ToolName>Input`, subclassing
   `pydantic.BaseModel`, with `Field(...)` descriptions and validation
   constraints on every parameter.
3. **An `async def` function** whose sole business parameter is
   `params: <ToolName>Input`, and whose return type is `dict`.
4. On success, return `ToolSuccess(data={...}).model_dump()`.
5. On failure, return an error envelope via `build_error_response(...)` for
   known/typed failures, or `handle_tool_error(exc, context=...)` in the catch-all
   `except`. Never raise out of a tool.
6. **Registered in `server.py`** with `mcp.tool()(<function>)`. If the tool
   belongs to the core subset, also re-export it from `tools/__init__.py`
   (add to both the imports and `__all__`, kept alphabetical).

Minimal skeleton:

```python
"""MCP tool for <what it does>."""
from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from auth import resolve_token
from config import get_settings
from error_handling import build_error_response, handle_tool_error
from models.responses import ToolSuccess

logger = logging.getLogger(__name__)


class DoThingInput(BaseModel):
    """Input schema for the do_thing tool."""

    issue_number: int = Field(description="GitHub issue number")
    note: str = Field(description="Note to attach", min_length=1, max_length=65536)


async def do_thing(params: DoThingInput) -> dict:
    """Do the thing.

    Args:
        params: Validated input for the operation.

    Returns:
        ToolSuccess on success, or an error envelope on failure.
    """
    try:
        await resolve_token()
        settings = get_settings()
        # ... perform the operation (prefer GraphQL — see §5) ...
        return ToolSuccess(
            data={"issue_number": params.issue_number, "message": "Done."},
        ).model_dump()
    except SomeTypedError as exc:
        logger.error("do_thing failed: %s", exc)
        return build_error_response(
            error_type="internal",
            message=f"Failed to do thing: {exc}",
            suggestion="Verify the issue exists and the token has write scope.",
        )
    except Exception as exc:
        logger.error("Error in do_thing: %s", exc)
        return handle_tool_error(exc, context="Do thing failed")
```

Then register in `server.py`:

```python
from tools.do_thing import do_thing
mcp.tool()(do_thing)
```

Response envelopes (`models/responses.py`): `ToolSuccess` is
`{"ok": true, "data": ...}`; the error path yields
`{"ok": false, "error_type": ..., "message": ..., "suggestion": ..., "request_id": ...}`.
`error_type` must be one of: `authentication`, `validation`, `not_found`,
`rate_limit`, `internal`.

> **Note on tool arguments:** every tool accepts **flat** arguments
> (`{"issue_number": 1}`); a legacy `{"params": {...}}` wrapper is still
> unwrapped on every tool. This is applied centrally at registration by
> `core/arguments.py` (#4), so a new tool may declare either a single
> `params: Model` argument or plain keyword arguments — clients see the same
> flat schema either way. Do not special-case argument shapes in a tool.

---

## 5. Conventions

- **Type hints everywhere.** Every public function and every model field is
  annotated. `from __future__ import annotations` at the top of each module.
- **Prefer GraphQL.** Projects V2 is a GraphQL-native API; reach for the GraphQL
  client (`clients/graphql_client.py`, documents in `graphql/`) whenever the
  operation is available there. Fall back to the `gh` CLI (`gh_cli_client.py`)
  only for operations GraphQL does not expose.
- **Secrets are never logged.** Do not log, echo, or embed tokens in responses,
  error messages, or test fixtures. `make secrets` must stay green.
- **Errors are explicit.** No silent `except: pass`. Every caught exception is
  logged and converted to a structured error envelope.
- **Docstrings** on every module, model, and tool function (Args/Returns).
- **No host dependencies.** Anything a contributor must run goes through a
  `Makefile` target backed by Docker.
- **Keep `tools/__init__.py` and `server.py` in sync** when adding/removing a
  core tool; `make tools` guards the total count (≥ 100).

---

## 6. Contribution Workflow

**Issue-first.** Every change starts from an issue. Open (or claim) an issue
describing the problem/feature and acceptance criteria before writing code.

**Branch naming** — `<type>/<short-kebab-slug>`, where `<type>` is one of:
`feat`, `fix`, `docs`, `chore`, `refactor`. Example:
`feat/milestone-bulk-close`.

**Conventional commits** — commit messages follow
`<type>(<scope>): <summary>`, e.g. `feat(tools): add bulk milestone close`.

**One commit per PR** — squash your branch to a single, well-described commit
before opening/updating the PR. Keep the PR focused on one issue.

**Closes #** — the PR description must reference the issue it resolves with
`Closes #<n>` so the issue auto-closes on merge.

**Document in the same PR** — if a PR adds a script, CI step, tool, or changes
behavior, update the relevant docs (`README.md`, `docs/**`, and this file if the
workflow changes) in the *same* PR. No undocumented tooling.

**Before proposing a PR** — run `make validate`, confirm a clean pass, and verify
that every required GitHub Actions check has completed successfully. Pending,
failed, or missing checks block delivery as ready to review.

---

## 7. Governance Rule — Epics

> **Any body of work that spans more than one issue MUST be grouped under an
> Epic.** Create an Epic issue whose title is prefixed with `🏔️ [Epic]`
> (e.g. `🏔️ [Epic] Milestone management overhaul`), and link the member issues
> to it as sub-issues. Single-issue changes do not require an Epic.

This keeps multi-issue efforts traceable on the Projects V2 board and legible to
both human maintainers and agents picking up related work.

---

## 8. Where to Look

- Setup & IDE/MCP-client integration: `docs/SETUP.md`
- Tool catalog & usage: `docs/USAGE.md`, `docs/CAPABILITIES.md`
- Argument reference: `docs/PARAMETERS.md`
- GraphQL reference: `docs/GRAPHQL_REFERENCE.md`
- Debugging: `docs/TROUBLESHOOTING.md`
- Contribution rules (human-facing): `CONTRIBUTING.md`
- Security policy: `SECURITY.md`
