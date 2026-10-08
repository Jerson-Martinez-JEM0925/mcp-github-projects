# Setup Guide

## Prerequisites

| Requirement | Purpose | Install |
|-------------|---------|---------|
| Docker 20+ | Container runtime for the MCP server | [docs.docker.com/get-docker](https://docs.docker.com/get-docker/) |
| GitHub Token | API authentication (PAT classic or fine-grained) | See [Token Generation](#token-generation) |
| GitHub CLI (optional) | Alternative auth + host utilities | [cli.github.com](https://cli.github.com/) |

Run the preflight check to validate all prerequisites:

```bash
./scripts/preflight.sh
# Or with automatic resolution:
./scripts/preflight.sh --fix
```

---

## Authentication Architecture

The MCP resolves credentials in a strict priority chain. The first valid token wins:

```
┌─────────────────────────────────────────────────────────┐
│  1. GITHUB_TOKEN env var (highest priority)             │
│  2. GH_TOKEN env var (fallback)                         │
│  3. gh auth token (CLI-based, requires gh installed)    │
└─────────────────────────────────────────────────────────┘
```

**Design invariants:**
- Tokens are NEVER persisted in files tracked by git
- Tokens are NEVER printed to stdout, logs, or diagnostics
- The MCP validates scopes at startup and fails fast with actionable messages
- Scope hierarchy is honored (`admin:org` implies `read:org`)

### Local Development

Set your token as an environment variable:

```bash
# Option A: Export in shell session
export GITHUB_TOKEN=ghp_your_token_here

# Option B: Add to shell profile (~/.bashrc, ~/.zshrc)
echo 'export GITHUB_TOKEN=ghp_...' >> ~/.bashrc

# Option C: Use .env file (auto-loaded by Docker --env-file)
echo 'GITHUB_TOKEN=ghp_...' > .env
# ⚠️ .env is in .gitignore — never commit it
```

### CI/CD (GitHub Actions)

```yaml
env:
  GITHUB_TOKEN: ${{ secrets.GH_TOKEN }}
  GH_PROJECT_ORG_NAME: your-org
  GH_PROJECT_REPO_NAME: your-repo
  GH_PROJECT_PROJECT_NUMBER: "1"
```

Store the token in **Settings → Secrets and variables → Actions** as `GH_TOKEN`.

### gh CLI Authentication (Alternative)

If you prefer not to manage tokens manually:

```bash
gh auth login --scopes "repo,project,read:org"
gh auth status  # Verify
```

The MCP container includes `gh` internally. When no `GITHUB_TOKEN` env var is set, it falls back to `gh auth token` — but this requires the host's gh session to be mounted or the token exported.

---

## Token Generation

### Classic Personal Access Token

1. Navigate to [github.com/settings/tokens](https://github.com/settings/tokens)
2. **Generate new token (classic)**
3. Configure:
   - Name: `github-project-mcp`
   - Expiration: 90 days
   - Scopes:

| Scope | Purpose |
|-------|---------|
| `repo` | Issue CRUD, PR operations, repository access |
| `project` | Project board read/write (items, fields, status) |
| `read:org` | Organization membership resolution |

4. Copy the token immediately (shown only once)

### Fine-Grained Personal Access Token

1. Navigate to [github.com/settings/tokens?type=beta](https://github.com/settings/tokens?type=beta)
2. **Generate new token**
3. Configure:
   - Name: `github-project-mcp`
   - Resource owner: Your target organization
   - Repository access: Select target repository
   - Permissions:

| Permission | Level | Purpose |
|------------|-------|---------|
| Issues | Read & Write | Create, edit, close issues |
| Projects | Read & Write | Board management |
| Metadata | Read | Repository metadata |
| Organization → Projects | Read & Write | Org-level project access |
| Organization → Members | Read | Resolve assignees |

> **Note:** Fine-grained tokens do not report scopes via `X-OAuth-Scopes` header. The MCP detects this and skips scope validation for `github_pat_*` tokens.

---

## Token Rotation

Rotate credentials periodically (recommended: every 90 days) or immediately if compromised.

### Rotation Procedure

```bash
# 1. Generate a new token (see Token Generation above)
#    Copy the new token value

# 2. Update your local environment
export GITHUB_TOKEN=ghp_new_token_here
# Or update .env file:
echo 'GITHUB_TOKEN=ghp_new_token_here' > .env

# 3. Verify the new token works
./scripts/preflight.sh

# 4. Revoke the old token
#    Go to: github.com/settings/tokens
#    Find the old token → Delete

# 5. If using CI/CD, update the secret
#    Settings → Secrets → Actions → GH_TOKEN → Update
```

### Post-Rotation Checklist

- [ ] New token has required scopes (`repo`, `project`, `read:org`)
- [ ] MCP server starts successfully with `./scripts/preflight.sh`
- [ ] Old token is revoked (not just unused)
- [ ] CI/CD secret is updated if applicable
- [ ] No residual references to old token in shell history

### Emergency Rotation (Token Compromised)

```bash
# 1. IMMEDIATELY revoke at github.com/settings/tokens
# 2. Check audit log: github.com/settings/security-log
# 3. Generate replacement with same scopes
# 4. Scan for unauthorized access in the audit log
# 5. Run secret scanner to verify no persistence:
./scripts/scan_secrets.sh --history
```

---

## Target Configuration

The MCP requires three mandatory fields identifying the GitHub Project:

| Variable | Description | Example |
|----------|-------------|---------|
| `GH_PROJECT_ORG_NAME` | GitHub owner (org or user login) | `my-org` |
| `GH_PROJECT_REPO_NAME` | Repository name | `my-repo` |
| `GH_PROJECT_PROJECT_NUMBER` | Project V2 board number | `1` |
| `GH_PROJECT_OWNER_TYPE` | `auto` (default), `organization`, or `user` | `auto` |

> **Owner-type auto-detection.** `GH_PROJECT_OWNER_TYPE` defaults to `auto`:
> the server asks GitHub what `GH_PROJECT_ORG_NAME` actually is (via a
> `repositoryOwner(login:){ __typename }` GraphQL query) and picks the right
> query variant automatically. This means **user-owned boards work with no
> extra configuration** — you no longer need to set
> `GH_PROJECT_OWNER_TYPE=user` for a personal project.
>
> Set the value explicitly (`organization` or `user`) to skip detection —
> useful in fully offline CI, or when the `gh` CLI is not available. If
> detection cannot run (no `gh`, no auth, or a network error) the server falls
> back to `organization` and logs a hint to set `GH_PROJECT_OWNER_TYPE=user`.

> **Startup exit codes.** A missing or invalid setting (no target fields, an
> unknown `MCP_ACCESS_LEVEL`, a malformed number) is reported as a single
> `Configuration error: …` line on stderr, after the explanatory help block,
> and the process exits with code **2** — never a Python traceback. Code **1**
> means authentication/scope validation failed; **0** is a normal shutdown.

#### Board field defaults

When `create_project_item` (default) or `update_project_item_fields` with
`apply_defaults: true` runs, any board field the caller omits is filled from
these settings, so a board item never lands with empty custom fields. All are
optional and only apply to fields that exist on the board.

| Variable | Purpose | Default |
|----------|---------|---------|
| `GH_PROJECT_DEFAULT_DUE_DAYS` | Days from today for the default Due date | `7` |
| `GH_PROJECT_DEFAULT_ESTIMATE` | Default Estimate (NUMBER field) | `3` |
| `GH_PROJECT_DEFAULT_PRIORITY` | Default Priority option (if valid on board) | `Medium` |
| `GH_PROJECT_DEFAULT_AREA` | Default Area option (empty = no default) | *(empty)* |
| `GH_PROJECT_DEFAULT_WORK_TYPE` | Work Type when not inferable from labels | *(empty → `Feature`)* |
| `GH_PROJECT_ENFORCE_FIELDS` | Strict mode: `update_project_item_fields` errors if any board field is still unset after defaults | `false` |

> **Work Type inference.** When Work Type is omitted, a `bug` label maps it to
> `Bug`; otherwise `GH_PROJECT_DEFAULT_WORK_TYPE` is used, falling back to
> `Feature`. Status defaults to the board's first option.
>
> **Strict mode.** Set `GH_PROJECT_ENFORCE_FIELDS=true` to make
> `update_project_item_fields` refuse to complete while board fields remain
> unset (after defaults). A per-call `enforce` argument overrides this.

### Using Profiles

Pre-built profiles live in `profiles/`:

```bash
# For an organization project:
cp profiles/example-org.env .env
# Edit with your values

# For a personal (user) project:
cp profiles/user-example.env .env
# Edit with your values
```

When a named profile is loaded by `GH_PROJECT_PROFILE`, the values from that
profile file take precedence over process-level `GH_PROJECT_*` variables for
its target and cache namespace. Profile files must not contain tokens, and the
metadata cache is namespaced by owner, repository, and project number.

### Using Environment Variables

```bash
export GH_PROJECT_ORG_NAME=my-org
export GH_PROJECT_REPO_NAME=my-repo
export GH_PROJECT_PROJECT_NUMBER=1
# Owner type is auto-detected by default; set it explicitly to skip detection.
export GH_PROJECT_OWNER_TYPE=auto
```

---

## Building and Running the Container

### Build

```bash
docker build -t mcp-github-projects:latest .
```

The image is ~150MB, based on `python:3.12-slim`, includes `gh` CLI, and runs as non-root user `mcp` (uid 1000).

### Run (stdio transport)

The MCP communicates via JSON-RPC over stdin/stdout. It is not a long-running daemon — it starts, processes requests, and exits.

```bash
# Minimal (env vars set in shell):
docker run --rm -i \
  -e GITHUB_TOKEN \
  -e GH_PROJECT_ORG_NAME \
  -e GH_PROJECT_REPO_NAME \
  -e GH_PROJECT_PROJECT_NUMBER \
  mcp-github-projects:latest

# With .env file:
docker run --rm -i \
  --env-file .env \
  mcp-github-projects:latest

# With a named profile:
docker run --rm -i \
  -e GITHUB_TOKEN \
  --env-file profiles/example-org.env \
  mcp-github-projects:latest
```

<a id="ide-integration"></a>
### HTTP transport

Set `MCP_TRANSPORT=streamable-http` to run the server over FastMCP's stateless
Streamable HTTP transport. The default remains stdio. HTTP binds to
`127.0.0.1:8080` and serves MCP at `/mcp` unless overridden:

```dotenv
MCP_TRANSPORT=streamable-http
MCP_HTTP_HOST=127.0.0.1
MCP_HTTP_PORT=8080
MCP_HTTP_PATH=/mcp
```

The server also exposes dependency-free `GET /healthz` and `GET /readyz` probes.
Stateless mode keeps no MCP sessions in memory and is suitable for replicated
instances. Health probes do not require credentials. The MCP endpoint is
fail-closed by default: every request must carry its own bearer token.

### HTTP transport and per-user credentials

Authentication is selected automatically: `env` for stdio and `request` for
Streamable HTTP. HTTP `env` mode is rejected unless
`MCP_ALLOW_SHARED_TOKEN=true` is explicitly set for a trusted local deployment.
Request mode never reads `GITHUB_TOKEN`, `GH_TOKEN`, or `gh auth token` as a
fallback, and the request credential is not stored in process environment,
logs, or a global cache.

| Variable | Default | Purpose |
|---|---|---|
| `MCP_TRANSPORT` | `stdio` | `stdio` or `streamable-http` |
| `MCP_HTTP_HOST` | `127.0.0.1` | HTTP bind address |
| `MCP_HTTP_PORT` | `8080` | HTTP listen port |
| `MCP_HTTP_PATH` | `/mcp` | MCP endpoint path |
| `MCP_AUTH_MODE` | `env` / `request` | Shared env auth or per-request bearer auth |
| `MCP_ALLOW_SHARED_TOKEN` | `false` | Required to opt into HTTP `env` mode |
| `MCP_CLIENT_KEY` | unset | Optional constant-time `X-MCP-Client-Key` check |
| `MCP_ALLOWED_TOKEN_PREFIXES` | `ghu_,github_pat_` | Comma-separated request-token prefixes; empty disables filtering |

A missing/malformed bearer, wrong client key, or rejected prefix returns `401`
with `WWW-Authenticate: Bearer` and a generic JSON body. The default GitHub
prefixes intentionally reject classic `ghp_` tokens; override them only when
there is a documented token format requirement.

LibreChat can send a per-user PAT through request headers:

```yaml
mcpServers:
  github-governance:
    type: streamable-http
    url: "http://mcp-github:8080/mcp"
    startup: false
    requiresOAuth: false
    headers:
      Authorization: "Bearer {{GITHUB_PAT}}"
      X-MCP-Client-Key: "${MCP_GITHUB_CLIENT_KEY}"
    customUserVars:
      GITHUB_PAT:
        title: "GitHub fine-grained token"
        sensitive: true
```

The `oauth` block for GitHub App user access tokens will be documented separately.
The HTTP endpoint supports MCP `initialize` and `tools/list` using JSON
responses. Existing stdio clients and the stdio startup/authentication path are
unchanged.

### MCP Client Integration

The server works with **any MCP client** that supports the stdio transport. The
config shape is the same everywhere — only the location of the config file
differs per client. Add an entry pointing the client at the `docker run` command:

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

Notes:

- The server key (`github-projects`) is arbitrary — it is only the display name.
- Prefer `--env-file` so no token value appears in the config JSON. If you pass
  `-e GITHUB_TOKEN` instead, Docker forwards the variable **name** and reads its
  value from the host environment at runtime.
- Consult your MCP client's documentation for where its server-config file lives.

### Verify

```bash
# Quick smoke test:
echo '{}' | docker run --rm -i \
  --env-file .env \
  mcp-github-projects:latest

# Full preflight:
./scripts/preflight.sh

# Secret scan:
./scripts/scan_secrets.sh
```

---

## Security Practices

### What is safe to commit

- Your MCP client's server-config file — it contains the command structure, NOT token values
- `profiles/*.env` — target config (org/repo/project), NOT tokens
- `docs/SETUP.md` — this file

### What must NEVER be committed

- `.env` files with `GITHUB_TOKEN=ghp_...` values
- Shell history containing token values
- Docker run commands with inline token values in CI logs

### Secret Scanning

Run before publishing or sharing the repository:

```bash
# Scan working tree
./scripts/scan_secrets.sh

# Scan git history (pre-publication)
./scripts/scan_secrets.sh --history
```

### Cache Isolation

Each target gets its own cache directory:
```
~/.cache/github-project-mcp/{org}/{repo}/project-{N}/metadata.json
```

Cache files have `0600` permissions and are never shared between targets.

---

## Troubleshooting

| Problem | Diagnostic | Fix |
|---------|-----------|-----|
| "Missing required configuration" | Missing env vars | Set `GH_PROJECT_ORG_NAME`, `REPO_NAME`, `PROJECT_NUMBER` |
| "No GitHub token found" | No token in any source | Set `GITHUB_TOKEN` or run `gh auth login` |
| "Insufficient token scopes" | Token missing permissions | Regenerate with `repo`, `project`, `read:org` |
| "Permission denied" on cache | Container user issue | Image uses `/home/mcp/.cache` (writable) |
| Token works locally but not in CI | Secret not set | Check Settings → Secrets → `GH_TOKEN` |
| Fine-grained token "scope error" | FG tokens don't report scopes | MCP auto-detects and skips validation |

The production image installs only `requirements.txt`. `make test` and the CI
test job build a separate image with pinned `requirements-dev.txt`, including
pytest, so runtime artifacts stay smaller and the validation path remains
explicit and reproducible.
