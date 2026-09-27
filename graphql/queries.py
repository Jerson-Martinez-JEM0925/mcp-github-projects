"""GraphQL query strings for GitHub Projects V2 API.

Contains read-only queries for project discovery and item listing.
Provides separate query variants for organization-owned and user-owned
projects, and a helper to select the correct variant based on owner_type.
"""

from typing import Literal

# ── Discovery Queries ────────────────────────────────────────────────────────

DISCOVERY_QUERY_ORG: str = """
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
            options {
              id
              name
            }
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
""".strip()

DISCOVERY_QUERY_USER: str = """
query DiscoverProject($login: String!, $number: Int!) {
  user(login: $login) {
    projectV2(number: $number) {
      id
      fields(first: 50) {
        nodes {
          ... on ProjectV2SingleSelectField {
            id
            name
            dataType
            options {
              id
              name
            }
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
""".strip()

# ── Item Listing Queries ─────────────────────────────────────────────────────

_ITEMS_FRAGMENT: str = """
      items(first: $first, after: $after) {
        pageInfo {
          hasNextPage
          endCursor
        }
        nodes {
          id
          content {
            ... on Issue {
              number
              title
              body
              url
              state
              assignees(first: 100) {
                nodes {
                  login
                }
              }
              labels(first: 100) {
                nodes {
                  name
                }
              }
            }
            ... on PullRequest {
              number
              title
              body
              url
              state
              assignees(first: 100) {
                nodes {
                  login
                }
              }
              labels(first: 100) {
                nodes {
                  name
                }
              }
            }
            ... on DraftIssue {
              title
              body
            }
            __typename
          }
          fieldValues(first: 100) {
            nodes {
              ... on ProjectV2ItemFieldSingleSelectValue {
                name
                field {
                  ... on ProjectV2SingleSelectField {
                    name
                  }
                }
              }
              ... on ProjectV2ItemFieldDateValue {
                date
                field {
                  ... on ProjectV2Field {
                    name
                  }
                }
              }
              ... on ProjectV2ItemFieldNumberValue {
                number
                field {
                  ... on ProjectV2Field {
                    name
                  }
                }
              }
              ... on ProjectV2ItemFieldTextValue {
                text
                field {
                  ... on ProjectV2Field {
                    name
                  }
                }
              }
              ... on ProjectV2ItemFieldMilestoneValue {
                milestone {
                  title
                }
                field {
                  ... on ProjectV2Field {
                    name
                  }
                }
              }
              ... on ProjectV2ItemFieldIterationValue {
                title
                field {
                  ... on ProjectV2IterationField {
                    name
                  }
                }
              }
            }
          }
        }
      }
"""

LIST_ITEMS_QUERY_ORG: str = (
    "query ListProjectItems($org: String!, $number: Int!, $first: Int!, $after: String) {\n"
    "  organization(login: $org) {\n"
    "    projectV2(number: $number) {\n"
    f"{_ITEMS_FRAGMENT}"
    "    }\n"
    "  }\n"
    "}"
)

LIST_ITEMS_QUERY_USER: str = (
    "query ListProjectItems($login: String!, $number: Int!, $first: Int!, $after: String) {\n"
    "  user(login: $login) {\n"
    "    projectV2(number: $number) {\n"
    f"{_ITEMS_FRAGMENT}"
    "    }\n"
    "  }\n"
    "}"
)

# ── Item Status Query (node-based, owner-agnostic) ───────────────────────────

GET_ITEM_STATUS_QUERY: str = """
query GetItemStatus($itemId: ID!) {
  node(id: $itemId) {
    ... on ProjectV2Item {
      id
      isArchived
      content {
        ... on Issue {
          number
          state
        }
        ... on DraftIssue {
          title
        }
        __typename
      }
      fieldValues(first: 100) {
        nodes {
          ... on ProjectV2ItemFieldSingleSelectValue {
            name
            field {
              ... on ProjectV2SingleSelectField {
                name
              }
            }
          }
        }
      }
    }
  }
}
""".strip()

