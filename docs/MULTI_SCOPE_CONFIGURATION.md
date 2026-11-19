# Multiple repositories and project scopes

There are two supported ways to span repositories:

1. **Multiple locked instances (recommended for single-user clients):** one
   named MCP server instance per repository/project target, each with its own
   `GH_PROJECT_*` tuple and `GH_PROJECT_SCOPE_LOCK=true`. Clients register
   several named servers at once; choose the server whose `server_info` target
   matches the intended repository before making changes.
2. **One organization-wide instance (for multi-user hosts such as
   LibreChat):** a single server with `GH_PROJECT_SCOPE_LOCK=false` combined
   with request-scoped bearer auth (`MCP_TRANSPORT=streamable-http` +
   `MCP_AUTH_MODE=request`). Repository-scoped tools then accept `owner`/`repo`
   overrides and every call runs under the *caller's* GitHub token — see
   [Organization-wide mode](#organization-wide-mode) below.

Do not turn off the scope lock to make one instance span repositories **when
the server authenticates with a single shared token**. The lock is an
application guard, not a GitHub permission boundary: a shared token that can
write several repositories would make an unlocked instance equally broad for
every caller. Only unlock when the server runs in request-auth mode so each
call is fenced by the caller's own token — or when you deliberately accept a
shared-token blast radius. If a token is shared, keep every instance locked
and never put its value in a committed client config.

## Organization-wide mode

With `GH_PROJECT_SCOPE_LOCK=false`, repository-scoped tools accept optional
`owner`/`repo` fields. The configured `GH_PROJECT_ORG_NAME`/
`GH_PROJECT_REPO_NAME` remain the defaults when the fields are omitted, so a
single unlocked server can target any repository the caller's token can
reach — the permission boundary becomes GitHub itself, not the server config.

What stays scoped even when unlocked:

- **Project-board tools** (`list_project_items`, `update_project_item_fields`,
  `create_project_item`, sprint/planning/bulk tools, field and view tools)
  still operate on the configured `GH_PROJECT_PROJECT_NUMBER` board. Projects
  V2 metadata has no per-call repository target.
- **`enforce_scope` still runs** — it is simply a no-op with the lock off, so
  flipping the variable back to `true` instantly restores the fence.

Typical deployment (LibreChat-style, one server per organization):

```dotenv
MCP_TRANSPORT=streamable-http
MCP_AUTH_MODE=request
GH_PROJECT_ORG_NAME=your-org
GH_PROJECT_REPO_NAME=governance-repo        # default target
GH_PROJECT_PROJECT_NUMBER=1                 # default board
GH_PROJECT_SCOPE_LOCK=false
```

Each end user supplies their own GitHub token as the request `Authorization:
Bearer` credential. Tools like `list_pull_requests`, `review_pull_request`,
`create_repository`, `comment_issue`, `edit_issue`, milestones, labels,
repository-content tools and the delete tools all honor `owner`/`repo`
overrides under that caller's permissions.

### GitHub App / OAuth app installation

When the caller's credential is a GitHub App *user* token (OAuth user-to-server
or app installation token minted for a user), two gates apply — both must
allow the repository:

1. **The app's installation repository access.** On the app installation page
   (`https://github.com/organizations/<org>/settings/installations`), set
   **Repository access → All repositories** (or explicitly add each target
   repository) instead of only the governance repository. Without this, user
   tokens minted through that installation cannot see other repositories at
   all — GitHub returns 404s, which is the desired failure mode.
2. **The user's own permissions.** A user token can never exceed what the user
   can do on GitHub: repo role (read/triage/write/maintain/admin), branch
   protection rules, required reviews and org restrictions all still apply.
   `review_pull_request` approving a PR, `create_repository`, or issue writes
   succeed only when the user could do the same in the GitHub UI.

Minimum GitHub App permissions for the org-wide surface:

| Permission | Level | Tools |
|------------|-------|-------|
| Metadata | Read | everything (mandatory) |
| Issues | Read & Write | issue create/edit/close/comment tools |
| Pull requests | Read & Write | PR listing/detail, `review_pull_request`, create/update/merge PR |
| Contents | Read (Write for commit tools) | `get_repository_file`, `create_branch`, `commit_files` |
| Actions | Read | workflow run/check tools |
| Projects (organization) | Read & Write | Projects V2 board tools |
| Administration | Write (optional) | `create_repository` inside the org |

For a classic PAT or OAuth bearer instead of a GitHub App, the token needs
`repo` (or fine-grained equivalents), `project` and `read:org`; the validator
in `core/auth.py` checks the scope hierarchy.

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

- Multi-instance mode: one server name and one fixed target tuple per
  repository/project, `GH_PROJECT_SCOPE_LOCK=true` for every instance.
- Organization-wide mode: a single instance with
  `GH_PROJECT_SCOPE_LOCK=false` **only** together with request-scoped auth,
  so each call is fenced by the caller's token.
- Correct owner type and Project V2 number for each target.
- Credentials stay in ignored env files or a secret store; never in prompts or
  committed JSON.
- Confirm `server_info` before a write and update the correct instance's
  Status as the issue/PR moves through its lifecycle.
