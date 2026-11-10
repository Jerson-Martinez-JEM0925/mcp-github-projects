"""Unit tests for the REST-backed issue dependency tools."""
from __future__ import annotations

import os
from unittest.mock import AsyncMock

import pytest

from clients.gh_cli_client import CLIError, CommandResult
from core.config import get_settings
from core.factory import use_service_factory
from tools.issues.dependencies import (
    IssueDependencyInput,
    ListIssueDependenciesInput,
    add_issue_dependency,
    list_issue_dependencies,
    remove_issue_dependency,
)


@pytest.fixture(autouse=True)
def configured_target(monkeypatch: pytest.MonkeyPatch) -> None:
    """Use a deterministic repository without reading credentials."""
    monkeypatch.setenv("GH_PROJECT_ORG_NAME", "octo-org")
    monkeypatch.setenv("GH_PROJECT_REPO_NAME", "octo-repo")
    monkeypatch.setenv("GH_PROJECT_PROJECT_NUMBER", "1")
    monkeypatch.delenv("GH_PROJECT_SCOPE_LOCK", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _ok(payload: object = None) -> CommandResult:
    import json

    return CommandResult(
        stdout="" if payload is None else json.dumps(payload),
        stderr="",
        return_code=0,
    )


def _gh(*results: CommandResult) -> AsyncMock:
    client = AsyncMock()
    client.run = AsyncMock(side_effect=list(results))
    return client


@pytest.mark.anyio
async def test_list_dependencies_returns_both_directions() -> None:
    gh = _gh(
        _ok([
            {"number": 24, "title": "A", "state": "open", "html_url": "u24", "id": 240},
        ]),
        _ok([
            {"number": 58, "title": "B", "state": "closed", "html_url": "u58", "id": 580},
        ]),
    )
    with use_service_factory(gh_client=gh):
        result = await list_issue_dependencies(ListIssueDependenciesInput(issue_number=26))

    assert result["ok"] is True
    assert result["data"]["blocked_by"] == [
        {"number": 24, "title": "A", "state": "open", "url": "u24"}
    ]
    assert result["data"]["blocking"] == [
        {"number": 58, "title": "B", "state": "closed", "url": "u58"}
    ]
    assert result["data"]["count"] == 2
    assert gh.run.await_args_list[0].args[0] == [
        "api",
        "--header",
        "X-GitHub-Api-Version: 2026-03-10",
        "repos/octo-org/octo-repo/issues/26/dependencies/blocked_by",
    ]


@pytest.mark.anyio
async def test_add_resolves_rest_id_and_posts_numeric_issue_id() -> None:
    gh = _gh(
        _ok([]),
        _ok({"number": 39, "id": 3900}),
        _ok(),
    )
    with use_service_factory(gh_client=gh):
        result = await add_issue_dependency(
            IssueDependencyInput(issue_number=58, blocked_by_issue_number=39)
        )

    assert result == {
        "ok": True,
        "data": {
            "issue_number": 58,
            "blocked_by_issue_number": 39,
            "already_present": False,
            "message": "Issue dependency added successfully.",
        },
    }
    post_args = gh.run.await_args_list[2].args[0]
    assert post_args[-2:] == ["-F", "issue_id=3900"]
    assert post_args[post_args.index("--method") + 1] == "POST"


@pytest.mark.anyio
async def test_add_is_idempotent_without_resolving_or_writing() -> None:
    gh = _gh(_ok([{"number": 39, "title": "Existing"}]))
    with use_service_factory(gh_client=gh):
        result = await add_issue_dependency(
            IssueDependencyInput(issue_number=58, blocked_by_issue_number=39)
        )

    assert result["ok"] is True
    assert result["data"]["already_present"] is True
    assert gh.run.await_count == 1


@pytest.mark.anyio
async def test_remove_is_idempotent_when_dependency_is_absent() -> None:
    gh = _gh(_ok([]))
    with use_service_factory(gh_client=gh):
        result = await remove_issue_dependency(
            IssueDependencyInput(issue_number=58, blocked_by_issue_number=39)
        )

    assert result["ok"] is True
    assert result["data"]["already_absent"] is True
    assert gh.run.await_count == 1


@pytest.mark.anyio
async def test_remove_resolves_id_and_deletes_relationship() -> None:
    gh = _gh(
        _ok([{"number": 39, "title": "Existing"}]),
        _ok({"number": 39, "id": 3900}),
        _ok(),
    )
    with use_service_factory(gh_client=gh):
        result = await remove_issue_dependency(
            IssueDependencyInput(issue_number=58, blocked_by_issue_number=39)
        )

    assert result["ok"] is True
    delete_args = gh.run.await_args_list[2].args[0]
    assert delete_args[delete_args.index("--method") + 1] == "DELETE"
    assert delete_args[-1].endswith("/blocked_by/3900")


@pytest.mark.anyio
async def test_add_maps_404_to_not_found() -> None:
    gh = _gh(CLIError("gh: Not Found (HTTP 404)", 1, "gh: Not Found (HTTP 404)"))
    with use_service_factory(gh_client=gh):
        result = await add_issue_dependency(
            IssueDependencyInput(issue_number=58, blocked_by_issue_number=39)
        )

    assert result["ok"] is False
    assert result["error_type"] == "not_found"


@pytest.mark.anyio
async def test_add_maps_422_to_validation_error() -> None:
    gh = _gh(
        _ok([]),
        _ok({"number": 39, "id": 3900}),
        CLIError("Validation Failed (HTTP 422)", 1, "Validation Failed (HTTP 422)"),
    )
    with use_service_factory(gh_client=gh):
        result = await add_issue_dependency(
            IssueDependencyInput(issue_number=58, blocked_by_issue_number=39)
        )

    assert result["ok"] is False
    assert result["error_type"] == "validation"
