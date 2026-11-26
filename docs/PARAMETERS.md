# Parameters Reference

Complete parameter documentation for each tool in the GitHub Project Management MCP server.

---

## Project-board target selection

Project-board tools accept the optional `project_number` selector. When it is
omitted, the configured `GH_PROJECT_PROJECT_NUMBER` is used. An explicit
selector must be included in `GH_PROJECT_PROJECT_NUMBERS` when that allowlist
is configured.

| Variable | Type | Required | Description |
|----------|------|----------|-------------|
| `GH_PROJECT_PROJECT_NUMBER` | `int` | Yes | Default Project V2 board. |
| `GH_PROJECT_PROJECT_NUMBERS` | `csv[int]` | No | Additional Project V2 boards allowed by board tools. |
| `project_number` | `int` | No | Per-call board selector; never changes the default. |

See [Multiple Project V2 boards](MULTI_PROJECTS.md) for cache isolation,
scope-lock behavior, Docker configuration, and migration guidance.

---

Discovers project ID, field IDs, and option IDs from the GitHub Project V2 API.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `force` | `bool` | Optional | `false` | Bypass the 24-hour cache and force a fresh API discovery query |

---

## list_project_items

Lists project board items with optional field-based filtering. Returns up to 200 items.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `status` | `string` | Optional | `null` | Filter by Status column value (exact match against project board status) |
| `priority` | `string` | Optional | `null` | Filter by Priority field value (exact match against project priority) |
| `labels` | `list[string]` | Optional | `null` | Filter by label names; items matching any label in the list are included |
| `assignee` | `string` | Optional | `null` | Filter by assignee GitHub username (exact match, case-insensitive) |
| `milestone` | `string` | Optional | `null` | Filter by milestone title (exact match against milestone name) |
| `due_date` | `string` | Optional | `null` | ISO 8601 date string in YYYY-MM-DD format for date comparison filtering |
| `due_date_op` | `string` | Optional | `null` | Comparison operator for due_date: "before", "after", or "exact" |
| `include_body` | `boolean` | Optional | `false` | Include full issue/PR bodies in the response. Keeping it false avoids large GraphQL payloads and reduces LLM context use |

### Valid Status Values

Status and Priority options are read from your board at runtime (`discover_ids`), never hardcoded; the tables below are an example board. A leading emoji is optional when naming an option (`Done` matches `✅ Done`) as long as the match is unique.

| Value | Description |
|-------|-------------|
| `Backlog` | Items not yet scheduled for active development work |
| `Todo` | Items scheduled and ready to be picked up for work |
| `In Progress` | Items currently being actively worked on by assignees |
| `In Review` | Items completed and awaiting code review or approval |
| `Done` | Items that have been completed and verified successfully |
| `Trash` | Items discarded or no longer relevant to the project |

### Valid Priority Values

| Value | Description |
|-------|-------------|
| `Urgent` | Critical items requiring immediate attention and resolution |
| `High` | Important items that should be addressed in the current sprint |
| `Medium` | Standard priority items for normal development scheduling |
| `Low` | Items that can be deferred to future development cycles |

### Due Date Operators

| Operator | Description |
|----------|-------------|
| `before` | Returns items with due date strictly before the specified date |
| `after` | Returns items with due date strictly after the specified date |
| `exact` | Returns items with due date exactly matching the specified date |

---

## create_project_item

Creates a new GitHub issue in the repository and adds it to the project board with optional field values.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `title` | `string` | Required | — | Issue title between 1 and 256 characters inclusive |
| `body` | `string` | Optional | `""` | Issue body content, supports Markdown, maximum 65536 characters |
| `status` | `string` | Optional | `null` | Status field value to set on the project item after creation |
| `priority` | `string` | Optional | `null` | Priority field value to set on the project item after creation |
| `area` | `string` | Optional | `null` | Area single-select option name |
| `work_type` | `string` | Optional | `null` | Work Type single-select option name |
| `estimate` | `float` | Optional | `null` | Estimate in hours |
| `milestone` | `string` | Optional | `null` | Milestone title to assign to the issue (must exist in repository) |
| `due_date` | `string` | Optional | `null` | Due date in ISO 8601 format (YYYY-MM-DD) to set on the project item |
| `assignees` | `list[string]` | Optional | `null` | List of GitHub usernames to assign to the created issue |
| `labels` | `list[string]` | Optional | `null` | List of label names to apply to the created issue |
| `apply_defaults` | `bool` | Optional | `true` | Fill omitted Project V2 fields using configured defaults |

