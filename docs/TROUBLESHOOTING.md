# Troubleshooting

Common issues encountered when using the GitHub Project Management MCP Server, with symptoms, causes, and resolutions.

---

## Server exits with code 2 at startup

**Symptom:** the process exits immediately with status `2` and stderr ends with
`Configuration error: <reason>` (no Python traceback).

**Cause:** a required or invalid setting — usually `GH_PROJECT_ORG_NAME`,
`GH_PROJECT_REPO_NAME` or `GH_PROJECT_PROJECT_NUMBER` unset, or an unknown
`MCP_ACCESS_LEVEL`.

**Resolution:** set the variable named in the message (via `.env` or
`--env-file`) and restart. See [SETUP.md → Target Configuration](SETUP.md#target-configuration).
Exit code `1` is different: it means token/scope validation failed (see below).

---

## Invalid Token

**Symptom:** The MCP server exits at startup with the error:
```
Error: The provided token is invalid or expired. Please provide a valid GitHub token via GITHUB_TOKEN or GH_TOKEN environment variable, or authenticate via `gh auth login`.
```

**Cause:** The token configured in `GITHUB_TOKEN` (or `GH_TOKEN`) was revoked, has expired, or the environment variable name is incorrect. This can also happen if a fine-grained token's expiration date has passed.

**Resolution:**
1. Generate a new Personal Access Token at [github.com/settings/tokens](https://github.com/settings/tokens)
2. Ensure the token has `repo`, `project`, and `read:org` scopes (classic) or equivalent permissions (fine-grained)
3. Update the `.env` file with the new token value in the `GITHUB_TOKEN` variable
4. Restart the MCP server

---

## Insufficient Scopes

**Symptom:** The MCP server exits at startup with the error:
```
Error: Insufficient token scopes. Missing scopes: project, read:org. Required scopes: repo, project, read:org.
```

**Cause:** The token is valid but lacks one or more required OAuth scopes. The server requires `repo` (for issue operations), `project` (for Projects V2 mutations), and `read:org` (for organization project access).

**Resolution:**
1. Navigate to your token settings at [github.com/settings/tokens](https://github.com/settings/tokens)
2. Edit the token or generate a new one with all three required scopes: `repo`, `project`, `read:org`
3. Update the `.env` file with the corrected token
4. Restart the MCP server

---

## Rate Limiting

**Symptom:** A tool invocation returns an error response with `error_type: "rate_limit"` and includes a reset timestamp:
```json
{
  "ok": false,
  "error_type": "rate_limit",
  "message": "GitHub API rate limit exceeded. Resets at 2024-01-15T14:30:00Z.",
  "suggestion": "Wait until the reset time before retrying. Consider reducing request frequency."
}
```

**Cause:** The authenticated user has exceeded the GitHub API rate limit (5,000 requests per hour for authenticated requests). This typically happens during heavy automation or rapid successive tool calls.

**Resolution:**
1. Wait until the reset timestamp indicated in the error response
2. Check current rate limit status: `gh api rate_limit`
3. Reduce request frequency if hitting limits regularly
4. Consider batching operations where possible (e.g., updating multiple fields in a single `update_project_item_fields` call)

---

## Non-existent Item IDs

**Symptom:** A tool invocation returns an error response with `error_type: "not_found"`:
```json
{
  "ok": false,
  "error_type": "not_found",
  "message": "Project item 'PVTI_abc123' not found.",
  "suggestion": "Run discover_ids or list_project_items to obtain valid item IDs."
}
```

**Cause:** The provided project item node ID does not correspond to an existing item. The item may have been deleted, permanently archived, or the ID is malformed or from a different project.

**Resolution:**
1. Run `list_project_items` to get current valid item IDs
2. Run `discover_ids` to refresh the project metadata cache
3. Verify the node ID format (should start with `PVTI_` for project items)
4. If the item was archived, it will not appear in list results unless explicitly queried

---

## Permission Errors

**Symptom:** A tool invocation returns an error response with `error_type: "authentication"` after the server has started successfully:
```json
{
  "ok": false,
  "error_type": "authentication",
  "message": "Permission denied: unable to access project in the configured organization.",
  "suggestion": "Verify organization membership and token resource access settings."
}
```

**Cause:** The token has the required scopes at the GitHub level but lacks access to the specific organization, repository, or project. This commonly occurs with fine-grained tokens that are scoped to specific repositories, or when the user is not a member of the organization.

**Resolution:**
1. Verify you are a member of the target organization
2. If using a fine-grained token, check that it grants access to the target repository
3. Ensure the token's resource access includes the target project
4. For organization-owned projects, confirm the organization has not restricted third-party access
5. Contact an organization admin if access was recently revoked

---

## Network Failures

**Symptom:** A tool invocation returns an error response with `error_type: "internal"` and a timeout or connectivity message:
```json
{
  "ok": false,
  "error_type": "internal",
  "message": "Request timed out after 10 seconds. The operation status is unknown.",
  "suggestion": "Check network connectivity and GitHub status page. Retry the operation after a brief wait."
}
```

**Cause:** The request to the GitHub API failed due to network unavailability, DNS resolution failure, or a GitHub API outage. For read operations, the server retries timeout failures according to `GH_PROJECT_RETRY_ATTEMPTS` with exponential backoff from `GH_PROJECT_RETRY_DELAY_SECONDS`. Mutations are never retried to avoid duplicate side effects.

**Resolution:**
1. Check your network connectivity (DNS resolution, firewall rules)
2. Verify GitHub API status at [githubstatus.com](https://www.githubstatus.com)
3. For read operations: retry the tool call after 30 seconds
4. For mutations: check if the operation completed before retrying (e.g., check if the field was updated, or if the item was archived) to avoid duplicate side effects
5. If running inside Docker, verify the container has outbound internet access

---

## DraftIssue Close Rejection

**Symptom:** The `close_issue` tool returns a validation error:
```json
{
  "ok": false,
  "error_type": "validation",
  "message": "Cannot close item: it is a DraftIssue with no underlying GitHub issue.",
  "suggestion": "Convert the draft to a full issue first, then close it."
}
```

**Cause:** The `close_issue` tool was called on a project item that is a DraftIssue. Draft issues exist only within the project board and have no corresponding GitHub issue that can be opened or closed.

**Resolution:**
1. Convert the DraftIssue to a full issue first (via the GitHub UI or by creating an issue with the same content)
2. After conversion, the item will have an issue number and can be closed normally
3. Alternatively, archive the draft item using `archive_project_item` if it should be removed from the board

---

## Error Type Reference

| Error Type | Meaning | Retryable |
|------------|---------|-----------|
| `authentication` | Token invalid, expired, or insufficient permissions | No — fix credentials |
| `validation` | Invalid input parameters or operation not applicable | No — fix the request |
| `not_found` | Referenced resource does not exist | No — use valid IDs |
| `rate_limit` | API rate limit exceeded | Yes — after reset time |
| `internal` | Network failure, timeout, or unexpected server error | Conditional — reads retry once automatically |

### Correlating an error with the server logs

Every error envelope has a `correlation_id` (12 hex characters). The server
prints the same ID at the start of each stderr log line of that call:

```
ERROR [f805e839ca90 get_issue_detail] tools.issues.advanced_operations: CLI error in get_issue_detail: ...
```

Filter the container's stderr by that ID to see everything the failing call
logged. `request_id` is a different field: GitHub's own `X-Request-Id`, useful
when escalating to GitHub support.

---

## Cache is ignored or refreshed unexpectedly

**Cause:** The cache is expired, has a future `discovered_at`, is malformed, or belongs to a different organization/project number.

**Resolution:**
1. Run `discover_ids(force=true)` to obtain fresh metadata.
2. Check `GH_PROJECT_CACHE_PATH` and ensure its parent directory is writable by the MCP user.
3. Do not copy cache files between organizations or projects.
4. Keep the cache file private; the server writes it with mode `0600` and atomically replaces incomplete files.

## CLI output exceeds the configured limit

**Symptom:** A CLI operation returns a bounded internal error instead of processing output.

**Cause:** gh CLI returned more than `GH_PROJECT_MAX_CLI_OUTPUT_CHARS` characters. This prevents unbounded memory use and response amplification.

**Resolution:** Narrow the command/query, use a more specific issue/project operation, or increase the setting within its validated maximum. Do not disable the limit in production.

## Malformed rate-limit headers

The GraphQL client treats malformed `X-RateLimit-Remaining` and `X-RateLimit-Reset` values defensively. A malformed header is not allowed to crash the MCP; a 403 without a valid exhausted-rate signal is reported as an access/permission problem. Verify token permissions and request ID before retrying.

## Docker validation fails

Run syntax checks and tests inside a freshly built image (`make validate`, or `make test`, which builds with `INSTALL_TEST_DEPS=true` and runs `pytest`). A long-running container started from an older image does not reflect unbuilt edits, so always rebuild before validating.

## Project fields with emoji prefixes

Project V2 field titles are editable. A field titled `📊 Priority`, `📅 Due date`, or another symbol-prefixed variant is resolved consistently by discovery, creation validation, defaults, filters, reads, and writes. Use the canonical name (`Priority`, `Due date`) when possible; the prefix may be included when it is unique. If two fields normalize to the same name, the MCP rejects the request instead of choosing one.

## Bulk imports and recoverable retries

`create_project_item` is a multi-step operation: it creates an issue, adds it to the project, then applies fields. It validates discovered single-select options before creating the issue and returns the issue number, issue URL, project item ID, and failed step when a later operation fails. A retry is not automatically idempotent because GitHub issue creation has no caller-supplied idempotency key; importers must persist each successful issue number/item ID and reconcile with `search_issues` or `list_project_items` before retrying. `add_item_to_project` is idempotent for an existing issue/PR.

## Incomplete Project mutation responses

**Symptom:** a field update appears to succeed, but the returned GraphQL payload does not contain a project item ID.

**Cause:** GitHub returned an incomplete `updateProjectV2ItemFieldValue` payload. The MCP does not treat a response without `data.updateProjectV2ItemFieldValue.projectV2Item.id` as success, because doing so can make a bulk import report fields as applied when the provider response is unusable.

**Resolution:** the tool returns a validation error with the project item ID and field still available for recovery. Refresh metadata with `discover_ids`, verify the item with `list_project_items`, and retry only the affected field using `update_project_item_fields`. A response with zero field values is different: it is valid only when the mutation payload includes the returned project item ID.

## Org-level projects still need a repository

`GH_PROJECT_REPO_NAME` remains required even for an organization-owned Project V2 because issue creation, labels, milestones, assignees, and issue/PR-to-project linking use a repository. Set it to the repository that owns the work items; `GH_PROJECT_ORG_NAME` and `GH_PROJECT_PROJECT_NUMBER` identify the organization project itself.
