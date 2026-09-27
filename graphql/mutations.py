"""GraphQL mutation strings for GitHub Projects V2 API.

Contains write operations for updating fields, archiving items,
creating custom fields, and adding items to projects. These mutations
are used by the ProjectService and FieldService to modify project
state via the GitHub GraphQL API.
"""

UPDATE_FIELD_MUTATION: str = """
mutation UpdateField($projectId: ID!, $itemId: ID!, $fieldId: ID!, $value: ProjectV2FieldValue!) {
  updateProjectV2ItemFieldValue(input: {
    projectId: $projectId
    itemId: $itemId
    fieldId: $fieldId
    value: $value
  }) {
    projectV2Item {
      id
    }
  }
}
""".strip()

ARCHIVE_ITEM_MUTATION: str = """
mutation ArchiveItem($projectId: ID!, $itemId: ID!) {
  archiveProjectV2Item(input: {
    projectId: $projectId
    itemId: $itemId
  }) {
    item {
      id
    }
  }
}
""".strip()

CREATE_FIELD_MUTATION: str = """
mutation CreateField($projectId: ID!, $name: String!, $dataType: ProjectV2CustomFieldType!) {
  createProjectV2Field(input: {
    projectId: $projectId
    name: $name
    dataType: $dataType
  }) {
    projectV2Field {
      ... on ProjectV2Field {
        id
        name
        dataType
      }
      ... on ProjectV2SingleSelectField {
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
""".strip()

ADD_ITEM_TO_PROJECT_MUTATION: str = """
mutation AddItemToProject($projectId: ID!, $contentId: ID!) {
  addProjectV2ItemById(input: {
    projectId: $projectId
    contentId: $contentId
  }) {
    item {
      id
    }
  }
}
""".strip()

# ── Provisioning mutations (project / field / option creation) ───────────────
# These power the project-provisioning tools: creating a Project V2 board,
# adding custom single-select and typed fields, and appending select options.

CREATE_PROJECT_MUTATION: str = """
mutation CreateProject($ownerId: ID!, $title: String!) {
  createProjectV2(input: {
    ownerId: $ownerId
    title: $title
  }) {
    projectV2 {
      id
      number
      title
      url
      public
    }
  }
}
""".strip()

# Update a project's metadata (short description, README, visibility, closed).
UPDATE_PROJECT_MUTATION: str = """
mutation UpdateProject($projectId: ID!, $public: Boolean, $shortDescription: String, $readme: String, $closed: Boolean) {
  updateProjectV2(input: {
    projectId: $projectId
    public: $public
    shortDescription: $shortDescription
    readme: $readme
    closed: $closed
  }) {
    projectV2 {
      id
      number
      title
      url
      public
      shortDescription
    }
  }
}
""".strip()

# Create a single-select field with its initial options in one call.
# GitHub requires at least one option when dataType is SINGLE_SELECT.
CREATE_SINGLE_SELECT_FIELD_MUTATION: str = """
mutation CreateSingleSelectField($projectId: ID!, $name: String!, $options: [ProjectV2SingleSelectFieldOptionInput!]!) {
  createProjectV2Field(input: {
    projectId: $projectId
    dataType: SINGLE_SELECT
    name: $name
    singleSelectOptions: $options
  }) {
    projectV2Field {
      ... on ProjectV2SingleSelectField {
        id
        name
        dataType
        options {
          id
          name
        }
      }
    }
  }
}
""".strip()

# Create a typed (TEXT / NUMBER / DATE) field.
CREATE_TYPED_FIELD_MUTATION: str = """
mutation CreateTypedField($projectId: ID!, $name: String!, $dataType: ProjectV2CustomFieldType!) {
  createProjectV2Field(input: {
    projectId: $projectId
    dataType: $dataType
    name: $name
  }) {
    projectV2Field {
      ... on ProjectV2Field {
        id
        name
        dataType
      }
    }
  }
}
""".strip()

# Link an existing repository to a project (so repo issues can be added).
LINK_REPOSITORY_MUTATION: str = """
mutation LinkRepo($projectId: ID!, $repositoryId: ID!) {
  linkProjectV2ToRepository(input: {
    projectId: $projectId
    repositoryId: $repositoryId
  }) {
    repository {
      id
      nameWithOwner
    }
  }
}
""".strip()

# ── Permanent-delete mutations (issue #34) ───────────────────────────────────
# These are IRREVERSIBLE on GitHub's side. They are exposed only at
# MCP_ACCESS_LEVEL=full and each wrapping tool requires confirm=true.

# deleteProjectV2Item removes a card from a Project V2 board permanently.
# Input: {projectId: ID!, itemId: ID!}; returns the deleted item's id.
# https://docs.github.com/en/graphql/reference/mutations#deleteprojectv2item
DELETE_PROJECT_ITEM_MUTATION: str = """
mutation DeleteProjectItem($projectId: ID!, $itemId: ID!) {
  deleteProjectV2Item(input: {
    projectId: $projectId
    itemId: $itemId
  }) {
    deletedItemId
  }
}
""".strip()

# deleteIssue permanently deletes an issue (GraphQL-only; there is no REST
# endpoint for issue deletion). Input: {issueId: ID!}; returns the owning
# repository so the caller can confirm the target.
# https://docs.github.com/en/graphql/reference/mutations#deleteissue
DELETE_ISSUE_MUTATION: str = """
mutation DeleteIssue($issueId: ID!) {
  deleteIssue(input: {
    issueId: $issueId
  }) {
    repository {
      nameWithOwner
    }
  }
}
""".strip()

# ── Board structure (issue #26) ──────────────────────────────────────────────

# updateProjectV2Field replaces a single-select field's option list. Options
# sent WITH their existing `id` keep their identity, so items keep their value;
# an existing option left out of the list is removed and items holding it lose
# the value. Verified against the public schema on 2026-09-27.
# https://docs.github.com/en/graphql/reference/mutations#updateprojectv2field
UPDATE_SINGLE_SELECT_OPTIONS_MUTATION: str = """
mutation UpdateSingleSelectOptions($fieldId: ID!, $options: [ProjectV2SingleSelectFieldOptionInput!]) {
  updateProjectV2Field(input: {
    fieldId: $fieldId
    singleSelectOptions: $options
  }) {
    projectV2Field {
      ... on ProjectV2SingleSelectField {
        id
        name
        options { id name color description }
      }
    }
  }
}
""".strip()

# createProjectV2View adds a view (table, board or roadmap) to a project.
# https://docs.github.com/en/graphql/reference/mutations#createprojectv2view
CREATE_PROJECT_VIEW_MUTATION: str = """
mutation CreateProjectView($projectId: ID!, $name: String!, $layout: ProjectV2ViewLayout!) {
  createProjectV2View(input: {
    projectId: $projectId
    name: $name
    layout: $layout
  }) {
    projectV2View { id number name layout filter }
  }
}
""".strip()

# updateProjectV2View sets a view's filter (createProjectV2View takes none).
# https://docs.github.com/en/graphql/reference/mutations#updateprojectv2view
UPDATE_PROJECT_VIEW_FILTER_MUTATION: str = """
mutation UpdateProjectViewFilter($viewId: ID!, $filter: String!) {
  updateProjectV2View(input: {
    viewId: $viewId
    filter: $filter
  }) {
    projectV2View { id number name layout filter }
  }
}
""".strip()
