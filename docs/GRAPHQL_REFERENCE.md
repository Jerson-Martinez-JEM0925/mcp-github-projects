# GraphQL Reference

All GraphQL operations used by the GitHub Project Management MCP Server.

## Queries

| Operation Name | Variables | Purpose |
|----------------|-----------|---------|
| `DiscoverProject` | `$org: String!`, `$number: Int!` | Discovers the project node ID, all field IDs, and field option IDs for the organization project. |
| `ListProjectItems` | `$org: String!`, `$number: Int!`, `$first: Int!`, `$after: String` | Fetches project items with pagination, including content, field values, assignees, and labels. |
| `GetItemStatus` | `$itemId: ID!` | Retrieves a project item's current status, archive state, and content type by node ID. |
| `SingleSelectFieldOptions` | `$fieldId: ID!` | Current options (id, name, color, description) of one single-select field — read before `set_field_options` builds its plan. |
| `ProjectViews` | `$projectId: ID!` | The project's views (id, number, name, layout, filter) for `list_project_views`. |

## Mutations

| Operation Name | Variables | Purpose |
|----------------|-----------|---------|
| `UpdateField` | `$projectId: ID!`, `$itemId: ID!`, `$fieldId: ID!`, `$value: ProjectV2FieldValue!` | Updates a single field value on a project item (Status, Priority, Due date, Estimate, etc.). |
| `ArchiveItem` | `$projectId: ID!`, `$itemId: ID!` | Archives a project item, removing it from the active project board. |
| `CreateField` | `$projectId: ID!`, `$name: String!`, `$dataType: ProjectV2CustomFieldType!` | Creates a new custom field on the project (used for auto-creating the Estimate field). |
| `AddItemToProject` | `$projectId: ID!`, `$contentId: ID!` | Adds an existing issue to the project board by its content node ID. |
| `UpdateSingleSelectOptions` | `$fieldId: ID!`, `$options: [ProjectV2SingleSelectFieldOptionInput!]` | `updateProjectV2Field` — replaces a single-select field's options; options sent with their `id` keep identity and item values (`set_field_options`). |
| `CreateProjectView` | `$projectId: ID!`, `$name: String!`, `$layout: ProjectV2ViewLayout!` | `createProjectV2View` — adds a BOARD/TABLE/ROADMAP view. |
| `UpdateProjectViewFilter` | `$viewId: ID!`, `$filter: String!` | `updateProjectV2View` — sets the new view's filter. |

These tables cover the operations described in this guide. Every query and
mutation the server sends is defined in `graphql/queries.py` and
`graphql/mutations.py` (tools never embed GraphQL inline).

## Query Details

### DiscoverProject

```graphql
query DiscoverProject($org: String!, $number: Int!) {
  organization(login: $org) {
    projectV2(number: $number) {
      id
      fields(first: 50) {
        nodes {
          ... on ProjectV2SingleSelectField {
            id
            name
            dataType
            options { id name }
          }
          ... on ProjectV2Field {
            id
            name
            dataType
          }
          ... on ProjectV2IterationField {
            id
            name
            dataType
          }
        }
      }
    }
  }
}
```

**Returns:** Project node ID and all fields with their IDs, data types, and options (for single-select fields).

### ListProjectItems

```graphql
query ListProjectItems($org: String!, $number: Int!, $first: Int!, $after: String) {
  organization(login: $org) {
    projectV2(number: $number) {
      items(first: $first, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          content {
            ... on Issue { number title body url assignees(first: 20) { nodes { login } } labels(first: 20) { nodes { name } } }
            ... on DraftIssue { title body }
            __typename
          }
          fieldValues(first: 20) {
            nodes {
              ... on ProjectV2ItemFieldSingleSelectValue { name field { ... on ProjectV2SingleSelectField { name } } }
              ... on ProjectV2ItemFieldDateValue { date field { ... on ProjectV2Field { name } } }
              ... on ProjectV2ItemFieldNumberValue { number field { ... on ProjectV2Field { name } } }
              ... on ProjectV2ItemFieldTextValue { text field { ... on ProjectV2Field { name } } }
              ... on ProjectV2ItemFieldMilestoneValue { milestone { title } field { ... on ProjectV2Field { name } } }
              ... on ProjectV2ItemFieldIterationValue { title field { ... on ProjectV2IterationField { name } } }
            }
          }
        }
      }
    }
  }
}
```

**Returns:** Paginated list of project items with their content (Issue or DraftIssue), all field values, assignees, and labels.

### GetItemStatus

```graphql
query GetItemStatus($itemId: ID!) {
  node(id: $itemId) {
    ... on ProjectV2Item {
      id
      isArchived
      content {
        ... on Issue { number state }
        ... on DraftIssue { title }
        __typename
      }
      fieldValues(first: 20) {
        nodes {
          ... on ProjectV2ItemFieldSingleSelectValue { name field { ... on ProjectV2SingleSelectField { name } } }
        }
      }
    }
  }
}
```

**Returns:** Item's archive state, content type (Issue with state or DraftIssue), and single-select field values (including Status).

## Mutation Details

### UpdateField

```graphql
mutation UpdateField($projectId: ID!, $itemId: ID!, $fieldId: ID!, $value: ProjectV2FieldValue!) {
  updateProjectV2ItemFieldValue(input: {
    projectId: $projectId
    itemId: $itemId
    fieldId: $fieldId
    value: $value
  }) {
    projectV2Item { id }
  }
}
```

**Returns:** The updated project item's node ID.

### ArchiveItem

```graphql
mutation ArchiveItem($projectId: ID!, $itemId: ID!) {
  archiveProjectV2Item(input: {
    projectId: $projectId
    itemId: $itemId
  }) {
    item { id }
  }
}
```

**Returns:** The archived item's node ID.

### CreateField

```graphql
mutation CreateField($projectId: ID!, $name: String!, $dataType: ProjectV2CustomFieldType!) {
  createProjectV2Field(input: {
    projectId: $projectId
    name: $name
    dataType: $dataType
  }) {
    projectV2Field { id name dataType }
  }
}
```

**Returns:** The newly created field's node ID, name, and data type.

### AddItemToProject

```graphql
mutation AddItemToProject($projectId: ID!, $contentId: ID!) {
  addProjectV2ItemById(input: {
    projectId: $projectId
    contentId: $contentId
  }) {
    item { id }
  }
}
```

**Returns:** The new project item's node ID.

## Variable Types Reference

| Type | Description | Example |
|------|-------------|---------|
| `String!` | Non-nullable string | `"my-org"` |
| `Int!` | Non-nullable integer | `1` |
| `ID!` | Non-nullable GitHub node ID | `"PVT_kwDOABC123"` |
| `ProjectV2FieldValue!` | Union input for field values | `{ singleSelectOptionId: "..." }` |
| `ProjectV2CustomFieldType!` | Enum for field types | `NUMBER`, `TEXT`, `DATE`, `SINGLE_SELECT` |

## Retry Behavior

| Operation Type | Retry on Timeout | Delay | Timeout |
|----------------|-----------------|-------|---------|
| Queries (read) | Yes, `GH_PROJECT_RETRY_ATTEMPTS` times (default 1, max 5) | Exponential from `GH_PROJECT_RETRY_DELAY_SECONDS` (default 2.0s) | `GH_PROJECT_TIMEOUT_SECONDS` (default 10s, max 120s) |
| Mutations (write) | Never | — | `GH_PROJECT_TIMEOUT_SECONDS` (default 10s, max 120s) |

Retries apply only to timeout failures. Transient HTTP responses are classified as unavailable; mutations are not retried because their remote outcome may be unknown.
