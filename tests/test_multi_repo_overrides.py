"""Tests for the owner/repo override surface (issue #137).

Proves both directions of the scope contract without any network call:

  1. With ``GH_PROJECT_SCOPE_LOCK`` unset, tools that accept the optional
     ``owner``/``repo`` pair address the named repository instead of the
     configured one — the reach is then bounded only by the token.
  2. With ``GH_PROJECT_SCOPE_LOCK=true``, the same overrides are refused by
     ``resolve_repository``/``enforce_scope`` with a validation error naming
     the lock variable, before any ``gh`` call happens.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from clients.gh_cli_client import CommandResult  # noqa: E402
from core.access import resolve_repository  # noqa: E402
from core.config import get_settings  # noqa: E402
from core.exceptions import ScopeLockError  # noqa: E402
from core.factory import use_service_factory  # noqa: E402


@pytest.fixture(autouse=True)
def configured_target(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GH_PROJECT_ORG_NAME", "octo-org")
    monkeypatch.setenv("GH_PROJECT_REPO_NAME", "octo-repo")
    monkeypatch.setenv("GH_PROJECT_PROJECT_NUMBER", "7")
    monkeypatch.delenv("GH_PROJECT_SCOPE_LOCK", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class FakeGH:
    """Records every call; answers {} / [] payloads by path prefix."""

    def __init__(self, routes: dict[str, object] | None = None):
        self.routes = routes or {}
        self.calls: list[list[str]] = []

    async def run(self, args: list[str]) -> CommandResult:
        self.calls.append(args)
        target = next(
            (a for a in args[1:] if a.startswith(("repos/", "search/"))), ""
        )
        payload: object = {}
        for prefix, value in self.routes.items():
            if target.split("?")[0] == prefix or target.startswith(prefix + "?"):
                payload = value
                break
        text = payload if isinstance(payload, str) else json.dumps(payload)
        return CommandResult(stdout=text, stderr="", return_code=0)

    async def api_graphql(self, query: str, variables: dict) -> dict:
        return {}


# ── resolve_repository unit behaviour ────────────────────────────────────────


def test_resolve_repository_defaults_to_configured_target() -> None:
    assert resolve_repository(None, None) == "octo-org/octo-repo"


def test_resolve_repository_accepts_owner_and_repo_override() -> None:
    assert resolve_repository("other-org", "other-repo") == "other-org/other-repo"
    assert resolve_repository(None, "other-repo") == "octo-org/other-repo"


def test_resolve_repository_refuses_foreign_target_when_locked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GH_PROJECT_SCOPE_LOCK", "true")
    get_settings.cache_clear()
    with pytest.raises(ScopeLockError, match="GH_PROJECT_SCOPE_LOCK"):
        resolve_repository("other-org", None)
    with pytest.raises(ScopeLockError, match="GH_PROJECT_SCOPE_LOCK"):
        resolve_repository(None, "other-repo")
    assert resolve_repository("octo-org", "octo-repo") == "octo-org/octo-repo"


# ── pull request tools honour the override ───────────────────────────────────


@pytest.mark.anyio
async def test_list_pull_requests_targets_override_repo() -> None:
    from tools.pull_requests.listing import (
        ListPullRequestsInput,
        list_pull_requests,
    )

    gh = FakeGH({"search/issues": {"items": [], "total_count": 0}})
    with use_service_factory(gh_client=gh):
        result = await list_pull_requests(
            ListPullRequestsInput(owner="acme", repo="widgets")
        )

    assert result["ok"] is True
    assert result["data"]["repository"] == "acme/widgets"
    query = " ".join(gh.calls[0])
    assert "repo:acme/widgets" in query


@pytest.mark.anyio
async def test_pull_request_detail_targets_override_repo() -> None:
    from tools.pull_requests.listing import (
        PullRequestDetailInput,
        get_pull_request_detail,
    )

    gh = FakeGH({
        "repos/acme/widgets/pulls/5": {"number": 5, "body": "", "state": "open"},
        "repos/acme/widgets/pulls/5/reviews": [],
        "repos/acme/widgets/pulls/5/files": [],
    })
    with use_service_factory(gh_client=gh):
        result = await get_pull_request_detail(
            PullRequestDetailInput(number=5, owner="acme", repo="widgets")
        )

    assert result["ok"] is True
    assert result["data"]["repository"] == "acme/widgets"
    assert any("repos/acme/widgets/pulls/5" in " ".join(c) for c in gh.calls)


# ── issue tools honour the override ──────────────────────────────────────────


@pytest.mark.anyio
async def test_comment_issue_targets_override_repo() -> None:
    from tools.issues.comment_issue import CommentIssueInput, comment_issue

    gh = FakeGH()
    gh.routes = {}
    with use_service_factory(gh_client=gh):
        await comment_issue(
            CommentIssueInput(
                issue_number=9, body="hi", owner="acme", repo="widgets"
            )
        )

    args = gh.calls[0]
    assert "--repo" in args
    assert args[args.index("--repo") + 1] == "acme/widgets"


# ── content tools honour the override ────────────────────────────────────────


@pytest.mark.anyio
async def test_list_repository_directory_targets_override_repo() -> None:
    from tools.repository_content import (
        ListDirectoryInput,
        list_repository_directory,
    )

    gh = FakeGH({
        "repos/acme/widgets": {"default_branch": "main"},
        "repos/acme/widgets/contents/docs": [],
    })
    with use_service_factory(gh_client=gh):
        result = await list_repository_directory(
            ListDirectoryInput(path="docs", owner="acme", repo="widgets")
        )

    assert result["ok"] is True
    assert result["data"]["repository"] == "acme/widgets"
    assert any(
        "repos/acme/widgets/contents" in " ".join(c) for c in gh.calls
    )


# ── scope lock still fences every override ───────────────────────────────────


@pytest.mark.anyio
async def test_override_is_refused_when_scope_lock_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tools.pull_requests.listing import (
        ListPullRequestsInput,
        list_pull_requests,
    )

    monkeypatch.setenv("GH_PROJECT_SCOPE_LOCK", "true")
    get_settings.cache_clear()

    gh = FakeGH()
    with use_service_factory(gh_client=gh):
        result = await list_pull_requests(
            ListPullRequestsInput(owner="acme", repo="widgets")
        )

    assert result["ok"] is False
    assert result["error_type"] == "validation"
    assert "GH_PROJECT_SCOPE_LOCK" in result["message"]
    assert gh.calls == []  # refused before any API call
