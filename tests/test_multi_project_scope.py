"""Tests for selecting multiple Project V2 boards from one MCP instance."""

from __future__ import annotations

import pytest

from core.access import enforce_scope
from core.config import GitHubProjectSettings, get_settings
from core.exceptions import ScopeLockError
from core.project_scope import configured_project_numbers, project_context, resolve_project_number


@pytest.fixture(autouse=True)
def clear_settings_cache() -> None:
    """Prevent environment-sensitive settings from leaking between tests."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _set_target(monkeypatch: pytest.MonkeyPatch, projects: str = "") -> None:
    monkeypatch.setenv("GH_PROJECT_ORG_NAME", "example-org")
    monkeypatch.setenv("GH_PROJECT_REPO_NAME", "example-repo")
    monkeypatch.setenv("GH_PROJECT_PROJECT_NUMBER", "6")
    monkeypatch.setenv("GH_PROJECT_PROJECT_NUMBERS", projects)
    monkeypatch.setenv("GH_PROJECT_OWNER_TYPE", "organization")


def test_legacy_configuration_keeps_one_default_project(monkeypatch: pytest.MonkeyPatch) -> None:
    """The existing single-project environment remains unchanged."""
    _set_target(monkeypatch)
    settings = GitHubProjectSettings()

    assert settings.configured_project_numbers() == (6,)
    assert configured_project_numbers() == (6,)
    assert resolve_project_number() == 6
    assert project_context().target.project_number == 6


def test_project_allowlist_accepts_additional_boards(monkeypatch: pytest.MonkeyPatch) -> None:
    """Additional project numbers are available without changing the default."""
    _set_target(monkeypatch, "6,5, 9,5")

    assert configured_project_numbers() == (6, 5, 9)
    assert resolve_project_number(9) == 9
    assert project_context(5).target.project_number == 5


def test_project_selector_rejects_board_outside_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit selector cannot silently target an unconfigured board."""
    _set_target(monkeypatch, "6,5")

    with pytest.raises(ScopeLockError, match="GH_PROJECT_PROJECT_NUMBERS"):
        resolve_project_number(9)


def test_invalid_project_allowlist_fails_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Malformed project allowlists fail with an actionable configuration error."""
    _set_target(monkeypatch, "6,nope")

    with pytest.raises(ValueError, match="GH_PROJECT_PROJECT_NUMBERS"):
        GitHubProjectSettings()


def test_scope_lock_allows_configured_additional_project(monkeypatch: pytest.MonkeyPatch) -> None:
    """A locked instance can intentionally fence several configured boards."""
    _set_target(monkeypatch, "6,5")
    monkeypatch.setenv("GH_PROJECT_SCOPE_LOCK", "true")

    enforce_scope(project_number=5)
    with pytest.raises(ScopeLockError, match="project allowlist"):
        enforce_scope(project_number=9)
