"""Tests for the update_pull_request tool.

Verifies that update_pull_request calls the GitHub REST pull-request update
endpoint (PATCH /repos/{owner}/{repo}/pulls/{pull_number} via `gh api`) with
only the supplied fields, returns the {ok, data} success envelope with the
PR number/url/updated_fields, enforces the at-least-one-field and valid-state
rules, and maps a CLI/provider failure to the standard error contract.

The GHCLIClient and token resolution are mocked so no network/gh call is made.
"""

import json
import sys
from unittest.mock import AsyncMock, patch

import pytest

from core.config import get_settings  # noqa: E402
from clients.gh_cli_client import CLIError, CommandResult  # noqa: E402
from tools.nice_to_have import (  # noqa: E402
    UpdatePullRequestInput,
    update_pull_request,
)

PR_API_RESPONSE = {
    "number": 238,
    "html_url": "https://github.com/jersonmartinez/mcp-github-projects/pull/238",
    "title": "Corrected title",
    "state": "open",
    "draft": False,
    "head": {"ref": "feat/x"},
    "base": {"ref": "main"},
}


@pytest.fixture(autouse=True)
def configured_target(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep this module independent from configuration-mutating tests."""
    monkeypatch.setenv("GH_PROJECT_ORG_NAME", "jersonmartinez")
    monkeypatch.setenv("GH_PROJECT_REPO_NAME", "mcp-github-projects")
    monkeypatch.setenv("GH_PROJECT_PROJECT_NUMBER", "12")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _cmd_result(stdout: str) -> CommandResult:
    return CommandResult(stdout=stdout, stderr="", return_code=0)


@pytest.mark.anyio
async def test_update_pull_request_success_multiline_and_boolean() -> None:
    """A multiline body plus a boolean field returns ok:true and the right PATCH."""
    run_mock = AsyncMock(return_value=_cmd_result(json.dumps(PR_API_RESPONSE)))
    multiline_body = "Line one.\n\nCloses #65\n\n- bullet"

    with patch("core.auth.resolve_token", new=AsyncMock(return_value="tok")), \
            patch("clients.gh_cli_client.GHCLIClient.run", new=run_mock):
        result = await update_pull_request(
            UpdatePullRequestInput(
                pull_number=238,
                title="Corrected title",
                body=multiline_body,
                maintainer_can_modify=True,
            )
        )

    assert result["ok"] is True
    assert result["data"]["pr_number"] == 238
    assert result["data"]["pr_url"] == PR_API_RESPONSE["html_url"]
    assert result["data"]["base"] == "main"
    assert result["data"]["updated_fields"] == [
        "body",
        "maintainer_can_modify",
        "title",
    ]

    # Verify the REST endpoint and only-supplied fields were passed.
    called_args = run_mock.call_args.args[0]
    assert "api" in called_args
    assert "--method" in called_args
    assert "PATCH" in called_args
    assert "repos/jersonmartinez/mcp-github-projects/pulls/238" in called_args
    assert "title=Corrected title" in called_args
    assert f"body={multiline_body}" in called_args
    assert "maintainer_can_modify=true" in called_args
    # Fields NOT supplied must not be sent.
    assert not any(a.startswith("base=") for a in called_args)
    assert not any(a.startswith("state=") for a in called_args)


@pytest.mark.anyio
async def test_update_pull_request_empty_update_is_validation_error() -> None:
    """Supplying no optional field returns a validation error without calling gh."""
    run_mock = AsyncMock(return_value=_cmd_result(json.dumps(PR_API_RESPONSE)))

    with patch("core.auth.resolve_token", new=AsyncMock(return_value="tok")), \
            patch("clients.gh_cli_client.GHCLIClient.run", new=run_mock):
        result = await update_pull_request(
            UpdatePullRequestInput(pull_number=238)
        )

    assert result["ok"] is False
    assert result["error_type"] == "validation"
    run_mock.assert_not_called()


@pytest.mark.anyio
async def test_update_pull_request_invalid_state_is_validation_error() -> None:
    """An invalid state value is rejected before any gh call."""
    run_mock = AsyncMock(return_value=_cmd_result(json.dumps(PR_API_RESPONSE)))

    with patch("core.auth.resolve_token", new=AsyncMock(return_value="tok")), \
            patch("clients.gh_cli_client.GHCLIClient.run", new=run_mock):
        result = await update_pull_request(
            UpdatePullRequestInput(pull_number=238, state="merged")
        )

    assert result["ok"] is False
    assert result["error_type"] == "validation"
    run_mock.assert_not_called()


@pytest.mark.anyio
async def test_update_pull_request_valid_state_closed() -> None:
    """state='closed' is accepted and forwarded to the PATCH call."""
    run_mock = AsyncMock(
        return_value=_cmd_result(json.dumps({**PR_API_RESPONSE, "state": "closed"}))
    )

    with patch("core.auth.resolve_token", new=AsyncMock(return_value="tok")), \
            patch("clients.gh_cli_client.GHCLIClient.run", new=run_mock):
        result = await update_pull_request(
            UpdatePullRequestInput(pull_number=238, state="closed")
        )

    assert result["ok"] is True
    assert result["data"]["state"] == "closed"
    assert result["data"]["updated_fields"] == ["state"]
    called_args = run_mock.call_args.args[0]
    assert "state=closed" in called_args


@pytest.mark.anyio
async def test_update_pull_request_cli_error() -> None:
    """A gh CLI/provider failure maps to the standard internal error envelope."""
    run_mock = AsyncMock(
        side_effect=CLIError("gh failed", return_code=1, stderr="Not Found")
    )

    with patch("core.auth.resolve_token", new=AsyncMock(return_value="tok")), \
            patch("clients.gh_cli_client.GHCLIClient.run", new=run_mock):
        result = await update_pull_request(
            UpdatePullRequestInput(pull_number=999999, body="x")
        )

    assert result["ok"] is False
    assert result["error_type"] == "internal"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
