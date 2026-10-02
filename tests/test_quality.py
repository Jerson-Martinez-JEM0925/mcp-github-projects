"""Focused tests for response-quality tools; no network or GitHub mutation."""
from __future__ import annotations

import json

import pytest

from clients.gh_cli_client import CommandResult
from core.config import get_settings
from core.factory import use_service_factory
from tools.quality import (
    ConsistencyInput,
    NumberInput,
    PageInput,
    RepoHealthInput,
    check_conclusion_summary,
    issue_metadata_consistency_report,
    paginated_issue_page,
    pull_request_lifecycle_summary,
    repository_health_summary,
    response_diagnostics,
)

REPO = "repos/octo-org/octo-repo"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def configured_target(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GH_PROJECT_ORG_NAME", "octo-org")
    monkeypatch.setenv("GH_PROJECT_REPO_NAME", "octo-repo")
    monkeypatch.setenv("GH_PROJECT_PROJECT_NUMBER", "7")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class RoutingGH:
    def __init__(self, routes: dict[str, object]):
        self.routes = routes
        self.calls: list[list[str]] = []

    async def run(self, args: list[str]) -> CommandResult:
        self.calls.append(args)
        path = next(value for value in args[1:] if value.startswith("repos/"))
        payload = self.routes.get(path.split("?")[0], {})
        return CommandResult(
            stdout=payload if isinstance(payload, str) else json.dumps(payload),
            stderr="",
            return_code=0,
        )

    async def api_graphql(self, query: str, variables: dict) -> dict:
        return {}


@pytest.mark.anyio
async def test_repository_health_reports_metadata_gaps() -> None:
    gh = RoutingGH({
        REPO: {"visibility": "public", "default_branch": "main", "archived": False},
        f"{REPO}/issues": [{"number": 1, "title": "Needs owner", "state": "open", "labels": [], "assignees": [], "milestone": None}],
    })
    with use_service_factory(gh_client=gh):
        result = await repository_health_summary(RepoHealthInput(limit=5))
    assert result["ok"] is True
    assert result["data"]["metadata_gaps"] == {"assignee": 1, "labels": 1, "milestone": 1}


@pytest.mark.anyio
async def test_check_conclusion_summary_treats_no_checks_as_none() -> None:
    gh = RoutingGH({
        f"{REPO}/pulls/4": {"head": {"sha": "abc"}},
        f"{REPO}/commits/abc/check-runs": {"check_runs": []},
    })
    with use_service_factory(gh_client=gh):
        result = await check_conclusion_summary(NumberInput(number=4))
    assert result["data"]["overall"] == "none"
    assert result["data"]["checked"] is False


@pytest.mark.anyio
async def test_paginated_issue_page_exposes_page_and_has_more() -> None:
    gh = RoutingGH({f"{REPO}/issues": [{"number": 1, "title": "One", "state": "open"}]})
    with use_service_factory(gh_client=gh):
        result = await paginated_issue_page(PageInput(page=2, per_page=1))
    assert result["data"]["page"] == 2
    assert result["data"]["has_more"] is True
    assert result["data"]["issues"][0]["number"] == 1


@pytest.mark.anyio
async def test_metadata_report_names_missing_fields() -> None:
    gh = RoutingGH({f"{REPO}/issues": [{"number": 9, "title": "", "body": "", "labels": [], "assignees": []}]})
    with use_service_factory(gh_client=gh):
        result = await issue_metadata_consistency_report(ConsistencyInput(limit=5))
    assert result["data"]["incomplete"] == 1
    assert set(result["data"]["issues"][0]["missing"]) == {"title", "body", "labels", "assignee"}



@pytest.mark.anyio
async def test_pull_request_lifecycle_accepts_wrapped_reviews_payload() -> None:
    gh = RoutingGH({
        f"{REPO}/pulls/8": {"state": "open", "head": {"sha": "abc", "ref": "feat/x"}, "base": {"ref": "main"}},
        f"{REPO}/pulls/8/reviews": {"reviews": [{"state": "APPROVED"}]},
    })
    with use_service_factory(gh_client=gh):
        result = await pull_request_lifecycle_summary(NumberInput(number=8))
    assert result["ok"] is True
    assert result["data"]["head_ref"] == "feat/x"
    assert result["data"]["review_states"]["APPROVED"] == 1

@pytest.mark.anyio
async def test_response_diagnostics_is_safe_and_structured() -> None:
    result = await response_diagnostics()
    assert result["ok"] is True
    assert result["data"]["repository"] == "octo-org/octo-repo"
    assert "tool_counts" in result["data"]