# ── Backward compatibility aliases ───────────────────────────────────────────
# Legacy code imports DISCOVERY_QUERY and LIST_ITEMS_QUERY directly.
# These default to the organization variants for compatibility.
DISCOVERY_QUERY: str = DISCOVERY_QUERY_ORG
LIST_ITEMS_QUERY: str = LIST_ITEMS_QUERY_ORG


# ── Query Selector ───────────────────────────────────────────────────────────

def get_discovery_query(owner_type: Literal["organization", "user"] = "organization") -> str:
    """Return the appropriate discovery query for the given owner type."""
    if owner_type == "user":
        return DISCOVERY_QUERY_USER
    return DISCOVERY_QUERY_ORG


def get_list_items_query(owner_type: Literal["organization", "user"] = "organization") -> str:
    """Return the appropriate list items query for the given owner type."""
    if owner_type == "user":
        return LIST_ITEMS_QUERY_USER
    return LIST_ITEMS_QUERY_ORG


def get_query_variables(
    owner_login: str,
    project_number: int,
    owner_type: Literal["organization", "user"] = "organization",
    **extra: object,
) -> dict:
    """Build GraphQL variables for the given owner type.

    Organization queries use $org, user queries use $login.
    """
    if owner_type == "user":
        variables = {"login": owner_login, "number": project_number}
    else:
        variables = {"org": owner_login, "number": project_number}
    variables.update(extra)
    return variables


def extract_project_data(response: dict, owner_type: Literal["organization", "user"] = "organization") -> dict | None:
    """Extract the projectV2 node from a response, regardless of owner type."""
    if owner_type == "user":
        return response.get("data", {}).get("user", {}).get("projectV2")
    return response.get("data", {}).get("organization", {}).get("projectV2")


# ── Owner / Repository node-id lookups (for provisioning) ────────────────────
# createProjectV2 needs the owner's node ID; linking a repo needs the repo node
# ID. These small queries resolve those IDs for a user or organization owner.

OWNER_ID_QUERY_USER: str = """
query OwnerId($login: String!) {
  user(login: $login) {
    id
    login
  }
}
""".strip()

OWNER_ID_QUERY_ORG: str = """
query OwnerId($login: String!) {
  organization(login: $login) {
    id
    login
  }
}
""".strip()

REPO_ID_QUERY: str = """
query RepoId($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    id
    nameWithOwner
  }
}
""".strip()


def get_owner_id_query(owner_type: Literal["organization", "user"] = "organization") -> str:
    """Return the owner-id lookup query for the given owner type."""
    if owner_type == "user":
        return OWNER_ID_QUERY_USER
    return OWNER_ID_QUERY_ORG


def extract_owner_id(
    response: dict, owner_type: Literal["organization", "user"] = "organization"
) -> str | None:
    """Extract the owner node ID from an owner-id lookup response."""
    key = "user" if owner_type == "user" else "organization"
    node = response.get("data", {}).get(key)
    if isinstance(node, dict):
        return node.get("id")
    return None

# ── Board structure (issue #26) ──────────────────────────────────────────────

# Current options of one single-select field, with colour and description so
# an options update can preserve what it does not change.
SINGLE_SELECT_FIELD_OPTIONS_QUERY: str = """
query SingleSelectFieldOptions($fieldId: ID!) {
  node(id: $fieldId) {
    ... on ProjectV2SingleSelectField {
      id
      name
      options { id name color description }
    }
  }
}
""".strip()

# Views of a project (id, number, name, layout, filter).
PROJECT_VIEWS_QUERY: str = """
query ProjectViews($projectId: ID!) {
  node(id: $projectId) {
    ... on ProjectV2 {
      url
      views(first: 50) {
        nodes { id number name layout filter }
      }
    }
  }
}
""".strip()
