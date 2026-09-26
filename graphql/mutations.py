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
