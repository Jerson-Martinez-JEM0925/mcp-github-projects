# Multiple Project V2 boards in one MCP instance

This server can manage more than one GitHub Project V2 board owned by the same
organization from one MCP process. The feature keeps the existing single-board
configuration as the default and adds an explicit allowlist for additional
boards.

## Configuration

Keep the existing default board settings and add a comma-separated allowlist:

```dotenv
GH_PROJECT_ORG_NAME=example-org
GH_PROJECT_REPO_NAME=example-repo
GH_PROJECT_PROJECT_NUMBER=1
GH_PROJECT_PROJECT_NUMBERS=1,2
GH_PROJECT_OWNER_TYPE=organization
GH_PROJECT_SCOPE_LOCK=true
```

`GH_PROJECT_PROJECT_NUMBER` remains mandatory and is the default used when a
call omits `project_number`. `GH_PROJECT_PROJECT_NUMBERS` adds boards that may
be selected explicitly. The default is always included, even when it is not
repeated in the list.

The value is validated at startup. It must contain unique or repeated
comma-separated integers between 1 and 100000. Repeated values are normalized.
Malformed values fail closed with a configuration error.

## Selecting a board

Project-board tools accept an optional `project_number` selector. Examples:

```json
{"project_number": 6}
```

When omitted, the configured default board is used. When supplied, the number
must belong to `GH_PROJECT_PROJECT_NUMBERS` (or be the default board). The
selector is checked before any GitHub API call.

The selector is available on discovery, item listing and mutation tools,
board field and view tools, estimate management, epic creation, project
reports, Project V2 progress reports, consistency checks, and other tools that
read or mutate board metadata.

A project item ID is board-specific. Always use the same `project_number` when
looking up and updating an item, and do not reuse an item ID from another
board.

## Scope and safety

`GH_PROJECT_SCOPE_LOCK=true` still fences owner and repository overrides. The
configured project allowlist is additive: a locked instance may target only the
listed project numbers. With the lock disabled, repository-scoped tools retain
their existing behavior, but project-board selectors are still bounded by the
explicit `GH_PROJECT_PROJECT_NUMBERS` list when it is configured.

Use separate MCP instances when boards require different credentials or
security boundaries. Use one multi-project instance only when the same token is
intentionally authorized for every listed board.

## Diagnostics

Call `server_info` before a write. The response includes:

- `target.project_number`: the default board;
- `target.project_numbers`: the normalized allowed board list;
- `scope_lock`: the active owner/repository/project policy.

Call `discover_ids` with the intended `project_number` before using board
fields. Each board has independent field IDs, options, views, and cache data.
The metadata cache is isolated per board so discovery from one project cannot
be reused for another.

## Docker and Kiro

The environment file remains the source of credentials and target defaults:

```bash
docker run --rm -i \
  --env-file /secure/github-project-mcp/.env \
  mcp-github-projects:latest
```

Do not place a token in a client configuration. The MCP client sends
`project_number` in the tool call; it does not need a second process or a
second token for each board.

## Migration

Existing clients require no changes. Keep only
`GH_PROJECT_PROJECT_NUMBER` for the original single-board behavior. Add
`GH_PROJECT_PROJECT_NUMBERS` when a second board is ready, then verify each
board with `server_info` and `discover_ids` before enabling writes.
