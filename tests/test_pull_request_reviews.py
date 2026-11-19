"""Tests for the pull request review tools (issue #138).

Covers review listing (with inline comment grouping), review submission
(approve / request_changes / comment) via ``gh pr review``, input
validation, and owner/repo overrides fenced by the scope lock.
"""

from __future__ import annotations

import json

import pytest

import tools.pull_requests.reviews as reviews_module
from core.config import get_settings
from tools.pull_requests.reviews import (
    ListPullRequestReviewsInput,
    ReviewPullRequestInput,
    list_pull_request_reviews,
    review_pull_request,
)


class _Result:
    def __init__(self, stdout: str = "", stderr: str = "") -> None:
        self.stdout = stdout
        self.stderr = stderr


class _FakeGH:
    def __init__(self, pages, run_stdout: str = "") -> None:
        self.pages = list(pages)
        self.api_calls: list[str] = []
        self.run_calls: list[list[str]] = []
        self._run_stdout = run_stdout

    async def run(self, argv):
        self.run_calls.append(list(argv))
        if argv and argv[0] == "api":
            self.api_calls.append(argv[1])
            return _Result(json.dumps(self.pages.pop(0)))
        return _Result(self._run_stdout)


class _Factory:
    def __init__(self, gh) -> None:
        self._gh = gh

    async def ensure_auth(self):
        return None

    def gh(self):
        return self._gh


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setenv("GH_PROJECT_ORG_NAME", "octo-org")
    monkeypatch.setenv("GH_PROJECT_REPO_NAME", "octo-repo")
    monkeypatch.setenv("GH_PROJECT_PROJECT_NUMBER", "7")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def gh(monkeypatch):
    def _apply(fake: _FakeGH) -> _FakeGH:
        monkeypatch.setattr(
            reviews_module, "get_service_factory", lambda: _Factory(fake)
        )
        return fake

    return _apply


def _review(rid: int, login: str, state: str, body: str = "") -> dict:
    return {
        "id": rid,
        "user": {"login": login},
        "state": state,
        "body": body,
        "commit_id": "abc123",
        "submitted_at": "2026-01-01T00:00:00Z",
        "html_url": f"https://github.com/octo-org/octo-repo/pull/42#pullrequestreview-{rid}",
    }


@pytest.mark.asyncio
async def test_list_reviews_groups_inline_comments(gh) -> None:
    fake = gh(
        _FakeGH(
            [
                [_review(10, "alice", "APPROVED", "LGTM"), _review(11, "bob", "CHANGES_REQUESTED", "fix tests")],
                [
                    {
                        "pull_request_review_id": 11,
                        "path": "src/app.py",
                        "line": 12,
                        "side": "RIGHT",
                        "user": {"login": "bob"},
                        "body": "this breaks",
                        "html_url": "https://example/c1",
                    }
                ],
            ]
        )
    )

    result = await list_pull_request_reviews(
        ListPullRequestReviewsInput(number=42)
    )

    data = result["data"]
    assert fake.api_calls == [
        "repos/octo-org/octo-repo/pulls/42/reviews?per_page=50",
        "repos/octo-org/octo-repo/pulls/42/comments?per_page=100",
    ]
    assert data["count"] == 2
    assert data["latest_state_by_reviewer"] == {"alice": "APPROVED", "bob": "CHANGES_REQUESTED"}
    assert data["reviews"][1]["comments"][0]["path"] == "src/app.py"
    assert data["reviews"][0]["comments"] == []


@pytest.mark.asyncio
async def test_list_reviews_without_comments(gh) -> None:
    fake = gh(_FakeGH([[_review(10, "alice", "APPROVED")]]))

    result = await list_pull_request_reviews(
        ListPullRequestReviewsInput(number=1, include_comments=False)
    )

    assert len(fake.api_calls) == 1
    assert "comments" not in result["data"]["reviews"][0]


@pytest.mark.asyncio
async def test_review_approve_invokes_gh_pr_review(gh) -> None:
    fake = gh(_FakeGH([], run_stdout="Review submitted"))

    result = await review_pull_request(
        ReviewPullRequestInput(number=7, event="approve", body="ship it")
    )

    assert result["ok"] is True
    assert fake.run_calls == [
        ["pr", "review", "7", "--repo", "octo-org/octo-repo", "--approve", "--body", "ship it"]
    ]


@pytest.mark.asyncio
async def test_review_request_changes_requires_body() -> None:
    with pytest.raises(ValueError):
        ReviewPullRequestInput(number=7, event="request_changes", body="  ")
    with pytest.raises(ValueError):
        ReviewPullRequestInput(number=7, event="comment")


@pytest.mark.asyncio
async def test_review_comment_event(gh) -> None:
    fake = gh(_FakeGH([]))

    result = await review_pull_request(
        ReviewPullRequestInput(number=9, event="comment", body="noted")
    )

    assert result["ok"] is True
    call = fake.run_calls[0]
    assert call[0:3] == ["pr", "review", "9"]
    assert "--comment" in call


@pytest.mark.asyncio
async def test_review_foreign_repo_rejected_when_scope_locked(gh, monkeypatch) -> None:
    monkeypatch.setenv("GH_PROJECT_SCOPE_LOCK", "true")
    get_settings.cache_clear()
    fake = gh(_FakeGH([]))

    result = await review_pull_request(
        ReviewPullRequestInput(
            number=7, event="approve", owner="other", repo="other-repo", body="ok"
        )
    )

    assert result["ok"] is False
    assert result["error_type"] == "validation"
    assert fake.run_calls == []


@pytest.mark.asyncio
async def test_review_foreign_repo_allowed_when_lock_off(gh, monkeypatch) -> None:
    monkeypatch.setenv("GH_PROJECT_SCOPE_LOCK", "false")
    get_settings.cache_clear()
    fake = gh(_FakeGH([]))

    result = await review_pull_request(
        ReviewPullRequestInput(
            number=7, event="approve", owner="other-org", repo="other-repo", body="ok"
        )
    )

    assert result["ok"] is True
    assert "--repo" in fake.run_calls[0]
    assert "other-org/other-repo" in fake.run_calls[0]
