# Multiple repositories and project scopes

Run one named MCP server instance per repository/project target. Each instance
has its own `GH_PROJECT_*` target and keeps `GH_PROJECT_SCOPE_LOCK=true`.
Clients can register several named servers at once; choose the server whose
`server_info` target matches the intended repository before making changes.

Do not turn off the scope lock to make one instance span repositories. The
server is configured around one owner/repository/project tuple, and the lock
rejects tools that try to escape that tuple. The lock is an application guard,
not a GitHub permission boundary: prefer a fine-grained token per repository
when practical. If a token is shared, keep every instance locked and never put
its value in a committed client config.

## Target-specific environment

Each instance needs the correct owner, repository, project number, and owner
type:

```dotenv
GH_PROJECT_ORG_NAME=example-org
GH_PROJECT_REPO_NAME=finance-app
GH_PROJECT_PROJECT_NUMBER=1
GH_PROJECT_OWNER_TYPE=organization
GH_PROJECT_SCOPE_LOCK=true
```

For a user-owned board, use the user login and `GH_PROJECT_OWNER_TYPE=user`.
Project numbers are specific to their owner and can differ even when the
repository name is the same. Store credentials in a gitignored `.env` file or
the client/OS secret store. A per-target `.env` is clearest when targets use
different credentials; a shared env file can be combined with Docker `-e`
overrides when the same token is deliberately authorized for both targets.

Verify every server after registration:

1. Call `server_info` and confirm `scope_lock.enabled` and the complete
   `target` (owner, repository, project number, owner type).
2. Call `discover_ids` with `force: true` to verify access to that board.
3. If a tool reports a scope violation, stop and correct the target config;
   do not disable the lock to work around it.

## Devin

On Windows, Devin's user-level MCP file is
`%APPDATA%\devin\mcp_config.json`. On macOS/Linux it is
`~/.config/devin/mcp_config.json`. The file maps unique names under
`mcpServers` to stdio commands. The following illustrative Windows config
registers two separate, scope-locked targets; replace the example values with
your own:

- `org-board` targets `example-org/finance-app`, organization Project 1.
- `user-board` targets `example-user/finance-app`, user Project 2.

```json
{
  "mcpServers": {
    "org-board": {
      "command": "docker",
      "args": [
        "run", "--rm", "-i",
        "--env-file", "C:/secure/org-board/.env",
        "mcp-github-projects:latest", "python", "server.py"
      ],
      "transport": "stdio"
    },
    "user-board": {
      "command": "docker",
      "args": [
        "run", "--rm", "-i",
        "--env-file", "C:/secure/user-board/.env",
        "-e", "GH_PROJECT_ORG_NAME=example-user",
        "-e", "GH_PROJECT_REPO_NAME=finance-app",
        "-e", "GH_PROJECT_PROJECT_NUMBER=2",
        "-e", "GH_PROJECT_OWNER_TYPE=user",
        "-e", "GH_PROJECT_SCOPE_LOCK=true",
        "-e", "GH_PROJECT_WRITE_BRANCH_PREFIXES=feat/",
        "mcp-github-projects:latest", "python", "server.py"
      ],
      "transport": "stdio"
    }
  }
}
```

Each env file contains its own target values and credentials. Keep the entries
under distinct keys in the same `mcpServers` object.
Changes to `%APPDATA%\devin\mcp_config.json` are user-local, not repository
changes. See the [Devin MCP configuration
guide](https://docs.devin.ai/cli/extensibility/mcp/configuration).

## Kiro

Kiro supports user-level `~/.kiro/settings/mcp.json` and workspace-level
`.kiro/settings/mcp.json`; Windows user settings live under
`%USERPROFILE%\.kiro\settings\mcp.json`. Add one `mcpServers` entry per target,
using a different name for each:

```json
{
  "mcpServers": {
    "org-board": {
      "command": "docker",
      "args": [
        "run", "--rm", "-i", "--env-file", "/secure/org-board/.env",
        "mcp-github-projects:latest", "python", "server.py"
      ],
      "disabled": false
    },
    "user-board": {
      "command": "docker",
      "args": [
        "run", "--rm", "-i", "--env-file", "/secure/user-board/.env",
        "mcp-github-projects:latest", "python", "server.py"
      ],
      "disabled": false
    }
  }
}
```

Each env file contains that target's own `GH_PROJECT_*` variables and
`GH_PROJECT_SCOPE_LOCK=true`; it is not checked into the repository. Kiro
reconnects local MCP servers when the config is saved. See
[Kiro MCP configuration](https://kiro.dev/docs/mcp/).

## OpenCode

OpenCode's global config is `~/.config/opencode/opencode.json` or
`opencode.jsonc`; a project can add entries in `opencode.json(c)` or
`.opencode/opencode.json(c)`. Local server definitions live under
`mcp.servers` and use a command array:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "servers": {
      "org-board": {
        "type": "local",
        "command": [
          "docker", "run", "--rm", "-i",
          "--env-file", "/secure/org-board/.env",
          "mcp-github-projects:latest", "python", "server.py"
        ]
      },
      "user-board": {
        "type": "local",
        "command": [
          "docker", "run", "--rm", "-i",
          "--env-file", "/secure/user-board/.env",
          "mcp-github-projects:latest", "python", "server.py"
        ]
      }
    }
  }
}
```

Use separate env files or add target-specific `-e` arguments as in the Devin
example. Keep token values out of tracked `opencode.json(c)` files. See
[OpenCode MCP servers](https://opencode.ai/docs/mcp-servers/).

## Claude Code

For user-only servers, register each instance with a unique name and
`--scope user`:

```sh
claude mcp add --scope user org-board -- \
  docker run --rm -i --env-file /secure/org-board/.env \
  mcp-github-projects:latest python server.py

claude mcp add --scope user user-board -- \
  docker run --rm -i --env-file /secure/user-board/.env \
  mcp-github-projects:latest python server.py
claude mcp list
```

Claude Code also supports project `.mcp.json` and local/user scope. A project
`.mcp.json` is commonly committed, so use it only for non-secret, portable
configuration; keep machine-specific env-file paths and credentials in a
user-scoped configuration. See
[Claude Code MCP](https://code.claude.com/docs/en/mcp).

## Scope checklist

- One server name and one fixed target tuple per repository/project.
- `GH_PROJECT_SCOPE_LOCK=true` for every instance.
- Correct owner type and Project V2 number for each target.
- Credentials stay in ignored env files or a secret store; never in prompts or
  committed JSON.
- Confirm `server_info` before a write and update the correct instance's
  Status as the issue/PR moves through its lifecycle.