Issue creation applies configured defaults by default. Supply explicit values
for every field that should differ from the board defaults; see
[SETUP](SETUP.md#board-field-defaults).

### Title Constraints

| Constraint | Value | Description |
|------------|-------|-------------|
| Minimum length | 1 character | Title cannot be empty; at least one character required |
| Maximum length | 256 characters | Exceeding this limit returns a validation error response |

### Body Constraints

| Constraint | Value | Description |
|------------|-------|-------------|
| Minimum length | 0 characters | Body is optional and can be left empty |
| Maximum length | 65536 characters | Maximum content length for issue body text |

---

## update_project_item_fields

Updates one or more fields on an existing project item. Each field is updated independently and reported individually.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `item_id` | `string` | Required | — | Project item node ID (e.g., "PVTI_...") identifying the item to update |
| `fields` | `dict[string, any]` | Required | — | Dictionary of field name to value pairs for the fields to update |

### Supported Field Names and Value Types

| Field Name | Value Type | Description |
|------------|-----------|-------------|
| `Status` | `string` | Single-select status value (must match a valid project status option) |
| `Priority` | `string` | Single-select priority value (must match a valid project priority option) |
| `Milestone` | `string` | Milestone title string (must exist as a milestone in the repository) |
| `Due date` | `string` | ISO 8601 date string in YYYY-MM-DD format for the item due date |
| `body` | `string` | Issue body content as a Markdown string (updates via Issues API) |
| `assignees` | `list[string]` | List of GitHub usernames; replaces all current assignees completely |
| `labels` | `list[string]` | List of label names; replaces all current labels on the issue |

### Update Behavior

- Each field is updated independently; failure of one does not block others
- Response reports per-field outcomes (success with value, or failure with reason)
- Assignee updates replace the entire list — provide all desired assignees each time
- Label updates replace the entire list — provide all desired labels each time

---

## set_estimate

Sets the time estimate (in hours) on a project item. Creates the Estimate field if it does not exist.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `item_id` | `string` | Required | — | Project item node ID identifying the item to set the estimate on |
| `value` | `float` | Required | — | Estimate in hours; must be between 0.25 and 9999 in 0.25 increments |

### Estimate Constraints

| Constraint | Value | Description |
|------------|-------|-------------|
| Minimum | 0.25 | Smallest allowable estimate representing 15 minutes of work |
| Maximum | 9999 | Largest allowable estimate in hours for a single item |
| Granularity | 0.25 | Values must be multiples of 0.25 (quarter-hour increments) |

### Valid Estimate Examples

| Value | Meaning |
|-------|---------|
| `0.25` | Fifteen minutes of estimated work effort |
| `1.0` | One hour of estimated work effort |
| `4.5` | Four and a half hours of estimated work effort |
| `8.0` | One full working day of estimated effort |
| `40.0` | One full working week of estimated effort |

---

## archive_project_item

Archives an item from the project board. The underlying issue remains open and unmodified.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `item_id` | `string` | Required | — | Project item node ID identifying the item to archive from the board |

### Behavior Notes

- Archiving is idempotent: archiving an already-archived item returns success
- The underlying GitHub issue is not affected (remains open unless separately closed)
- Archived items can be restored from the project board UI but not via this tool

---

## move_to_done

Moves an item's Status field to "Done". This is a convenience wrapper around update_project_item_fields.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `item_id` | `string` | Required | — | Project item node ID identifying the item to move to Done status |

### Behavior Notes

- Idempotent: moving an item already in Done status returns success with no-change message
- Does not close the underlying issue; use `close_issue` separately if needed

---

## move_to_trash

Moves an item's Status field to "Trash". This is a convenience wrapper around update_project_item_fields.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `item_id` | `string` | Required | — | Project item node ID identifying the item to move to Trash status |

### Behavior Notes

- Idempotent: moving an item already in Trash status returns success with no-change message
- Does not archive or delete the item; it remains visible on the board in the Trash column

---

## close_issue

Closes the underlying GitHub issue by issue number. Does not modify the project board item.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `issue_number` | `int` | Required | — | GitHub issue number (positive integer) within the configured target repository |

### Behavior Notes

- Cannot close DraftIssues — returns a validation error with suggestion to archive instead
- Closing an already-closed issue is idempotent and returns success
- The project board item is not archived automatically; use `archive_project_item` if needed

---

## update_milestone

Updates selected fields on an existing milestone through
`PATCH /repos/{owner}/{repo}/milestones/{milestone_number}`. At least one
update field is required; owner and repo are optional target overrides.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `milestone_number` | `int` | Required | — | Positive GitHub milestone number |
| `title` | `string` | Optional | `null` | Replacement milestone title |
| `description` | `string` | Optional | `null` | Replacement description; an empty string clears it |
| `due_on` | `string` or `null` | Optional | `null` | `YYYY-MM-DD`, `null`, or empty string; null/empty removes the due date |
| `state` | `string` | Optional | `null` | New state, either `open` or `closed` |
| `owner` | `string` | Optional | configured owner | GitHub owner override; rejected outside the locked scope |
| `repo` | `string` | Optional | configured repository | Repository override; rejected outside the locked scope |

Only fields supplied by the caller are sent. The date form `YYYY-MM-DD` is
normalized to `YYYY-MM-DDT00:00:00Z` for GitHub. The operation is idempotent.

---

## server_info

Diagnostics tool. Returns safe, credential-free server metadata: the effective
access level, scope-lock state, tool-exposure counts, and the configured target.
Takes no parameters.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| — | — | — | — | No parameters. |

### Response fields

| Field | Description |
|-------|-------------|
| `access_level` | Effective `MCP_ACCESS_LEVEL` (`read` / `write` / `full`) |
| `access_level_meaning` | One-line gloss of what the level exposes |
| `tool_exposure` | `classified_total`, `exposed`, `hidden`, `hidden_delete_tools` |
| `exposed_tools` | Names of every tool registered at this level |
| `scope_lock` | `enabled`, `variable`, `confined_to`, `meaning` |
| `target` | Configured org / repo / project (no credentials) |

---

## Permanent-delete tools

The following five tools perform **irreversible** deletions on GitHub. They are
registered **only when `MCP_ACCESS_LEVEL=full`**, and every one of them refuses
to act unless called with `confirm: true`. Without confirmation they return a
`validation` error that states the operation is permanent and points to the
reversible alternative.

### delete_project_item

Permanently deletes a card from the Project V2 board (`deleteProjectV2Item`).

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `item_id` | `string` | Required | — | Project item node ID to delete (e.g. `PVTI_...`) |
| `confirm` | `bool` | Optional | `false` | Must be `true` to proceed; the deletion is permanent |

Reversible alternative: `archive_project_item` (archives; restorable in the UI)
or `move_to_trash`.

### delete_issue

Permanently deletes a GitHub issue (`deleteIssue` GraphQL mutation; there is no
REST equivalent).

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `issue_number` | `int` | Required | — | Issue number to delete (positive integer) |
| `confirm` | `bool` | Optional | `false` | Must be `true` to proceed; the deletion is permanent |

Reversible alternative: `close_issue` (a closed issue can be reopened).

### delete_issue_comment

Permanently deletes an issue comment (REST `DELETE /issues/comments/{id}`).

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `comment_id` | `int` | Required | — | Numeric REST comment ID (not the issue number) |
| `confirm` | `bool` | Optional | `false` | Must be `true` to proceed; the deletion is permanent |

Alternative: edit the comment via `edit_issue_comment` instead of deleting it.

### delete_label

Permanently deletes a repository label (`gh label delete --yes`). The label is
removed from every issue/PR that carried it.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `name` | `string` | Required | — | Exact label name to delete |
| `confirm` | `bool` | Optional | `false` | Must be `true` to proceed; the deletion is permanent |

### delete_milestone

Permanently deletes a milestone (REST `DELETE /milestones/{number}`), resolved
by exact title. Issues linked to it are unlinked but not deleted.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `title` | `string` | Required | — | Exact milestone title to delete |
| `confirm` | `bool` | Optional | `false` | Must be `true` to proceed; the deletion is permanent |

Reversible alternative: `close_milestone` (keeps the milestone and its history).

### list_issue_dependencies

Lists real GitHub issue dependencies using the REST API. `direction` may be
`blocked_by`, `blocking`, or `both` (the default). `owner` and `repo` are
optional overrides and default to `GH_PROJECT_ORG_NAME` and
`GH_PROJECT_REPO_NAME`; they are checked by the scope lock.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `issue_number` | `int` | Required | — | Issue whose dependencies are listed |
| `direction` | `string` | Optional | `both` | Relationship direction: `blocked_by`, `blocking`, or `both` |
| `owner` | `string` | Optional | configured owner | Repository owner override |
| `repo` | `string` | Optional | configured repository | Repository name override |

### add_issue_dependency

Adds a real dependency where `issue_number` is blocked by
`blocked_by_issue_number`. The tool resolves the blocking issue's REST `id`
(the API does not accept its issue number) and is idempotent.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `issue_number` | `int` | Required | — | Issue that is blocked |
| `blocked_by_issue_number` | `int` | Required | — | Issue that blocks it |
| `owner` | `string` | Optional | configured owner | Repository owner override |
| `repo` | `string` | Optional | configured repository | Repository name override |

### remove_issue_dependency

Removes the dependency where `issue_number` is blocked by
`blocked_by_issue_number`. Removing an absent relationship succeeds without a
change.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `issue_number` | `int` | Required | — | Issue that is blocked |
| `blocked_by_issue_number` | `int` | Required | — | Issue that blocks it |
| `owner` | `string` | Optional | configured owner | Repository owner override |
| `repo` | `string` | Optional | configured repository | Repository name override |

---

## create_pull_request

Creates a pull request in the configured repository. The tool can also set
repository metadata, request reviewers, and add/configure the PR on the
scope-locked Project V2 board. Metadata mutations are reported individually;
`metadata_status` is `partial` if any requested follow-up failed.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `title` | `string` | Required | — | Pull request title |
| `head` | `string` | Required | — | Head branch |
| `base` | `string` | Optional | `main` | Base branch |
| `body` | `string` | Optional | `null` | Pull request description |
| `draft` | `bool` | Optional | `false` | Create a draft pull request |
| `link_to_issue` | `int` | Optional | `null` | Issue to link to the pull request |
| `reviewers` | `list[string]` | Optional | `null` | GitHub usernames to request for review; cannot include the PR author |
| `team_reviewers` | `list[string]` | Optional | `null` | GitHub team slugs to request for review |
| `labels` | `list[string]` | Optional | `null` | Labels to set on the PR; supplying a list replaces the current list |
| `assignees` | `list[string]` | Optional | `null` | Assignees to set; supplying a list replaces the current list |
| `milestone` | `string` | Optional | `null` | Existing repository milestone title |
| `add_to_project` | `bool` | Optional | `false` | Add the PR to the configured Project V2 board |
| `status`, `priority`, `area`, `work_type` | `string` | Optional | `null` | Project single-select field values; option names are resolved from the board |
| `estimate` | `float` | Optional | `null` | Project estimate in hours, within configured range and granularity |
| `due_date` | `string` | Optional | `null` | Project due date in `YYYY-MM-DD` format |
| `apply_defaults` | `bool` | Optional | `false` | Fill omitted Project V2 fields using configured defaults |

Supplying a Project V2 field or `apply_defaults: true` automatically adds the
PR to the configured board if it is not already present. `Reviewers` and
`Milestone` Project fields are populated by GitHub from requested reviewers and
the repository milestone, respectively; they are not written as generic
custom fields.

## update_pull_request

Updates an existing pull request. `pull_number` is required; at least one PR
field or metadata operation must be provided.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `pull_number` | `int` | Required | — | Pull request number |
| `title` | `string` | Optional | `null` | New title |
| `body` | `string` | Optional | `null` | New body |
| `base` | `string` | Optional | `null` | New base branch |
| `state` | `string` | Optional | `null` | `open` or `closed` |
| `maintainer_can_modify` | `bool` | Optional | `null` | Allow maintainers to update the head branch |
| `reviewers`, `team_reviewers` | `list[string]` | Optional | `null` | Request GitHub user/team reviewers; the PR author cannot review their own PR |
| `labels`, `assignees` | `list[string]` | Optional | `null` | Replace the PR's label/assignee list |
| `milestone` | `string` | Optional | `null` | Existing repository milestone title |
| `status`, `priority`, `area`, `work_type` | `string` | Optional | `null` | Project single-select field values |
| `estimate` | `float` | Optional | `null` | Project estimate in hours |
| `due_date` | `string` | Optional | `null` | Project due date in `YYYY-MM-DD` format |
| `apply_defaults` | `bool` | Optional | `false` | Fill omitted Project V2 fields using configured defaults |
| `add_to_project` | `bool` | Optional | `false` | Add the PR to the configured Project V2 board |

Metadata-only updates do not send an empty PR patch. The response includes
per-field `metadata_results` and `metadata_status`; a partial result means the
PR mutation succeeded but one or more requested metadata operations did not.

See [Multiple repositories and project scopes](MULTI_SCOPE_CONFIGURATION.md)
for client-specific configuration.

## list_pull_request_reviews

Lists the reviews submitted on a pull request: reviewer, verdict
(`APPROVED`, `CHANGES_REQUESTED`, `COMMENTED`, `DISMISSED`, `PENDING`),
summary body and, optionally, the inline file/line comments attached to
each review. Also returns `latest_state_by_reviewer` — the newest verdict
per reviewer — to answer "is this PR approved?" in one call.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `number` | `int` | Required | — | Pull request number |
| `include_comments` | `bool` | Optional | `true` | Include inline review comments grouped per review |
| `per_page` | `int` | Optional | `50` | Reviews per page (max 100; `has_more` flags truncation) |
| `owner` | `string` | Optional | configured owner | GitHub owner override; rejected outside the locked scope |
| `repo` | `string` | Optional | configured repository | Repository override; rejected outside the locked scope |

## review_pull_request

Submits a pull request review as the authenticated user — in request-auth
mode that is the calling user, so the review is authored under their GitHub
identity and bounded by their permissions and the repository's review rules
(for example, GitHub rejects approving your own PR).

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `number` | `int` | Required | — | Pull request number |
| `event` | `string` | Required | — | `approve`, `request_changes`, or `comment` (feedback without verdict) |
| `body` | `string` | Optional | `""` | Review summary body; required for `comment` and `request_changes` |
| `owner` | `string` | Optional | configured owner | GitHub owner override; rejected outside the locked scope |
| `repo` | `string` | Optional | configured repository | Repository override; rejected outside the locked scope |

`body` is validated: omitting it (or passing whitespace) with
`event=comment` or `event=request_changes` returns a validation error
before any GitHub call.

## Global Configuration

These values apply across all tools and are configured at the server level.

| Setting | Value | Description |
|---------|-------|-------------|
| Organization | (configured) | GitHub owner (org or user) from GH_PROJECT_ORG_NAME |
| Repository | (configured) | Repository from GH_PROJECT_REPO_NAME |
| Project number | (configured) | GitHub Project V2 number from GH_PROJECT_PROJECT_NUMBER |
| Access level | `write` (`read`/`write`/`full`) | Which tools are exposed, from **`MCP_ACCESS_LEVEL`** (MCP_-prefixed). `read` = read-only tools; `write` = + create/update/close/archive; `full` = + permanent-delete tools (each needs `confirm:true`) |
| Tool profile | `full` (`full`/`developer`/`devops`/`project_manager`/`read_only`/`commenter`) | From **`MCP_TOOL_PROFILE`**. Narrows the visible catalog by role/capability. It can only reduce what `MCP_ACCESS_LEVEL` permits; it never grants additional writes or deletes |
| Scope lock | `false` | From `GH_PROJECT_SCOPE_LOCK`. When `true`, every tool is confined to the configured org/repo/project; foreign targets are refused with a typed error before any mutation |
| Startup auth validation | `true` | From `GH_PROJECT_VALIDATE_AUTH_ON_STARTUP`; validates env credentials before accepting stdio MCP traffic. Disable only for controlled fast-start deployments |
| Timeout | `30s` (1–120s) | Maximum time for a single API request before timeout error |
| Retry attempts | `1` (0–5) | Read timeouts only; mutations are never retried |
| Retry delay | `2.0s` (0–60s) | Initial delay before exponential read retry |
| Cache TTL | `24h` (1–720h) | Duration for which discovered metadata remains valid locally |
| Cache path | `.github_project_cache.json` | Configurable private metadata cache path |
| Page size | `100` (1–100) | Maximum GraphQL page size used by list operations |
| Max items | `200` (1–1,000) | Maximum number of items returned by list operations |
| CLI output limit | `1,000,000` chars | Maximum gh CLI output retained in memory |
| Concurrency limit | `5` (1–20) | From `GH_PROJECT_MAX_CONCURRENT_REQUESTS`; bounds independent GitHub operations inside bulk/update tools |
| Resource metadata cache | `300s` (0–86,400) | From `GH_PROJECT_RESOURCE_CACHE_TTL_SECONDS`; short-lived in-process cache for stable labels, milestones and workflow definitions. Mutations invalidate the affected resource kind; zero disables it |
| HTTP body limit | `1,000,000` bytes | From `MCP_HTTP_MAX_BODY_BYTES`; rejects oversized Streamable HTTP MCP requests |
| HTTP rate limit | `240/min` | From `MCP_HTTP_RATE_LIMIT_PER_MINUTE`; per-client budget keyed by token fingerprint or remote address |
| HTTP token validation | `true` | From `MCP_HTTP_VALIDATE_TOKENS`; validates bearers before exposing MCP metadata, caching verdicts by fingerprint |
| HTTP auth cache | `60s` | From `MCP_HTTP_AUTH_CACHE_TTL_SECONDS`; positive validation TTL, with shorter negative caching |
| HTTP TLS enforcement | `false` | From `MCP_HTTP_REQUIRE_TLS`; when true, non-loopback plaintext HTTP is rejected unless the request scheme or first `X-Forwarded-Proto` hop is HTTPS |
| Circuit breaker | `5` failures / `30s` reset | `GH_PROJECT_CIRCUIT_BREAKER_FAILURES` and `GH_PROJECT_CIRCUIT_BREAKER_RESET_SECONDS`; bounds repeated transient GitHub API failures per credential fingerprint |
| Log level/format | `WARNING`/`text` | `MCP_LOG_LEVEL` and `MCP_LOG_FORMAT`; `DEBUG` enables per-tool latency logs, `json` emits one structured object per line |
| OpenTelemetry tracing | disabled | `MCP_OTEL_ENABLED`, `MCP_OTEL_SERVICE_NAME`, `MCP_OTEL_EXPORTER_OTLP_ENDPOINT`; spans contain only tool/access/correlation metadata, never arguments or secrets |
| Estimate range | `0.25–9999` | Valid numeric range for time estimates in hours |
| Estimate granularity | `0.25` | Minimum increment for estimate values (quarter-hour) |

## Program-scale tools

The following tools accept `repository` as an optional `owner/name` target;
`repository` is mutually exclusive with the legacy `owner` and `repo` pair.

| Tool | Required parameters | Notable optional parameters |
|------|---------------------|-----------------------------|
| `get_pull_request_merge_readiness` | `pull_number` | repository target |
| `compare_branches` | `base`, `head` | repository target |
| `create_promotion_pull_request` | `base`, `head` | `title`, `body`, `draft`, `back_merge` |
| `list_repository_rulesets` | — | `diff_ruleset` JSON snapshot |
| `get_branch_rules` | `branch` | repository target |
| `project_progress_report` | `numeric_field` | `group_by`, `repository_filter`, `label`, `epic_issue_number` |
| `upsert_issue_body_section` | `number`, `section`, `content` | `kind` (`issue` or `pull_request`) |
| `get_pull_request_bot_comments` | `pull_number` | `author`, `pattern`, `extract_terraform_plan` |
| `bulk_link_pull_requests` | `links` | `keyword` |
| `epic_rollup` | `epic_issue_number` | `numeric_field` |
| `project_items_consistency_check` | `expected` | repository target |
| `find_stale_branches` | — | `stale_days`, `include_protected` |
| `list_open_prs_by_base` | — | `base` |

`link_pull_request` supports the compatibility-preserving default keyword
`closes` plus `fixes`, `resolves`, `refs`, and `part_of`. Existing reference
lines for the same issue are replaced instead of duplicated.
