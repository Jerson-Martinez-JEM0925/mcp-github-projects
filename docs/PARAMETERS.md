# Parameters Reference

Complete parameter documentation for each tool in the GitHub Project Management MCP server.

---

## discover_ids

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
| `milestone` | `string` | Optional | `null` | Milestone title to assign to the issue (must exist in repository) |
| `due_date` | `string` | Optional | `null` | Due date in ISO 8601 format (YYYY-MM-DD) to set on the project item |
| `assignees` | `list[string]` | Optional | `null` | List of GitHub usernames to assign to the created issue |
| `labels` | `list[string]` | Optional | `null` | List of label names to apply to the created issue |

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

---

## Global Configuration

These values apply across all tools and are configured at the server level.

| Setting | Value | Description |
|---------|-------|-------------|
| Organization | (configured) | GitHub owner (org or user) from GH_PROJECT_ORG_NAME |
| Repository | (configured) | Repository from GH_PROJECT_REPO_NAME |
| Project number | (configured) | GitHub Project V2 number from GH_PROJECT_PROJECT_NUMBER |
| Access level | `write` (`read`/`write`/`full`) | Which tools are exposed, from **`MCP_ACCESS_LEVEL`** (MCP_-prefixed). `read` = read-only tools; `write` = + create/update/close/archive; `full` = + permanent-delete tools (each needs `confirm:true`) |
| Scope lock | `false` | From `GH_PROJECT_SCOPE_LOCK`. When `true`, every tool is confined to the configured org/repo/project; foreign targets are refused with a typed error before any mutation |
| Timeout | `10s` (1–120s) | Maximum time for a single API request before timeout error |
| Retry attempts | `1` (0–5) | Read timeouts only; mutations are never retried |
| Retry delay | `2.0s` (0–60s) | Initial delay before exponential read retry |
| Cache TTL | `24h` (1–720h) | Duration for which discovered metadata remains valid locally |
| Cache path | `.github_project_cache.json` | Configurable private metadata cache path |
| Page size | `100` (1–100) | Maximum GraphQL page size used by list operations |
| Max items | `200` (1–1,000) | Maximum number of items returned by list operations |
| CLI output limit | `1,000,000` chars | Maximum gh CLI output retained in memory |
| Estimate range | `0.25–9999` | Valid numeric range for time estimates in hours |
| Estimate granularity | `0.25` | Minimum increment for estimate values (quarter-hour) |
