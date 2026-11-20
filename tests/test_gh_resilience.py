"""Resilience tests: gh CLI retry/backoff, transient classification and
enriched error envelopes for timeout/transient failures."""
from __future__ import annotations

import json

import pytest

from clients.gh_cli_client import (
    CLIError,
    CLITimeoutError,
    CommandResult,
    GHCLIClient,
    _is_transient_cli_error,
)
from core.error_handling import handle_tool_error


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GH_PROJECT_ORG_NAME", "octo-org")
    monkeypatch.setenv("GH_PROJECT_REPO_NAME", "octo-repo")
    monkeypatch.setenv("GH_PROJECT_PROJECT_NUMBER", "7")
    monkeypatch.setenv("GH_PROJECT_RETRY_DELAY_SECONDS", "0")
    yield


class TestRetry:
    @pytest.mark.asyncio
    async def test_retries_timeout_then_succeeds(self, monkeypatch):
        calls = {"n": 0}

        async def fake_once(args):
            calls["n"] += 1
            if calls["n"] < 3:
                raise CLITimeoutError("gh command timed out", timeout_seconds=30)
            return CommandResult(stdout='{"ok":true}', stderr="", return_code=0)

        client = GHCLIClient()
        monkeypatch.setattr(client, "_run_once", fake_once)
        result = await client.run(["api", "repos/o/r"])
        assert calls["n"] == 3
        assert result.stdout == '{"ok":true}'

    @pytest.mark.asyncio
    async def test_timeout_error_reports_attempts(self, monkeypatch):
        async def fake_once(args):
            raise CLITimeoutError("gh command timed out", timeout_seconds=30, command="gh api x")

        client = GHCLIClient()
        monkeypatch.setattr(client, "_run_once", fake_once)
        with pytest.raises(CLITimeoutError) as info:
            await client.run(["api", "x"])
        assert info.value.attempts == 3  # 1 + retry_attempts(2)

    @pytest.mark.asyncio
    async def test_retries_transient_cli_error(self, monkeypatch):
        calls = {"n": 0}

        async def fake_once(args):
            calls["n"] += 1
            if calls["n"] == 1:
                raise CLIError("rate limit exceeded", return_code=1, stderr="HTTP 429")
            return CommandResult(stdout="{}", stderr="", return_code=0)

        client = GHCLIClient()
        monkeypatch.setattr(client, "_run_once", fake_once)
        result = await client.run(["api", "x"])
        assert calls["n"] == 2
        assert result.return_code == 0

    @pytest.mark.asyncio
    async def test_non_transient_error_not_retried(self, monkeypatch):
        calls = {"n": 0}

        async def fake_once(args):
            calls["n"] += 1
            raise CLIError("HTTP 404: Not Found", return_code=1, stderr="not found")

        client = GHCLIClient()
        monkeypatch.setattr(client, "_run_once", fake_once)
        with pytest.raises(CLIError):
            await client.run(["api", "x"])
        assert calls["n"] == 1

    @pytest.mark.asyncio
    async def test_write_timeout_never_retried(self, monkeypatch):
        """A timed-out mutation may already be applied upstream — no replay."""
        calls = {"n": 0}

        async def fake_once(args):
            calls["n"] += 1
            raise CLITimeoutError("gh command timed out", timeout_seconds=30)

        client = GHCLIClient()
        monkeypatch.setattr(client, "_run_once", fake_once)
        for args in (
            ["api", "-X", "POST", "repos/o/r/issues"],
            ["api", "repos/o/r/issues", "-f", "title=x"],  # implicit POST
            ["issue", "create", "--title", "x"],
            ["pr", "merge", "1"],
        ):
            calls["n"] = 0
            with pytest.raises(CLITimeoutError) as info:
                await client.run(args)
            assert calls["n"] == 1, args
            assert info.value.safe_to_retry is False, args


