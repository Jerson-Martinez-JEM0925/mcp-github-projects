"""Unit tests for program-scale diagnostics and traceability helpers."""
from __future__ import annotations

import json

import pytest

from clients.gh_cli_client import CLIError, CommandResult
from core.config import get_settings
from core.error_handling import classify_error
from core.factory import use_service_factory
from tools.meta.nice_to_have import LinkPullRequestInput, link_pull_request
from tools.meta.program_scale import (
    CompareBranchesInput,
    BotCommentsInput,
    MergeReadinessInput,
    PromotionPullRequestInput,
    UpsertIssueBodySectionInput,
    compare_branches,
    create_promotion_pull_request,
    get_pull_request_bot_comments,
    get_pull_request_merge_readiness,
    upsert_issue_body_section,
)
from tools.projects.add_item import AddItemToProjectInput


class FakeGH:
    """Route-based fake for gh commands."""

    def __init__(self, responses: list[object]):
        self.responses = list(responses)
        self.calls: list[list[str]] = []

    async def run(self, args: list[str]) -> CommandResult:
        self.calls.append(args)
        response = self.responses.pop(0) if self.responses else {}
        if isinstance(response, Exception):
            raise response
        return CommandResult(
            stdout=response if isinstance(response, str) else json.dumps(response),
            stderr="",
            return_code=0,
        )

    async def api_graphql(self, query: str, variables: dict) -> dict:
        return {"data": {}}


