"""Tests for the repository provisioning MCP tool."""
from __future__ import annotations

import json
import os
import sys
from unittest.mock import AsyncMock, patch

import pytest

os.environ.setdefault("GH_PROJECT_ORG_NAME", "jersonmartinez")
os.environ.setdefault("GH_PROJECT_REPO_NAME", "mcp-github-projects")
os.environ.setdefault("GH_PROJECT_PROJECT_NUMBER", "11")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from clients.gh_cli_client import CLIError, CommandResult  # noqa: E402
from tools.repositories import CreateRepositoryInput, create_repository  # noqa: E402


REPOSITORY_RESPONSE = {
    "id": 987,
    "node_id": "R_kgDOExample",
    "name": "mcp-monday-projects",
    "full_name": "jersonmartinez/mcp-monday-projects",
    "private": False,
    "visibility": "public",
    "default_branch": "main",
    "html_url": "https://github.com/jersonmartinez/mcp-monday-projects",
    "clone_url": "https://github.com/jersonmartinez/mcp-monday-projects.git",
    "owner": {"login": "jersonmartinez"},
}


def _result(payload: dict) -> CommandResult:
    return CommandResult(stdout=json.dumps(payload), stderr="", return_code=0)


@pytest.mark.anyio
async def test_create_repository_success() -> None:
    """A public user repository returns safe repository metadata."""
    run_mock = AsyncMock(return_value=_result(REPOSITORY_RESPONSE))

    with patch("core.auth.resolve_token", new=AsyncMock(return_value="token")), \
            patch("clients.gh_cli_client.GHCLIClient.run", new=run_mock):
        result = await create_repository(
            CreateRepositoryInput(name="mcp-monday-projects", owner="jersonmartinez")
        )

    assert result["ok"] is True
    assert result["data"]["full_name"] == "jersonmartinez/mcp-monday-projects"
    assert result["data"]["visibility"] == "public"
    called_args = run_mock.call_args.args[0]
    assert "user/repos" in called_args
    assert "name=mcp-monday-projects" in called_args
    assert "visibility=public" in called_args
    assert "auto_init=true" in called_args


@pytest.mark.anyio
async def test_internal_visibility_requires_organization() -> None:
    """Internal repositories cannot be created under a user account."""
    with patch("core.auth.resolve_token", new=AsyncMock(return_value="token")):
        result = await create_repository(
            CreateRepositoryInput(
                name="internal-repository",
                owner="jersonmartinez",
                visibility="internal",
            )
        )

    assert result["ok"] is False
    assert result["error_type"] == "validation"


@pytest.mark.anyio
async def test_duplicate_repository_is_classified_as_validation() -> None:
    """GitHub duplicate-name failures do not become opaque internal errors."""
    run_mock = AsyncMock(
        side_effect=CLIError(
            "GitHub rejected request",
            return_code=422,
            stderr="name already exists on this account",
        )
    )

    with patch("core.auth.resolve_token", new=AsyncMock(return_value="token")), \
            patch("clients.gh_cli_client.GHCLIClient.run", new=run_mock):
        result = await create_repository(
            CreateRepositoryInput(name="mcp-monday-projects", owner="jersonmartinez")
        )

    assert result["ok"] is False
    assert result["error_type"] == "validation"
    assert "already exists" in result["message"]


@pytest.fixture
def anyio_backend() -> str:
    """Use asyncio for the async tool tests."""
    return "asyncio"
