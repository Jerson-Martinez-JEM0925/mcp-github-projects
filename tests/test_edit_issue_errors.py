"""Tests for edit_issue error reporting.

A `gh issue edit` "not found" can be about the issue, a label, a milestone
or a user; the error returned to the client must carry gh's own reason so the
caller can tell which one (regression for issue #47).
"""

from unittest.mock import AsyncMock, patch

import pytest

from clients.gh_cli_client import CLIError
from core.config import get_settings
from tools.issues.edit_issue import edit_issue


@pytest.fixture(autouse=True)
def configured_target(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GH_PROJECT_ORG_NAME", "jersonmartinez")
    monkeypatch.setenv("GH_PROJECT_REPO_NAME", "mcp-github-projects")
    monkeypatch.setenv("GH_PROJECT_PROJECT_NUMBER", "11")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.anyio
async def test_not_found_error_names_the_missing_resource() -> None:
    stderr = "could not add label: 'chore' not found"
    run_mock = AsyncMock(side_effect=CLIError("gh failed", 1, stderr))

    with patch("core.auth.resolve_token", new=AsyncMock(return_value="tok")), \
            patch("clients.gh_cli_client.GHCLIClient.run", new=run_mock):
        result = await edit_issue(issue_number=46, add_labels=["chore"])

    assert result["ok"] is False
    assert result["error_type"] == "not_found"
    assert "'chore' not found" in result["message"], result["message"]
