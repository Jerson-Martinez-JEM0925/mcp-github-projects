"""Tests for pull-request reviewer and metadata mutations."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from clients.gh_cli_client import CommandResult
from tools.nice_to_have import (
    PullRequestMetadataInput,
    _apply_pull_request_metadata,
)


def _result(payload: object) -> CommandResult:
    """Build a successful gh CLI result."""
    return CommandResult(stdout=json.dumps(payload), stderr="", return_code=0)


@pytest.mark.anyio
async def test_requests_user_and_team_reviewers() -> None:
    gh_client = Mock()
    gh_client.run = AsyncMock(side_effect=[
        _result({"user": {"login": "author"}}),
        _result({"requested_reviewers": []}),
    ])

    result = await _apply_pull_request_metadata(
        repo="owner/repo",
        pull_number=12,
        gh_client=gh_client,
        params=PullRequestMetadataInput(
            reviewers=["ffactib"],
            team_reviewers=["maintainers"],
        ),
    )

    assert result == [
        {"field": "reviewers", "outcome": "success", "value": "ffactib"},
        {"field": "team_reviewers", "outcome": "success", "value": "maintainers"},
    ]
    request_args = gh_client.run.call_args_list[1].args[0]
    assert "repos/owner/repo/pulls/12/requested_reviewers" in request_args
    assert "reviewers[]=ffactib" in request_args
    assert "team_reviewers[]=maintainers" in request_args


@pytest.mark.anyio
async def test_rejects_requesting_review_from_pr_author() -> None:
    gh_client = Mock()
    gh_client.run = AsyncMock(return_value=_result({"user": {"login": "jersonmartinez"}}))

    result = await _apply_pull_request_metadata(
        repo="owner/repo",
        pull_number=12,
        gh_client=gh_client,
        params=PullRequestMetadataInput(reviewers=["jersonmartinez"]),
    )

    assert result == [{
        "field": "reviewers",
        "outcome": "failure",
        "reason": "Cannot request a review from the pull request author 'jersonmartinez'.",
    }]
    gh_client.run.assert_awaited_once()


@pytest.mark.anyio
async def test_sets_labels_assignees_and_resolved_milestone() -> None:
    gh_client = Mock()
    gh_client.run = AsyncMock(side_effect=[
        _result([{"title": "Sprint 9", "number": 9}]),
        _result({"number": 12}),
    ])

    result = await _apply_pull_request_metadata(
        repo="owner/repo",
        pull_number=12,
        gh_client=gh_client,
        params=PullRequestMetadataInput(
            labels=["feature"],
            assignees=["ffactib"],
            milestone="Sprint 9",
        ),
    )

    assert [item["field"] for item in result] == ["milestone", "labels", "assignees"]
    assert all(item["outcome"] == "success" for item in result)
    patch_args = gh_client.run.call_args_list[1].args[0]
    assert "repos/owner/repo/issues/12" in patch_args
    assert "milestone=9" in patch_args
    assert "labels[]=feature" in patch_args
    assert "assignees[]=ffactib" in patch_args


@pytest.mark.anyio
async def test_applies_explicit_and_default_project_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata = object()
    discovery = Mock()
    discovery.get_cached_or_discover = AsyncMock(return_value=metadata)
    project_service = Mock()
    project_service.resolve_item_id = AsyncMock(return_value="PVTI_item")
    project_service.update_field = AsyncMock()
    factory = Mock()
    factory.graphql = AsyncMock(return_value=object())
    factory.discovery_service = AsyncMock(return_value=discovery)
    factory.project_service = AsyncMock(return_value=project_service)
    settings = SimpleNamespace(
        estimate_min=0.25,
        estimate_max=9999,
        estimate_granularity=0.25,
    )
    gh_client = Mock()

    monkeypatch.setattr("tools.nice_to_have.get_service_factory", lambda: factory)
    monkeypatch.setattr("tools.nice_to_have.get_settings", lambda: settings)
    monkeypatch.setattr(
        "tools.nice_to_have.compute_defaults",
        lambda **kwargs: {"Status": "In Progress", "Priority": "High"},
    )
    result = await _apply_pull_request_metadata(
        repo="owner/repo",
        pull_number=12,
        gh_client=gh_client,
        params=PullRequestMetadataInput(status="In Progress", apply_defaults=True),
    )

    assert result == [
        {"field": "Status", "outcome": "success", "value": "In Progress"},
        {"field": "Priority", "outcome": "success", "value": "High"},
    ]
    assert project_service.update_field.await_count == 2
    project_service.resolve_item_id.assert_awaited_once_with(metadata, 12)


@pytest.mark.anyio
async def test_rejects_estimate_outside_project_granularity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata = object()
    discovery = Mock()
    discovery.get_cached_or_discover = AsyncMock(return_value=metadata)
    project_service = Mock()
    project_service.resolve_item_id = AsyncMock(return_value="PVTI_item")
    project_service.update_field = AsyncMock()
    factory = Mock()
    factory.graphql = AsyncMock(return_value=object())
    factory.discovery_service = AsyncMock(return_value=discovery)
    factory.project_service = AsyncMock(return_value=project_service)
    settings = SimpleNamespace(
        estimate_min=0.25,
        estimate_max=9999,
        estimate_granularity=0.25,
    )

    monkeypatch.setattr("tools.nice_to_have.get_service_factory", lambda: factory)
    monkeypatch.setattr("tools.nice_to_have.get_settings", lambda: settings)
    result = await _apply_pull_request_metadata(
        repo="owner/repo",
        pull_number=12,
        gh_client=Mock(),
        params=PullRequestMetadataInput(estimate=0.3),
    )

    assert result[0]["outcome"] == "failure"
    assert result[0]["field"] == "Estimate"
    project_service.update_field.assert_not_awaited()


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