@pytest.fixture(autouse=True)
def configured_target(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GH_PROJECT_ORG_NAME", "example-org")
    monkeypatch.setenv("GH_PROJECT_REPO_NAME", "example-repo")
    monkeypatch.setenv("GH_PROJECT_PROJECT_NUMBER", "1")
    monkeypatch.setenv("GH_PROJECT_OWNER_TYPE", "organization")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_compare_branches_returns_commit_and_file_summary() -> None:
    gh = FakeGH([
        {
            "ahead_by": 2,
            "behind_by": 1,
            "status": "ahead",
            "total_commits": 2,
            "commits": [
                {"sha": "abc", "commit": {"message": "one\nbody"}},
                {"sha": "def", "commit": {"message": "Merge pull request #9 from example-org/feature"}},
            ],
            "files": [{"filename": "app.py", "additions": 3, "deletions": 1, "changes": 4}],
        },
        [{"number": 9, "title": "Feature", "state": "closed", "merged_at": "2026-01-01T00:00:00Z",
          "html_url": "https://github.com/example-org/example-repo/pull/9", "head": {"ref": "feat/x"}, "base": {"ref": "dev"}}],
        [{"number": 9, "title": "Feature", "state": "closed", "merged_at": "2026-01-01T00:00:00Z",
          "html_url": "https://github.com/example-org/example-repo/pull/9", "head": {"ref": "feat/x"}, "base": {"ref": "dev"}}],
    ])
    with use_service_factory(gh_client=gh):
        result = await compare_branches(CompareBranchesInput(base="qa", head="dev", repository="example-org/example-repo"))
    assert result["ok"] is True
    assert result["data"]["ahead"] == 2
    assert result["data"]["file_totals"]["additions"] == 3
    # Included PRs come from the compared commits (deduplicated), not from PRs whose head is the branch.
    assert [p["number"] for p in result["data"]["pull_requests"]] == [9]
    assert result["data"]["pull_requests_truncated"] is False
    assert gh.calls[1][1] == "repos/example-org/example-repo/commits/abc/pulls?per_page=100"
    assert all("head=example-org:dev" not in " ".join(call) for call in gh.calls)


@pytest.mark.anyio
async def test_compare_branches_excludes_earlier_promotions_and_unmerged_prs() -> None:
    gh = FakeGH([
        {"ahead_by": 1, "behind_by": 0, "status": "ahead", "total_commits": 1,
         "commits": [{"sha": "m1", "commit": {"message": "Merge"}}], "files": []},
        [
            {"number": 3, "title": "Earlier promotion", "merged_at": "2026-01-01T00:00:00Z", "head": {"ref": "dev"}, "base": {"ref": "qa"}},
            {"number": 4, "title": "Open draft", "merged_at": None, "head": {"ref": "feat/y"}, "base": {"ref": "dev"}},
            {"number": 5, "title": "Real change", "merged_at": "2026-01-02T00:00:00Z", "head": {"ref": "fix/z"}, "base": {"ref": "dev"}},
        ],
    ])
    with use_service_factory(gh_client=gh):
        result = await compare_branches(CompareBranchesInput(base="qa", head="dev", repository="example-org/example-repo"))
    assert [p["number"] for p in result["data"]["pull_requests"]] == [5]


@pytest.mark.anyio
async def test_promotion_pull_request_lists_prs_from_compared_commits() -> None:
    gh = FakeGH([
        {"ahead_by": 1, "behind_by": 0, "status": "ahead", "total_commits": 1,
         "commits": [{"sha": "m1", "commit": {"message": "Merge"}}], "files": []},
        [{"number": 5, "title": "Real | change", "merged_at": "2026-01-02T00:00:00Z", "head": {"ref": "fix/z"}, "base": {"ref": "dev"}}],
        {"number": 6, "html_url": "https://github.com/example-org/example-repo/pull/6"},
    ])
    with use_service_factory(gh_client=gh):
        result = await create_promotion_pull_request(
            PromotionPullRequestInput(head="dev", base="qa", repository="example-org/example-repo")
        )
    assert result["ok"] is True
    assert result["data"]["included_pull_requests"] == [5]
    body = next(arg for arg in gh.calls[2] if arg.startswith("body="))
    assert "| #5 | Real \\| change |" in body


@pytest.mark.anyio
async def test_merge_readiness_ignores_comments_after_an_approval() -> None:
    gh = FakeGH([
        {"number": 1, "state": "open", "draft": False, "mergeable": True, "mergeable_state": "clean",
         "base": {"ref": "main"}, "head": {"ref": "qa", "sha": "abc"}},
        {"check_runs": [{"name": "Tests", "status": "completed", "conclusion": "success"}]},
        {"statuses": []},
        [
            {"user": {"login": "reviewer"}, "state": "APPROVED"},
            {"user": {"login": "reviewer"}, "state": "COMMENTED"},
        ],
        {},
        [],
        {"ahead_by": 1, "behind_by": 0},
    ])
    with use_service_factory(gh_client=gh):
        result = await get_pull_request_merge_readiness(
            MergeReadinessInput(pull_number=1, repository="example-org/example-repo")
        )
    assert result["ok"] is True
    assert result["data"]["reviewDecision"] == "APPROVED"


@pytest.mark.anyio
async def test_upsert_body_section_replaces_existing_marker() -> None:
    old = "intro\n\n<!-- mcp:trace:start -->\nold\n<!-- mcp:trace:end -->"
    gh = FakeGH([{"body": old}, {"ok": True}])
    with use_service_factory(gh_client=gh):
        result = await upsert_issue_body_section(
            UpsertIssueBodySectionInput(number=4, section="Trace", content="new", repository="example-org/example-repo")
        )
    assert result["ok"] is True
    patch = " ".join(gh.calls[1])
    assert "new" in patch
    assert "old" not in patch


@pytest.mark.anyio
async def test_bot_comment_filter_and_plan_extraction() -> None:
    gh = FakeGH([[{"id": 1, "user": {"login": "ci[bot]", "type": "Bot"}, "body": "Plan: 1 to add, 2 to change, 0 to destroy"}, {"id": 2, "user": {"login": "human", "type": "User"}, "body": "ignore"}]])
    with use_service_factory(gh_client=gh):
        result = await get_pull_request_bot_comments(BotCommentsInput(pull_number=7, extract_terraform_plan=True, repository="example-org/example-repo"))
    assert result["ok"] is True
    assert result["data"]["count"] == 1
    assert result["data"]["comments"][0]["terraform_plan"] == {"add": 1, "change": 2, "destroy": 0}


@pytest.mark.anyio
async def test_link_keyword_replaces_default_reference_idempotently() -> None:
    body = "Summary\n\nCloses #8"
    gh = FakeGH([
        {"url": "https://github.com/example-org/example-repo/pull/7", "body": body},
        {"ok": True},
        {"ok": True},
    ])
    with use_service_factory(gh_client=gh):
        result = await link_pull_request(LinkPullRequestInput(issue_number=8, pr_number=7, keyword="refs", repository="example-org/example-repo"))
    assert result["ok"] is True
    assert "Refs #8" in " ".join(gh.calls[1])


def test_cli_422_is_validation_error() -> None:
    error = CLIError("validation failed", return_code=1, stderr="gh: Validation Failed (HTTP 422): Cannot change base")
    assert classify_error(error) == "validation"


def test_add_item_accepts_issue_number_alias() -> None:
    params = AddItemToProjectInput(issue_number=12)
    assert params.issue_or_pr_number == 12
