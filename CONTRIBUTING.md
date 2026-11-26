# Contributing to GitHub Project MCP Server

Thank you for your interest in contributing! This guide will help you get started.

## Development Environment

1. Docker (required — all code runs inside containers)
2. GitHub CLI (`gh`) authenticated
3. Python 3.12+ (for local syntax checks only)

## Quick Start

```bash
# Clone the repository
git clone https://github.com/jersonmartinez/mcp-github-projects.git
cd mcp-github-projects

# Copy a profile
cp profiles/example-org.env .env
# Edit .env with your target org/repo/project

# Build and verify
docker build -t mcp-github-projects:latest .
docker run --rm -e GH_PROJECT_ORG_NAME=YourOrg -e GH_PROJECT_REPO_NAME=YourRepo \
  -e GH_PROJECT_PROJECT_NUMBER=1 mcp-github-projects:latest \
  python3 tests/test_owner_type.py
```

## Branch Naming

```
<type>/<description-in-kebab-case>
```

| Type | When |
|------|------|
| `feat/` | New tool or functionality |
| `fix/` | Bug fix |
| `docs/` | Documentation only |
| `chore/` | Maintenance, deps |
| `refactor/` | Code restructuring |

## Project Structure

Before adding or moving code, read
[docs/architecture/PROJECT_STRUCTURE.md](docs/architecture/PROJECT_STRUCTURE.md).
It is the source of truth for the directory layout, naming conventions, the
**tool taxonomy** (which `tools/<category>/` a tool belongs to), and the
step-by-step recipes for adding a tool or a module. It also lists the hard
invariants a refactor must preserve (tool count, byte-identical tool names,
working entrypoint/image build).

## Adding a New Tool

Full recipe: [PROJECT_STRUCTURE.md §4](docs/architecture/PROJECT_STRUCTURE.md#4-recipe--add-a-new-tool).
In short:

1. Pick the category and create/extend `tools/<category>/<concern>.py` with:
   - Pydantic `<ToolName>Input` model
   - Async tool function with a Google-style docstring
   - Error handling via `core.error_handling.build_error_response` / `handle_tool_error`
2. Register in `server.py`: `mcp.tool()(your_tool_function)`
3. Add the capability mapping in `core/capabilities.py`
4. Document it in `docs/CAPABILITIES.md` + `docs/USAGE.md` **in the same PR**
5. Add tests in `tests/test_<area>.py`
6. Verify the count moved as intended: `python scripts/count_tools.py`

Import shared infrastructure from the **canonical** paths (`core.*`, `clients.*`,
`graphql.*`, `models.*`, `services.*`) — never from a backward-compatibility shim.

## Pull Request Requirements

- Title follows conventional commits: `feat(scope): description`
- Body includes `Closes #<issue_number>`
- All Python files pass `ast.parse` syntax check
- Docker image builds successfully
- New tools have at least one test
- Every required GitHub Actions check has completed successfully before the PR is presented as ready; pending, failed, or missing checks block delivery
- Do not manually bump `core/version.py` or create release tags. After merge, `.github/workflows/release-automation.yaml` versions, tags, and publishes automatically; use `release:none` only when the PR must not publish artifacts. See [docs/RELEASING.md](docs/RELEASING.md)

## Code Style

- Python 3.12, type hints on all public functions
- Docstrings on all public functions (Google style)
- No `any` type hints
- Error handling: never silent catches, always log or raise
- Security: never log tokens, never serialize credentials

## Reporting Issues

Use the issue templates in `.github/ISSUE_TEMPLATE/`:
- Bug reports: include reproduction steps and expected vs actual behavior
- Feature requests: describe the use case and proposed API

## Security

See [SECURITY.md](SECURITY.md) for reporting vulnerabilities.
