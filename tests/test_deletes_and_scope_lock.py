"""Tests for permanent-delete confirm gate and scope-lock refusals (issue #34).

All GitHub clients are mocked, so NO real delete or network call is ever made.
Two behaviours are proven:

  1. Each of the five delete tools REFUSES (confirm=false default) without
     touching any client, and the refusal says the op is permanent and points
     to the reversible alternative. With confirm=true it proceeds to the
     (mocked) client call.

  2. Scope lock (GH_PROJECT_SCOPE_LOCK=true) refuses a foreign owner/repo/
     project on the override-accepting tools BEFORE any client call, with a
     typed error naming GH_PROJECT_SCOPE_LOCK. With the lock off, the same
     call proceeds.
"""

import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from clients.gh_cli_client import CommandResult  # noqa: E402
from core.access import enforce_scope  # noqa: E402
from core.config import get_settings  # noqa: E402
from core.exceptions import ScopeLockError  # noqa: E402


@pytest.fixture(autouse=True)
def configured_target(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GH_PROJECT_ORG_NAME", "jersonmartinez")
    monkeypatch.setenv("GH_PROJECT_REPO_NAME", "mcp-github-projects")
    monkeypatch.setenv("GH_PROJECT_PROJECT_NUMBER", "11")
    monkeypatch.delenv("MCP_ACCESS_LEVEL", raising=False)
    monkeypatch.delenv("GH_PROJECT_SCOPE_LOCK", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _cmd(stdout: str = "") -> CommandResult:
    return CommandResult(stdout=stdout, stderr="", return_code=0)


# ── Confirm gate: refuses without confirm, no client touched ─────────────────


@pytest.mark.anyio
async def test_delete_label_refuses_without_confirm() -> None:
    from tools.deletes import DeleteLabelInput, delete_label

    run_mock = AsyncMock()
    with patch("core.auth.resolve_token", new=AsyncMock(return_value="t")), \
            patch("clients.gh_cli_client.GHCLIClient.run", new=run_mock):
        result = await delete_label(DeleteLabelInput(name="bug"))

    assert result["ok"] is False
    assert result["error_type"] == "validation"
    assert "permanent" in result["message"].lower()
    run_mock.assert_not_called()  # no API call without confirm


@pytest.mark.anyio
async def test_delete_issue_refuses_without_confirm_points_to_close() -> None:
    from tools.deletes import DeleteIssueInput, delete_issue

    run_mock = AsyncMock()
    with patch("core.auth.resolve_token", new=AsyncMock(return_value="t")), \
            patch("clients.gh_cli_client.GHCLIClient.run", new=run_mock):
        result = await delete_issue(DeleteIssueInput(issue_number=5))

    assert result["ok"] is False
    assert "close_issue" in result["suggestion"]
    run_mock.assert_not_called()


@pytest.mark.anyio
async def test_delete_milestone_refuses_without_confirm() -> None:
    from tools.deletes import DeleteMilestoneInput, delete_milestone

    run_mock = AsyncMock()
    with patch("core.auth.resolve_token", new=AsyncMock(return_value="t")), \
            patch("clients.gh_cli_client.GHCLIClient.run", new=run_mock):
        result = await delete_milestone(DeleteMilestoneInput(title="Sprint 1"))

    assert result["ok"] is False
    assert "close_milestone" in result["suggestion"]
    run_mock.assert_not_called()


@pytest.mark.anyio
async def test_delete_issue_comment_refuses_without_confirm() -> None:
    from tools.deletes import DeleteIssueCommentInput, delete_issue_comment

    run_mock = AsyncMock()
    with patch("core.auth.resolve_token", new=AsyncMock(return_value="t")), \
            patch("clients.gh_cli_client.GHCLIClient.run", new=run_mock):
        result = await delete_issue_comment(DeleteIssueCommentInput(comment_id=99))

    assert result["ok"] is False
    run_mock.assert_not_called()


@pytest.mark.anyio
async def test_delete_project_item_refuses_without_confirm() -> None:
    from tools.deletes import DeleteProjectItemInput, delete_project_item

    with patch("core.auth.resolve_token", new=AsyncMock(return_value="t")), \
            patch("clients.graphql_client.GraphQLClient") as gql:
        result = await delete_project_item(
            DeleteProjectItemInput(item_id="PVTI_x")
        )

    assert result["ok"] is False
    assert "archive_project_item" in result["suggestion"]
    gql.assert_not_called()  # GraphQL client never constructed


# ── Confirm gate: proceeds with confirm=true (mocked client) ─────────────────


@pytest.mark.anyio
async def test_delete_label_proceeds_with_confirm() -> None:
    from tools.deletes import DeleteLabelInput, delete_label

    run_mock = AsyncMock(return_value=_cmd(""))
    with patch("core.auth.resolve_token", new=AsyncMock(return_value="t")), \
            patch("clients.gh_cli_client.GHCLIClient.run", new=run_mock):
        result = await delete_label(DeleteLabelInput(name="bug", confirm=True))

    assert result["ok"] is True
    assert result["data"]["deleted"] is True
    args = run_mock.call_args.args[0]
    assert args[:2] == ["label", "delete"]
    assert "--yes" in args


@pytest.mark.anyio
async def test_delete_issue_comment_proceeds_with_confirm() -> None:
    from tools.deletes import DeleteIssueCommentInput, delete_issue_comment

    run_mock = AsyncMock(return_value=_cmd(""))
    with patch("core.auth.resolve_token", new=AsyncMock(return_value="t")), \
            patch("clients.gh_cli_client.GHCLIClient.run", new=run_mock):
        result = await delete_issue_comment(
            DeleteIssueCommentInput(comment_id=99, confirm=True)
        )

    assert result["ok"] is True
    args = run_mock.call_args.args[0]
    assert "DELETE" in args
    assert "repos/jersonmartinez/mcp-github-projects/issues/comments/99" in args


@pytest.mark.anyio
async def test_delete_issue_proceeds_with_confirm_uses_graphql() -> None:
    from tools.deletes import DeleteIssueInput, delete_issue

    # First call resolves node_id via REST; GraphQL mutation is mocked.
    run_mock = AsyncMock(return_value=_cmd("I_node123"))
    exec_mock = AsyncMock(
        return_value={"data": {"deleteIssue": {"repository": {"nameWithOwner": "jersonmartinez/mcp-github-projects"}}}}
    )
    gql_instance = MagicMock()
    gql_instance.execute_with_retry = exec_mock

    with patch("core.auth.resolve_token", new=AsyncMock(return_value="t")), \
            patch("clients.gh_cli_client.GHCLIClient.run", new=run_mock), \
            patch("clients.graphql_client.GraphQLClient", return_value=gql_instance):
        result = await delete_issue(DeleteIssueInput(issue_number=5, confirm=True))

    assert result["ok"] is True
    assert result["data"]["deleted"] is True
    # The GraphQL mutation received the resolved node id.
    called_vars = exec_mock.call_args.args[1]
    assert called_vars == {"issueId": "I_node123"}


@pytest.mark.anyio
async def test_delete_project_item_proceeds_with_confirm() -> None:
    from tools.deletes import DeleteProjectItemInput, delete_project_item

    meta = MagicMock()
    meta.project_id = "PVT_kwABC"
    discovery = MagicMock()
    discovery.get_cached_or_discover = AsyncMock(return_value=meta)
    exec_mock = AsyncMock(
        return_value={"data": {"deleteProjectV2Item": {"deletedItemId": "PVTI_x"}}}
    )
    gql_instance = MagicMock()
    gql_instance.execute_with_retry = exec_mock

    with patch("core.auth.resolve_token", new=AsyncMock(return_value="t")), \
            patch("clients.graphql_client.GraphQLClient", return_value=gql_instance), \
            patch("services.discovery_service.DiscoveryService", return_value=discovery):
        result = await delete_project_item(
            DeleteProjectItemInput(item_id="PVTI_x", confirm=True)
        )

    assert result["ok"] is True
    assert result["data"]["deleted_item_id"] == "PVTI_x"
    called_vars = exec_mock.call_args.args[1]
    assert called_vars == {"projectId": "PVT_kwABC", "itemId": "PVTI_x"}


# ── Scope lock: enforce_scope helper ─────────────────────────────────────────


def test_enforce_scope_noop_when_lock_off() -> None:
    # lock off by default — foreign targets are allowed
    enforce_scope(owner="someone-else", repo="other-repo", project_number=999)


def test_enforce_scope_allows_configured_target(monkeypatch) -> None:
    monkeypatch.setenv("GH_PROJECT_SCOPE_LOCK", "true")
    get_settings.cache_clear()
    # matching target (case-insensitive) and None (defaults) are allowed
    enforce_scope(owner="JersonMartinez", repo="mcp-github-projects", project_number=11)
    enforce_scope(owner=None, repo=None, project_number=None)


def test_enforce_scope_refuses_foreign_owner(monkeypatch) -> None:
    monkeypatch.setenv("GH_PROJECT_SCOPE_LOCK", "true")
    get_settings.cache_clear()
    with pytest.raises(ScopeLockError, match="GH_PROJECT_SCOPE_LOCK"):
        enforce_scope(owner="evil-org")


def test_enforce_scope_refuses_foreign_repo(monkeypatch) -> None:
    monkeypatch.setenv("GH_PROJECT_SCOPE_LOCK", "true")
    get_settings.cache_clear()
    with pytest.raises(ScopeLockError, match="GH_PROJECT_SCOPE_LOCK"):
        enforce_scope(repo="other-repo")


def test_enforce_scope_refuses_foreign_project(monkeypatch) -> None:
    monkeypatch.setenv("GH_PROJECT_SCOPE_LOCK", "true")
    get_settings.cache_clear()
    with pytest.raises(ScopeLockError, match="GH_PROJECT_SCOPE_LOCK"):
        enforce_scope(project_number=999)


# ── Scope lock: tool-level refusals (before any client call) ─────────────────


@pytest.mark.anyio
async def test_list_projects_scope_lock_refuses_foreign_owner(monkeypatch) -> None:
    monkeypatch.setenv("GH_PROJECT_SCOPE_LOCK", "true")
    get_settings.cache_clear()
    from tools.project_provisioning import ListProjectsInput, list_projects

    with patch("core.auth.resolve_token", new=AsyncMock(return_value="t")), \
            patch("clients.graphql_client.GraphQLClient") as gql:
        result = await list_projects(ListProjectsInput(owner_login="evil-org"))

    assert result["ok"] is False
    assert result["error_type"] == "validation"
    assert "GH_PROJECT_SCOPE_LOCK" in result["message"]
    gql.assert_not_called()  # refused before any GraphQL call


@pytest.mark.anyio
async def test_link_repository_scope_lock_refuses_foreign_repo(monkeypatch) -> None:
    monkeypatch.setenv("GH_PROJECT_SCOPE_LOCK", "true")
    get_settings.cache_clear()
    from tools.project_provisioning import LinkRepositoryInput, link_repository

    with patch("core.auth.resolve_token", new=AsyncMock(return_value="t")), \
            patch("clients.graphql_client.GraphQLClient") as gql:
        result = await link_repository(
            LinkRepositoryInput(project_id="PVT_x", repo_name="other-repo")
        )

    assert result["ok"] is False
    assert "GH_PROJECT_SCOPE_LOCK" in result["message"]
    gql.assert_not_called()


@pytest.mark.anyio
async def test_create_repository_disabled_under_scope_lock(monkeypatch) -> None:
    monkeypatch.setenv("GH_PROJECT_SCOPE_LOCK", "true")
    get_settings.cache_clear()
    from tools.repositories import CreateRepositoryInput, create_repository

    with patch("core.auth.resolve_token", new=AsyncMock(return_value="t")), \
            patch("clients.gh_cli_client.GHCLIClient.run", new=AsyncMock()) as run_mock:
        result = await create_repository(CreateRepositoryInput(name="new-repo"))

    assert result["ok"] is False
    assert "GH_PROJECT_SCOPE_LOCK" in result["message"]
    run_mock.assert_not_called()


@pytest.mark.anyio
async def test_create_project_disabled_under_scope_lock(monkeypatch) -> None:
    monkeypatch.setenv("GH_PROJECT_SCOPE_LOCK", "true")
    get_settings.cache_clear()
    from tools.project_provisioning import CreateProjectInput, create_project

    with patch("core.auth.resolve_token", new=AsyncMock(return_value="t")), \
            patch("clients.graphql_client.GraphQLClient") as gql:
        result = await create_project(CreateProjectInput(title="New Board"))

    assert result["ok"] is False
    assert "GH_PROJECT_SCOPE_LOCK" in result["message"]
    gql.assert_not_called()


@pytest.mark.anyio
async def test_list_projects_scope_lock_allows_configured_owner(monkeypatch) -> None:
    """With the lock on, the CONFIGURED owner still works."""
    monkeypatch.setenv("GH_PROJECT_SCOPE_LOCK", "true")
    get_settings.cache_clear()
    from tools.project_provisioning import ListProjectsInput, list_projects

    exec_mock = AsyncMock(
        return_value={"data": {"organization": {"projectsV2": {"nodes": []}}}}
    )
    gql_instance = MagicMock()
    gql_instance.execute_with_retry = exec_mock

    with patch("core.auth.resolve_token", new=AsyncMock(return_value="t")), \
            patch("clients.graphql_client.GraphQLClient", return_value=gql_instance):
        result = await list_projects(
            ListProjectsInput(owner_login="jersonmartinez", owner_type="organization")
        )

    assert result["ok"] is True
    exec_mock.assert_awaited()  # proceeded past the scope check
