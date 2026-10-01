"""Issue creation must never report failure for an issue that already exists.

`gh issue create --assignee X` creates the issue first and only then sets the
assignees. When the token may not assign (for example, a fork contributor on
the upstream repository) gh exits non-zero, the tool used to answer "Failed to
create issue" and a retry created a duplicate. Assignees are now applied as a
separate, best-effort step reported through ``warnings``.
"""

import os
import sys
from unittest.mock import AsyncMock, patch

import pytest

os.environ.setdefault("GH_PROJECT_ORG_NAME", "jersonmartinez")
os.environ.setdefault("GH_PROJECT_REPO_NAME", "mcp-github-projects")
os.environ.setdefault("GH_PROJECT_PROJECT_NUMBER", "11")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from clients.gh_cli_client import CLIError, CommandResult  # noqa: E402
from core.config import get_settings  # noqa: E402
from models.context import GitHubContext  # noqa: E402
from services.issue_service import IssueService  # noqa: E402

ISSUE_URL = "https://github.com/jersonmartinez/mcp-github-projects/issues/37\n"
PERMISSION_ERROR = (
    "GraphQL: someone does not have the correct permissions to execute "
    "`ReplaceActorsForAssignable` (replaceActorsForAssignable)"
)


@pytest.fixture(autouse=True)
def configured_target(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GH_PROJECT_ORG_NAME", "jersonmartinez")
    monkeypatch.setenv("GH_PROJECT_REPO_NAME", "mcp-github-projects")
    monkeypatch.setenv("GH_PROJECT_PROJECT_NUMBER", "11")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _service(run: AsyncMock) -> IssueService:
    gh = AsyncMock()
    gh.run = run
    return IssueService(gh, GitHubContext.from_settings(token_provider=lambda: "tok"))


def _ok(stdout: str = "") -> CommandResult:
    return CommandResult(stdout=stdout, stderr="", return_code=0)


@pytest.mark.anyio
async def test_assignees_are_not_passed_to_issue_create() -> None:
    run = AsyncMock(side_effect=[_ok(ISSUE_URL), _ok()])
    created = await _service(run).create(title="T", body="B", labels=["bug"], assignees=["ana", "ana", "luis"])

    create_args = run.await_args_list[0].args[0]
    assert create_args[:2] == ["issue", "create"]
    assert "--assignee" not in create_args
    assert create_args[create_args.index("--label") + 1] == "bug"

    edit_args = run.await_args_list[1].args[0]
    assert edit_args[:3] == ["issue", "edit", "37"]
    assert edit_args[edit_args.index("--add-assignee") + 1] == "ana,luis"
    assert created.number == 37
    assert created.warnings == ()


@pytest.mark.anyio
async def test_assignee_permission_error_is_a_warning_not_a_failure() -> None:
    run = AsyncMock(side_effect=[_ok(ISSUE_URL), CLIError("gh failed", 1, PERMISSION_ERROR)])
    created = await _service(run).create(title="T", assignees=["ana"])

    assert created.number == 37
    assert len(created.warnings) == 1
    assert "Issue #37 was created" in created.warnings[0]
    assert "ReplaceActorsForAssignable" in created.warnings[0]


@pytest.mark.anyio
async def test_no_assignees_means_a_single_gh_call() -> None:
    run = AsyncMock(return_value=_ok(ISSUE_URL))
    created = await _service(run).create(title="T")
    assert run.await_count == 1
    assert created.warnings == ()


@pytest.mark.anyio
async def test_create_project_item_returns_success_with_warnings() -> None:
    from services.issue_service import CreatedIssue
    from tools.issues import create_item as module

    created = CreatedIssue(number=37, url=ISSUE_URL.strip(), warnings=("Issue #37 was created, but assignees ana could not be set: denied",))
    issue_service = AsyncMock()
    issue_service.create = AsyncMock(return_value=created)
    project_service = AsyncMock()
    project_service.add_item = AsyncMock(return_value="PVTI_x")
    discovery = AsyncMock()
    discovery.get_cached_or_discover = AsyncMock(return_value=AsyncMock(fields={}))

    factory = AsyncMock()
    factory.graphql = AsyncMock(return_value=object())
    factory.gh = lambda: object()
    factory.discovery_service = AsyncMock(return_value=discovery)
    factory.project_service = AsyncMock(return_value=project_service)
    factory.issue_service = lambda _gh: issue_service

    with patch.object(module, "get_service_factory", return_value=factory), \
            patch.object(module, "compute_defaults", return_value={}):
        result = await module.create_project_item(title="T", assignees=["ana"])

    assert result["ok"] is True, result
    assert result["data"]["issue_number"] == 37
    assert result["data"]["warnings"] == list(created.warnings)
    assert "see warnings" in result["data"]["message"]
