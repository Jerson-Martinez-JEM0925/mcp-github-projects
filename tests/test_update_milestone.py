"""Unit tests for the update_milestone REST tool."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from clients.gh_cli_client import CLIError, CommandResult
from core.config import get_settings
from tools.fields.milestones import UpdateMilestoneInput, update_milestone


MILESTONE_RESPONSE = {
    "number": 5,
    "title": "Release",
    "description": "Ship the release",
    "state": "open",
    "due_on": "2026-11-18T00:00:00Z",
    "html_url": "https://github.com/octo-org/octo-repo/milestone/5",
}


@pytest.fixture(autouse=True)
def configured_target(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep target settings deterministic and isolated between tests."""
    monkeypatch.setenv("GH_PROJECT_ORG_NAME", "octo-org")
    monkeypatch.setenv("GH_PROJECT_REPO_NAME", "octo-repo")
    monkeypatch.setenv("GH_PROJECT_PROJECT_NUMBER", "1")
    monkeypatch.delenv("GH_PROJECT_SCOPE_LOCK", raising=False)
    get_settings.cache_clear()
    monkeypatch.setattr("core.auth.resolve_token", AsyncMock(return_value="tok"))
    yield
    get_settings.cache_clear()


def _result(payload: dict) -> CommandResult:
    return CommandResult(stdout=json.dumps(payload), stderr="", return_code=0)


@pytest.mark.anyio
async def test_update_milestone_sends_only_supplied_fields_and_converts_date() -> None:
    """A partial update preserves unrelated fields and normalizes due_on."""
    run_mock = AsyncMock(return_value=_result(MILESTONE_RESPONSE))

    with patch("clients.gh_cli_client.GHCLIClient.run", new=run_mock):
        result = await update_milestone(
            UpdateMilestoneInput(milestone_number=5, title="Release", due_on="2026-11-18")
        )

    assert result["ok"] is True
    assert result["data"]["updated_fields"] == ["due_on", "title"]
    args = run_mock.call_args.args[0]
    assert "repos/octo-org/octo-repo/milestones/5" in args
    assert "title=Release" in args
    assert "due_on=2026-11-18T00:00:00Z" in args
    assert not any(value.startswith("description=") for value in args)
    assert not any(value.startswith("state=") for value in args)


@pytest.mark.anyio
async def test_update_milestone_can_remove_due_date_with_empty_string() -> None:
    """An empty due_on is sent as JSON null to clear the GitHub date."""
    response = {**MILESTONE_RESPONSE, "due_on": None}
    run_mock = AsyncMock(return_value=_result(response))

    with patch("clients.gh_cli_client.GHCLIClient.run", new=run_mock):
        result = await update_milestone(
            UpdateMilestoneInput(milestone_number=5, due_on="")
        )

    assert result["ok"] is True
    assert "due_on" in result["data"]["updated_fields"]
    args = run_mock.call_args.args[0]
    assert "-F" in args
    assert "due_on=null" in args


@pytest.mark.anyio
async def test_update_milestone_accepts_state() -> None:
    """The supported state values are forwarded to the PATCH request."""
    run_mock = AsyncMock(return_value=_result({**MILESTONE_RESPONSE, "state": "closed"}))

    with patch("clients.gh_cli_client.GHCLIClient.run", new=run_mock):
        result = await update_milestone(
            UpdateMilestoneInput(milestone_number=5, state="closed")
        )

    assert result["ok"] is True
    assert "state=closed" in run_mock.call_args.args[0]


@pytest.mark.anyio
async def test_update_milestone_rejects_invalid_state_before_api_call() -> None:
    """Invalid state values produce validation without calling GitHub."""
    run_mock = AsyncMock(return_value=_result(MILESTONE_RESPONSE))

    with patch("clients.gh_cli_client.GHCLIClient.run", new=run_mock):
        result = await update_milestone(
            UpdateMilestoneInput(milestone_number=5, state="archived")
        )

    assert result["ok"] is False
    assert result["error_type"] == "validation"
    run_mock.assert_not_called()


@pytest.mark.anyio
async def test_update_milestone_rejects_empty_update() -> None:
    """A milestone number alone cannot produce a no-op PATCH request."""
    run_mock = AsyncMock(return_value=_result(MILESTONE_RESPONSE))

    with patch("clients.gh_cli_client.GHCLIClient.run", new=run_mock):
        result = await update_milestone(UpdateMilestoneInput(milestone_number=5))

    assert result["ok"] is False
    assert result["error_type"] == "validation"
    run_mock.assert_not_called()


@pytest.mark.anyio
async def test_update_milestone_maps_404_to_not_found() -> None:
    """A GitHub 404 is exposed as a typed not_found response."""
    run_mock = AsyncMock(
        side_effect=CLIError("gh failed", return_code=1, stderr="HTTP 404: Not Found")
    )

    with patch("clients.gh_cli_client.GHCLIClient.run", new=run_mock):
        result = await update_milestone(
            UpdateMilestoneInput(milestone_number=999, description="x")
        )

    assert result["ok"] is False
    assert result["error_type"] == "not_found"
    assert "999" in result["message"]


@pytest.mark.anyio
async def test_update_milestone_respects_scope_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    """An owner override outside a locked scope is rejected before mutation."""
    monkeypatch.setenv("GH_PROJECT_SCOPE_LOCK", "true")
    get_settings.cache_clear()
    run_mock = AsyncMock(return_value=_result(MILESTONE_RESPONSE))

    with patch("clients.gh_cli_client.GHCLIClient.run", new=run_mock):
        result = await update_milestone(
            UpdateMilestoneInput(milestone_number=5, title="Release", owner="other-org")
        )

    assert result["ok"] is False
    assert result["error_type"] == "validation"
    assert "GH_PROJECT_SCOPE_LOCK" in result["message"]
    run_mock.assert_not_called()


@pytest.fixture
def anyio_backend() -> str:
    """Use asyncio for the async tool tests."""
    return "asyncio"
