"""Tests for the create_pull_request tool.

Verifies that create_pull_request calls the GitHub REST API
(POST /repos/{owner}/{repo}/pulls via `gh api`) with the correct
arguments, returns the {ok, data} success envelope with pr_number/pr_url,
supports the draft flag, and optionally links the new PR to an issue.

The GHCLIClient and token resolution are mocked so no network/gh call is made.
"""

import json
import os
import sys
from unittest.mock import AsyncMock, patch

import pytest

from core.config import get_settings  # noqa: E402
from clients.gh_cli_client import CLIError, CommandResult  # noqa: E402
from tools.nice_to_have import (  # noqa: E402
    CreatePullRequestInput,
    create_pull_request,
)

PR_API_RESPONSE = {
    "number": 123,
    "html_url": "https://github.com/jersonmartinez/mcp-github-projects/pull/123",
    "state": "open",
}


@pytest.fixture(autouse=True)
def configured_target(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep this module independent from configuration-mutating tests."""
    monkeypatch.setenv("GH_PROJECT_ORG_NAME", "jersonmartinez")
    monkeypatch.setenv("GH_PROJECT_REPO_NAME", "mcp-github-projects")
    monkeypatch.setenv("GH_PROJECT_PROJECT_NUMBER", "11")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _cmd_result(stdout: str) -> CommandResult:
    return CommandResult(stdout=stdout, stderr="", return_code=0)


@pytest.mark.anyio
async def test_create_pull_request_success() -> None:
    """A successful create returns ok:true with pr_number and pr_url."""
    run_mock = AsyncMock(return_value=_cmd_result(json.dumps(PR_API_RESPONSE)))

    with patch("core.auth.resolve_token", new=AsyncMock(return_value="tok")), \
            patch("clients.gh_cli_client.GHCLIClient.run", new=run_mock):
        result = await create_pull_request(
            CreatePullRequestInput(
                title="Add create_pull_request tool",
                head="feat/create-pull-request-tool",
                base="main",
                body="Closes #7",
            )
        )

    assert result["ok"] is True
    assert result["data"]["pr_number"] == 123
    assert result["data"]["pr_url"] == PR_API_RESPONSE["html_url"]
    assert result["data"]["base"] == "main"

    # Verify the REST endpoint and required fields were passed.
    called_args = run_mock.call_args.args[0]
    assert "api" in called_args
    assert "--method" in called_args
    assert "repos/jersonmartinez/mcp-github-projects/pulls" in called_args
    assert "title=Add create_pull_request tool" in called_args
    assert "head=feat/create-pull-request-tool" in called_args
    assert "base=main" in called_args


@pytest.mark.anyio
async def test_create_pull_request_draft_flag() -> None:
    """draft=True is forwarded as draft=true to the REST call."""
    run_mock = AsyncMock(return_value=_cmd_result(json.dumps(PR_API_RESPONSE)))

    with patch("core.auth.resolve_token", new=AsyncMock(return_value="tok")), \
            patch("clients.gh_cli_client.GHCLIClient.run", new=run_mock):
        result = await create_pull_request(
            CreatePullRequestInput(
                title="Draft PR",
                head="feat/x",
                draft=True,
            )
        )

    assert result["ok"] is True
    assert result["data"]["draft"] is True
    called_args = run_mock.call_args.args[0]
    assert "draft=true" in called_args


@pytest.mark.anyio
async def test_create_pull_request_links_issue() -> None:
    """link_to_issue reuses link_pull_request and includes its result."""
    run_mock = AsyncMock(return_value=_cmd_result(json.dumps(PR_API_RESPONSE)))
    link_mock = AsyncMock(return_value={"ok": True, "data": {"linked": True}})

    with patch("core.auth.resolve_token", new=AsyncMock(return_value="tok")), \
            patch("clients.gh_cli_client.GHCLIClient.run", new=run_mock), \
            patch("tools.nice_to_have.link_pull_request", new=link_mock):
        result = await create_pull_request(
            CreatePullRequestInput(
                title="Linked PR",
                head="feat/y",
                link_to_issue=7,
            )
        )

    assert result["ok"] is True
    assert result["data"]["link_result"] == {"ok": True, "data": {"linked": True}}
    # link_pull_request was invoked with the created PR number.
    link_input = link_mock.call_args.args[0]
    assert link_input.issue_number == 7
    assert link_input.pr_number == 123


@pytest.mark.anyio
async def test_create_pull_request_cli_error() -> None:
    """A gh CLI failure is mapped to an internal error envelope."""
    run_mock = AsyncMock(
        side_effect=CLIError("gh failed", return_code=1, stderr="A pull request already exists")
    )

    with patch("core.auth.resolve_token", new=AsyncMock(return_value="tok")), \
            patch("clients.gh_cli_client.GHCLIClient.run", new=run_mock):
        result = await create_pull_request(
            CreatePullRequestInput(title="Dup", head="feat/z")
        )

    assert result["ok"] is False
    assert result["error_type"] == "internal"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