class TestReadOnlyDetection:
    @pytest.mark.parametrize(
        "args",
        [
            ["api", "repos/o/r"],
            ["api", "-X", "GET", "repos/o/r/pulls"],
            ["api", "search/issues", "-f", "q=x", "-X", "GET"],
            ["issue", "list"],
            ["pr", "view", "1"],
            ["search", "issues", "bug"],
            ["repo", "view"],
        ],
    )
    def test_read_only(self, args):
        from clients.gh_cli_client import _is_read_only_command

        assert _is_read_only_command(args)

    @pytest.mark.parametrize(
        "args",
        [
            ["api", "-X", "POST", "repos/o/r/issues"],
            ["api", "repos/o/r/issues", "-f", "title=x"],
            ["api", "--method", "PATCH", "repos/o/r/issues/1"],
            ["issue", "create"],
            ["issue", "comment", "1"],
            ["pr", "merge", "1"],
            ["pr", "review", "1", "--approve"],
            ["repo", "create", "x"],
            ["release", "create", "v1"],
        ],
    )
    def test_write(self, args):
        from clients.gh_cli_client import _is_read_only_command

        assert not _is_read_only_command(args)


class TestTransientDetection:
    @pytest.mark.parametrize(
        "stderr",
        ["HTTP 429 rate limit", "secondary rate limit", "HTTP 502 Bad Gateway", "connection reset"],
    )
    def test_transient(self, stderr):
        assert _is_transient_cli_error(CLIError("x", return_code=1, stderr=stderr))

    @pytest.mark.parametrize("stderr", ["HTTP 404 Not Found", "validation failed", "Forbidden"])
    def test_not_transient(self, stderr):
        assert not _is_transient_cli_error(CLIError("x", return_code=1, stderr=stderr))


class TestErrorEnvelope:
    def test_timeout_error_has_structured_fields(self):
        exc = CLITimeoutError(
            "gh command timed out after 30 seconds: gh api repos/o/r/pulls/1/files",
            timeout_seconds=30,
            command="gh api repos/o/r/pulls/1/files",
        )
        exc.attempts = 3
        out = handle_tool_error(exc, context="Get pull request detail failed")
        assert out["ok"] is False
        assert out["error_type"] == "internal"
        assert out["retryable"] is True
        assert out["timeout_seconds"] == 30
        assert out["upstream"] == "github_cli"
        assert out["endpoint"] == "gh api repos/o/r/pulls/1/files"
        assert out["attempts"] == 3

    def test_transient_cli_error_flagged_retryable(self):
        exc = CLIError("gh failed", return_code=1, stderr="HTTP 503")
        out = handle_tool_error(exc, context="x")
        assert out["retryable"] is True
        assert out["upstream"] == "github_cli"

    def test_regular_error_untouched(self):
        out = handle_tool_error(ValueError("bad input"), context="x")
        assert out["ok"] is False
        assert out.get("retryable") is None


class TestPullRequestDetailGranularity:
    @pytest.mark.asyncio
    async def test_include_reviews_false_skips_reviews_call(self, monkeypatch):
        from tools.pull_requests.listing import (
            PullRequestDetailInput,
            get_pull_request_detail,
        )

        calls: list[str] = []

        class FakeGH:
            async def run(self, args):
                calls.append(" ".join(args))
                path = args[1]
                if path.endswith("/reviews?per_page=100"):
                    return CommandResult(stdout="[]", stderr="", return_code=0)
                if path.endswith("/files?per_page=100"):
                    return CommandResult(stdout="[]", stderr="", return_code=0)
                return CommandResult(stdout=json.dumps({"title": "T"}), stderr="", return_code=0)

        class FakeFactory:
            async def ensure_auth(self):
                return None

            def gh(self):
                return FakeGH()

        monkeypatch.setattr(
            "tools.pull_requests.listing.get_service_factory", lambda: FakeFactory()
        )
        out = await get_pull_request_detail(
            PullRequestDetailInput(number=1, include_files=False, include_reviews=False)
        )
        assert out["ok"] is True
        assert out["data"]["reviews_by_user"] is None
        assert not any("reviews" in c for c in calls)
        assert not any("files" in c for c in calls)
