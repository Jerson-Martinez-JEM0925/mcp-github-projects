"""Tests for organization and user owner type support in GraphQL queries.

Verifies that the correct query variant and variables are used based
on the owner_type configuration.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("GH_PROJECT_ORG_NAME", "TestOrg")
os.environ.setdefault("GH_PROJECT_REPO_NAME", "TestRepo")
os.environ.setdefault("GH_PROJECT_PROJECT_NUMBER", "1")

from graphql.queries import (
    get_discovery_query,
    get_query_variables,
    extract_project_data,
)
from models.context import ProjectTarget, GitHubContext


def test_get_discovery_query_org():
    """Organization owner_type returns the org query variant."""
    query = get_discovery_query("organization")
    assert "organization(login:" in query
    assert "user(login:" not in query


def test_get_discovery_query_user():
    """User owner_type returns the user query variant."""
    query = get_discovery_query("user")
    assert "user(login:" in query
    assert "organization(login:" not in query


def test_get_query_variables_org():
    """Organization variables use $org key."""
    variables = get_query_variables("ExampleOrg", 1, "organization")
    assert variables == {"org": "ExampleOrg", "number": 1}
    assert "login" not in variables


def test_get_query_variables_user():
    """User variables use $login key."""
    variables = get_query_variables("octocat", 3, "user")
    assert variables == {"login": "octocat", "number": 3}
    assert "org" not in variables


def test_get_query_variables_extra_params():
    """Extra parameters are passed through."""
    variables = get_query_variables("ExampleOrg", 1, "organization", first=50, after="cursor")
    assert variables["first"] == 50
    assert variables["after"] == "cursor"


def test_extract_project_data_org():
    """Extracts project from organization response."""
    response = {"data": {"organization": {"projectV2": {"id": "PVT_123"}}}}
    result = extract_project_data(response, "organization")
    assert result == {"id": "PVT_123"}


def test_extract_project_data_user():
    """Extracts project from user response."""
    response = {"data": {"user": {"projectV2": {"id": "PVT_456"}}}}
    result = extract_project_data(response, "user")
    assert result == {"id": "PVT_456"}


def test_extract_project_data_missing():
    """Returns None when project is not found."""
    response = {"data": {"organization": {}}}
    result = extract_project_data(response, "organization")
    assert result is None


def test_project_target_accepts_user_type():
    """ProjectTarget validates 'user' as a valid owner_type."""
    target = ProjectTarget(
        owner_type="user",
        owner_login="octocat",
        project_number=3,
        repository="hello-world",
    )
    assert target.owner_type == "user"
    assert target.owner_login == "octocat"


def test_project_target_accepts_org_type():
    """ProjectTarget validates 'organization' as a valid owner_type."""
    target = ProjectTarget(
        owner_type="organization",
        owner_login="ExampleOrg",
        project_number=1,
        repository="ExampleOrg",
    )
    assert target.owner_type == "organization"


def test_context_from_settings_reads_owner_type():
    """GitHubContext.from_settings uses the owner_type from config."""
    os.environ["GH_PROJECT_OWNER_TYPE"] = "user"
    os.environ["GH_PROJECT_ORG_NAME"] = "octocat"
    os.environ["GH_PROJECT_REPO_NAME"] = "my-repo"
    os.environ["GH_PROJECT_PROJECT_NUMBER"] = "5"

    from config import GitHubProjectSettings
    settings = GitHubProjectSettings()
    context = GitHubContext.from_settings(
        settings=settings,
        token_provider=lambda: "fake-token",
    )
    assert context.target.owner_type == "user"
    assert context.target.owner_login == "octocat"

    # Reset
    os.environ["GH_PROJECT_OWNER_TYPE"] = "organization"


def test_same_project_number_different_owners():
    """Same project_number with different owners produces different targets."""
    org_target = ProjectTarget(
        owner_type="organization",
        owner_login="ExampleOrg",
        project_number=1,
    )
    user_target = ProjectTarget(
        owner_type="user",
        owner_login="jersonmartinez",
        project_number=1,
    )
    assert org_target.cache_namespace != user_target.cache_namespace


if __name__ == "__main__":
    test_get_discovery_query_org()
    print("✅ test_get_discovery_query_org")

    test_get_discovery_query_user()
    print("✅ test_get_discovery_query_user")

    test_get_query_variables_org()
    print("✅ test_get_query_variables_org")

    test_get_query_variables_user()
    print("✅ test_get_query_variables_user")

    test_get_query_variables_extra_params()
    print("✅ test_get_query_variables_extra_params")

    test_extract_project_data_org()
    print("✅ test_extract_project_data_org")

    test_extract_project_data_user()
    print("✅ test_extract_project_data_user")

    test_extract_project_data_missing()
    print("✅ test_extract_project_data_missing")

    test_project_target_accepts_user_type()
    print("✅ test_project_target_accepts_user_type")

    test_project_target_accepts_org_type()
    print("✅ test_project_target_accepts_org_type")

    test_context_from_settings_reads_owner_type()
    print("✅ test_context_from_settings_reads_owner_type")

    test_same_project_number_different_owners()
    print("✅ test_same_project_number_different_owners")

    print("\nAll owner type tests passed.")
