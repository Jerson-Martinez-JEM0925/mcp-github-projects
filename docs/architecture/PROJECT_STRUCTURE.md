# Project Structure & Conventions

This document is the single source of truth for **where code lives**, **how it is
named**, and **how to add to it** in the `mcp-github-projects` server. It exists so
that a contributor (human or LLM) can place a new tool, module, or test correctly on
the first try, without reverse-engineering the layout.

> **Scope:** organization and naming only. This document describes *structure*, not
> behavior. The set of client-facing MCP tool names and the total tool count (**106**)
> are an external contract — see [Invariants](#invariants).

---

## 1. Directory Layout

```
mcp-github-projects/
├── server.py                # MCP entry point: builds FastMCP, registers every tool
├── __main__.py              # `python -m` shim → server.main()
├── __init__.py              # package metadata / settings re-export
│
├── core/                    # Cross-cutting infrastructure (no GitHub domain logic)
│   ├── config.py            # Settings model + get_settings()
│   ├── auth.py              # Token resolution + scope validation
│   ├── access.py            # MCP_ACCESS_LEVEL gating + GH_PROJECT_SCOPE_LOCK
│   ├── factory.py           # ServiceFactory: the ONLY place clients/services are built
│   ├── protocols.py         # GraphQLExecutor / GHCLIRunner transport interfaces
│   ├── context.py           # Per-call RequestContext (correlation ID, log filter)
│   ├── error_handling.py    # build_error_response() / handle_tool_error()
│   ├── exceptions.py        # Typed exception hierarchy
│   ├── hardening.py         # Input parsing, redaction, atomic writes
│   ├── profiles.py          # Named env-profile loading
│   └── capabilities.py      # Tool → required-capability mapping (contract-checked)
│
├── clients/                 # Thin transport wrappers over external systems
│   ├── graphql_client.py    # GitHub GraphQL v4 client (retry, context)
│   ├── gh_cli_client.py     # `gh` CLI subprocess wrapper
│   └── cache_manager.py     # On-disk project-metadata cache (per-target isolation)
│
├── graphql/                 # GraphQL documents + response helpers (no I/O)
│   ├── queries.py           # Read documents + variable/extraction helpers
│   └── mutations.py         # Write documents
│
├── models/                  # Pydantic models & typed data structures
│   ├── context.py           # GitHubContext / ProjectTarget
│   ├── items.py             # Project item / create-input models
│   ├── metadata.py          # ProjectMetadata cache shapes
│   └── responses.py         # ToolSuccess / ToolError envelopes
│
├── services/                # Domain logic; orchestrates clients + graphql + models
│   ├── discovery_service.py # Resolve board/field node IDs
│   ├── project_service.py   # Project board operations
│   ├── issue_service.py     # Issue read/write helpers
│   ├── field_service.py     # Field value resolution + validation
│   └── owner_type_resolver.py # user vs organization auto-detection
│
├── tools/                   # MCP tool functions, grouped by domain category
│   ├── discovery/           # ID discovery & board listing
│   ├── issues/              # Issue CRUD, comments, sub-issues, lifecycle
│   ├── pull_requests/       # PR ↔ issue linkage & closure readiness
│   ├── projects/            # Board item placement / archive / status
│   ├── fields/              # Field, estimate, label, milestone writes
│   ├── planning/            # Sprint planning, workflows, release notes
│   ├── bulk/                # Multi-item batch operations & search
│   └── meta/                # Stats, PR helpers, and the capability suite
│
├── tests/                   # pytest suite (mirrors module names: test_<area>.py)
├── scripts/                 # Operational helpers (validate, count, mcp_call, …)
└── docs/                    # Documentation (this file lives in docs/architecture/)
```

### Backward-compatibility layer

The physical reorganization keeps a set of **thin re-export shim modules** at the
historical import paths (e.g. `config.py`, `hardening.py`, `tools/workflows.py`,
`tools/capability_suite.py`). Each shim does nothing but re-export from the new home:

```python
# config.py (shim)
from core.config import *          # noqa: F401,F403
from core.config import get_settings, GitHubProjectSettings  # explicit re-exports
```

These shims exist so that pinned imports in the test suite, the `scripts/`, and any
downstream tooling keep resolving byte-identically. **New code must import from the
new canonical path** (`from core.config import get_settings`), never from a shim.
Shims are a migration aid, not a public surface.

---

## 2. Naming Conventions

| Kind | Convention | Example |
|------|-----------|---------|
| Modules / packages | `snake_case`, one concern per module | `field_service.py`, `pull_requests/` |
| Tool functions | `snake_case` verb-first, **client-facing name = function name** | `create_project_item`, `move_to_status` |
| Pydantic input models | `<ToolName>Input` (PascalCase) | `CreateEpicInput`, `SetEstimateInput` |
| Services | `<Domain>Service` | `ProjectService`, `FieldService` |
| GraphQL documents | `UPPER_SNAKE_CASE` constant, suffix `_QUERY` / `_MUTATION` | `DISCOVERY_QUERY_USER`, `CREATE_FIELD_MUTATION` |
| Exceptions | `<Reason>Error` | `ValidationError` |
| Tests | `test_<area>.py`, classes `Test<Behavior>`, methods `test_<case>` | `test_owner_type.py::TestDiscoveryQueries` |
| Scripts | `snake_case.py` / `kebab-or-snake.sh` | `count_tools.py`, `run_contract_tests.sh` |

Rules:

- **One concern per module.** A tool module owns one cohesive operation or a small
  family of closely-related operations. If a module grows past ~600 lines or mixes
  unrelated domains, split it along the category boundaries above.
- **Tool function name is a hard contract.** The name you give an `async def` tool is
  the exact name clients call. Renaming it is a breaking change, never a refactor.
- **No domain logic in `core/` or `clients/`.** Those layers know nothing about
  issues, sprints, or epics.

---

## 3. Tool Taxonomy

The ~105 tools group into these categories. A tool's category determines its
`tools/<category>/` home and the doc section it belongs to.

| Category | `tools/` home | Responsibility | Representative tools |
|----------|---------------|----------------|----------------------|
| **discovery** | `tools/discovery/` | Resolve node IDs, list board items | `discover_ids`, `list_project_items` |
| **issues** | `tools/issues/` | Issue create/edit/close/reopen, comments, sub-issues, detail | `create_project_item`, `edit_issue`, `close_issue`, `comment_issue`, `add_sub_issue`, `get_issue_detail`, `reopen_issue` |
| **pull_requests** | `tools/pull_requests/` | PR ↔ issue linkage, closure readiness | `verify_acceptance_criteria`, `get_pr_linked_issues`, `validate_issue_closure_readiness`, `close_issue_on_pr_merge` |
| **projects** | `tools/projects/` | Board placement: status, done, trash, archive | `move_to_status`, `move_to_done`, `move_to_trash`, `archive_project_item`, `add_item_to_project`, `update_project_item_fields` |
| **fields** | `tools/fields/` | Field/estimate/label/milestone writes | `set_estimate`, `create_label`, `list_labels`, `create_milestone`, `close_milestone`, `list_milestones` |
| **planning** | `tools/planning/` | Sprints, workflows, release notes, epics | `sprint_planning`, `generate_release_notes`, `create_epic`, `daily_standup`, `sprint_review`, `close_sprint`, `triage_new_issues` |
| **discovery/bulk** | `tools/bulk/` | Batch operations + search across many items | `bulk_close_issues`, `bulk_update_items`, `search_issues`, `bulk_assign` |
| **meta** | `tools/meta/` | Reports/stats, PR-create helpers, and the 60-tool capability suite | `get_project_stats`, `get_sprint_summary`, `create_pull_request`, `update_pull_request`, `link_pull_request`, plus `capability_suite.CAPABILITY_TOOL_NAMES` |

> The **capability suite** (`tools/meta/capability_suite.py`) holds 60 read/analysis/
> text-processing tools registered via `CAPABILITY_TOOL_NAMES`. It is a category of its
> own because those tools are declared as a batch list rather than one function per
> module — see the recipe below for extending it.

Every registered tool **must** have an entry in `core/capabilities.py::TOOL_CAPABILITIES`.
A contract test (`tests/test_contracts.py::TestCapabilitiesCoverage`) fails the build if
a registered tool has no capability mapping.

---

## 4. Recipe — Add a New Tool

1. **Pick the category** from the taxonomy above and open `tools/<category>/`.
2. **Add the function** to the most relevant module (or a new module — see recipe 5):
   ```python
   from pydantic import BaseModel
   from core.error_handling import build_error_response, handle_tool_error
   from core.factory import get_service_factory
   from models.responses import ToolSuccess

   class DoThingInput(BaseModel):
       issue_number: int

   async def do_thing(params: DoThingInput) -> dict:
       """One-line summary shown to the MCP client."""
       try:
           factory = get_service_factory()
           project = await factory.project_service()  # never ProjectService(...)
           ...
           return ToolSuccess(data={...}).model_dump()
       except Exception as exc:  # narrow in real code
           return handle_tool_error(exc)
   ```
   Follow the existing arg convention of the sibling tools in that module
   (`params`-wrapped vs flat — see `docs/PARAMETERS.md`).
3. **Register it in `server.py`**: import the function and append it to `_ALL_TOOLS`.
   Registration wraps it in a `RequestContext` and only exposes it when its
   access class (step 4) is allowed by `MCP_ACCESS_LEVEL`.
   (Capability-suite tools register automatically via the list — recipe 5.)
4. **Map its capability** in `core/capabilities.py::TOOL_CAPABILITIES` using the
   minimal required set. Tools with no API calls map to `frozenset()`. The
   capability also decides its access class (read / write / delete) in
   `core/access.py`.
5. **Document it** in `docs/CAPABILITIES.md` and `docs/USAGE.md` **in the same PR**
   (documentation is a pre-PR gate, never a follow-up).
6. **Add a test** in `tests/test_<area>.py`. Drive the tool with fakes through
   `use_service_factory(...)` (see *Clients, services and tests* below) instead of
   patching module attributes.
7. **Verify the count moved as intended**: `python scripts/count_tools.py` — a *new*
   tool raises the count; a pure refactor keeps it identical.

### Clients, services and tests

`core/factory.py::ServiceFactory` is the single construction point for
`GraphQLClient`, `GHCLIClient`, `CacheManager` and the services. Tools call
`get_service_factory()` and ask it for what they need; a test in
`tests/test_architecture.py` fails if a tool constructs any of them inline.

Services and helpers type their transports with the protocols in
`core/protocols.py` (`GraphQLExecutor`, `GHCLIRunner`), so any object with the
same methods works. A test therefore needs no `unittest.mock.patch`:

```python
from core.factory import use_service_factory

class FakeGH:
    async def run(self, args): ...
    async def api_graphql(self, query, variables): ...

with use_service_factory(gh_client=FakeGH()):
    result = await close_issue(CloseIssueInput(issue_number=12))
```

Injected transports skip token resolution. The default factory builds fresh
objects per call, so production behaviour is the same as inline construction.

### Request context

Every registered tool runs inside `core.context.request_scope`. The context
carries a 12-hex-character `correlation_id`, the tool name and the start time.
It reaches two places: every stderr log line (`[<correlation_id> <tool>]`
prefix, via `RequestContextFilter`) and every error envelope
(`ToolError.correlation_id`). A user can quote the ID from an error and find the
matching log lines. It is unrelated to GitHub's own `request_id`.

### Adding to the capability suite (60-tool batch)

1. Define the `async def` in `tools/meta/capability_suite.py`.
2. Append its name to `CAPABILITY_TOOL_NAMES` (keep the count assertion in
   `test_contracts.py::test_capability_tool_names_list_matches` in sync).
3. Add its `TOOL_CAPABILITIES` entry. No `server.py` edit needed — the registration
   loop picks it up from the list.

---

## 5. Recipe — Add a New Module

1. Create `tools/<category>/<concern>.py` with a module docstring stating the one
   concern it owns.
2. Import shared infra from the **canonical** paths (`core.*`, `clients.*`,
   `graphql.*`, `models.*`, `services.*`) — never from a compatibility shim.
3. Export the tool functions and register them in `server.py` (recipe 4, steps 3–7).
4. If the module belongs to a new category, add the category to the taxonomy table in
   this file and create `tools/<category>/__init__.py`.

### Add a new service / model / core module

- **Service** (`services/`): domain orchestration that combines clients + graphql +
  models. Name it `<domain>_service.py`, class `<Domain>Service`. Register any public
  symbol in `services/__init__.py`.
- **Model** (`models/`): pure data shapes (Pydantic / dataclass). No I/O.
- **Core** (`core/`): cross-cutting infra with no GitHub domain knowledge. Adding a
  core module that historical code imports by bare name may need a root shim (see
  the compatibility layer note in §1) — prefer updating callers to the canonical path
  instead of adding new shims.

---

## 6. Invariants (do not break)

- **Tool count per access level is 65 (`read`) / 114 (`write`) / 119 (`full`).**
  A structural change must not add or drop a registered tool.
  `scripts/count_tools.py` (at `full`) and `test_contracts.py::TestToolRegistration`
  enforce ≥100 and no duplicates.
- **Client-facing tool names and input schemas are byte-identical** across a refactor.
- **Clients and services are built only through `core.factory`.**
- **`python server.py` and the Docker image build** must keep working — do not move
  `server.py`, `__main__.py`, `requirements.txt`, `Dockerfile`, `Makefile`, or the
  license/readme out of the repo root.
- **Capability coverage is total** — every registered tool has a `TOOL_CAPABILITIES`
  entry.
- **Import from canonical paths** in new code; shims are a migration aid only.

---

## 7. Best Practices

- Keep tool functions thin: validate input → call a service → wrap the result. Push
  branching logic into `services/`.
- Return `ToolSuccess(...).model_dump()` on success and `handle_tool_error(...)` /
  `build_error_response(...)` on failure — never raise raw exceptions across the tool
  boundary.
- Never log or echo a token. Use `core.hardening.redact_sensitive`.
- One logical change per commit; documentation ships with the code that needs it.
- When a change spans more than one issue, group them under an Epic (see
  `GOVERNANCE.md`).
