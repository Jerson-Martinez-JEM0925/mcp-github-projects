"""Tests for MCP_ACCESS_LEVEL and the tool access classification (issue #34).

Covers:
  - config parsing of MCP_ACCESS_LEVEL and GH_PROJECT_SCOPE_LOCK (defaults,
    valid values, invalid rejection);
  - the single-source-of-truth classification: EVERY registered tool resolves
    to a read/write/delete tier (no tool is left unclassified);
  - exposure per level (read < write < full), including that the five
    permanent-delete tools are hidden below 'full';
  - the server_info diagnostics tool reports the level and scope lock.

No GitHub API call is made; server registration is a structural, offline check.
"""

import asyncio
import os
import sys
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.access import (  # noqa: E402
    AccessLevel,
    ToolAccess,
    all_classified_tools,
    classify,
    is_exposed,
    level_counts,
    tools_for_level,
)
from core.config import GitHubProjectSettings, get_settings  # noqa: E402

DELETE_TOOLS = {
    "delete_project_item",
    "delete_issue",
    "delete_issue_comment",
    "delete_label",
    "delete_milestone",
}


@pytest.fixture(autouse=True)
def configured_target(monkeypatch: pytest.MonkeyPatch):
    """Deterministic target + clean settings cache around each test."""
    monkeypatch.setenv("GH_PROJECT_ORG_NAME", "jersonmartinez")
    monkeypatch.setenv("GH_PROJECT_REPO_NAME", "mcp-github-projects")
    monkeypatch.setenv("GH_PROJECT_PROJECT_NUMBER", "11")
    monkeypatch.delenv("MCP_ACCESS_LEVEL", raising=False)
    monkeypatch.delenv("GH_PROJECT_SCOPE_LOCK", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# ── Config parsing ───────────────────────────────────────────────────────────


def test_access_level_defaults_to_write() -> None:
    settings = GitHubProjectSettings()
    assert settings.access_level == "write"


def test_scope_lock_defaults_to_false() -> None:
    settings = GitHubProjectSettings()
    assert settings.scope_lock is False


@pytest.mark.parametrize("value", ["read", "write", "full", "READ", "Full", " write "])
def test_access_level_valid_values(monkeypatch, value: str) -> None:
    monkeypatch.setenv("MCP_ACCESS_LEVEL", value)
    settings = GitHubProjectSettings()
    assert settings.access_level == value.strip().lower()


def test_access_level_invalid_rejected(monkeypatch) -> None:
    monkeypatch.setenv("MCP_ACCESS_LEVEL", "admin")
    with pytest.raises(ValueError, match="MCP_ACCESS_LEVEL must be"):
        GitHubProjectSettings()


@pytest.mark.parametrize(
    "raw,expected", [("true", True), ("false", False), ("1", True), ("0", False)]
)
def test_scope_lock_parsing(monkeypatch, raw: str, expected: bool) -> None:
    monkeypatch.setenv("GH_PROJECT_SCOPE_LOCK", raw)
    settings = GitHubProjectSettings()
    assert settings.scope_lock is expected


def test_access_level_reads_mcp_prefixed_var(monkeypatch) -> None:
    """The var is MCP_ACCESS_LEVEL (NOT GH_PROJECT_ACCESS_LEVEL)."""
    monkeypatch.setenv("MCP_ACCESS_LEVEL", "full")
    monkeypatch.delenv("GH_PROJECT_ACCESS_LEVEL", raising=False)
    assert GitHubProjectSettings().access_level == "full"


def test_access_level_parse_helper() -> None:
    assert AccessLevel.parse(None) is AccessLevel.WRITE
    assert AccessLevel.parse("") is AccessLevel.WRITE
    assert AccessLevel.parse("read") is AccessLevel.READ
    assert AccessLevel.parse("FULL") is AccessLevel.FULL
    with pytest.raises(ValueError):
        AccessLevel.parse("nope")


# ── Classification completeness (single source of truth) ─────────────────────


def _registered_tool_names() -> set[str]:
    """Names of every tool the server would register at 'full' level."""
    os.environ["MCP_ACCESS_LEVEL"] = "full"
    get_settings.cache_clear()

    async def _noop(_: str) -> None:
        return None

    with patch("core.auth.validate_scopes", new=_noop):
        import importlib

        import server as server_module

        importlib.reload(server_module)
        tools = asyncio.run(server_module.mcp.list_tools())
    return {t.name for t in tools}


def test_every_registered_tool_is_classified() -> None:
    """No registered tool may be left without a read/write/delete tier."""
    names = _registered_tool_names()
    assert names, "expected the server to register tools"
    unclassified = []
    for name in names:
        try:
            classify(name)
        except KeyError:
            unclassified.append(name)
    assert not unclassified, f"unclassified tools: {sorted(unclassified)}"


def test_classification_returns_valid_tiers() -> None:
    for name in all_classified_tools():
        assert classify(name) in (
            ToolAccess.READ,
            ToolAccess.WRITE,
            ToolAccess.DELETE,
        )


def test_delete_tools_classified_delete() -> None:
    for name in DELETE_TOOLS:
        assert classify(name) is ToolAccess.DELETE


def test_server_info_is_read() -> None:
    assert classify("server_info") is ToolAccess.READ


# ── Exposure per level ───────────────────────────────────────────────────────


def test_read_level_exposes_only_read_tools() -> None:
    exposed = tools_for_level(AccessLevel.READ)
    for name in exposed:
        assert classify(name) is ToolAccess.READ
    # No delete or write tool leaks into read.
    assert not (set(exposed) & DELETE_TOOLS)


def test_write_level_hides_deletes_but_exposes_writes() -> None:
    exposed = set(tools_for_level(AccessLevel.WRITE))
    assert "create_project_item" in exposed
    assert "close_issue" in exposed
    assert not (exposed & DELETE_TOOLS), "deletes must be hidden at write"


def test_full_level_exposes_everything_including_deletes() -> None:
    exposed = set(tools_for_level(AccessLevel.FULL))
    assert DELETE_TOOLS <= exposed
    assert exposed == set(all_classified_tools())


def test_level_monotonicity() -> None:
    """read ⊆ write ⊆ full."""
    read = set(tools_for_level(AccessLevel.READ))
    write = set(tools_for_level(AccessLevel.WRITE))
    full = set(tools_for_level(AccessLevel.FULL))
    assert read <= write <= full


def test_is_exposed_matrix() -> None:
    assert is_exposed("list_labels", AccessLevel.READ)
    assert not is_exposed("create_label", AccessLevel.READ)
    assert is_exposed("create_label", AccessLevel.WRITE)
    assert not is_exposed("delete_label", AccessLevel.WRITE)
    assert is_exposed("delete_label", AccessLevel.FULL)


def test_level_counts_report_hidden_deletes() -> None:
    write = level_counts(AccessLevel.WRITE)
    assert write["hidden_delete_tools"] == len(DELETE_TOOLS)
    full = level_counts(AccessLevel.FULL)
    assert full["hidden_delete_tools"] == 0
    assert full["hidden"] == 0


# ── Diagnostics tool ─────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_server_info_reports_level_and_scope_lock(monkeypatch) -> None:
    monkeypatch.setenv("MCP_ACCESS_LEVEL", "full")
    monkeypatch.setenv("GH_PROJECT_SCOPE_LOCK", "true")
    get_settings.cache_clear()

    from tools.meta.server_info import ServerInfoInput, server_info

    result = await server_info(ServerInfoInput())
    assert result["ok"] is True
    data = result["data"]
    assert data["access_level"] == "full"
    assert data["access_level_variable"] == "MCP_ACCESS_LEVEL"
    assert data["scope_lock"]["enabled"] is True
    assert data["scope_lock"]["variable"] == "GH_PROJECT_SCOPE_LOCK"
    assert data["target"]["repository"] == "jersonmartinez/mcp-github-projects"
    # Delete tools are visible in the exposed list at full.
    assert "delete_issue" in data["exposed_tools"]


@pytest.mark.anyio
async def test_server_info_default_write_hides_deletes(monkeypatch) -> None:
    get_settings.cache_clear()
    from tools.meta.server_info import ServerInfoInput, server_info

    result = await server_info(ServerInfoInput())
    data = result["data"]
    assert data["access_level"] == "write"
    assert data["scope_lock"]["enabled"] is False
    assert "delete_issue" not in data["exposed_tools"]
    assert data["tool_exposure"]["hidden_delete_tools"] == len(DELETE_TOOLS)
