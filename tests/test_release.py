"""Release plumbing: version single source of truth and release-notes guard."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import release_notes  # noqa: E402
from core.version import VERSION  # noqa: E402


def test_version_is_semver() -> None:
    parts = VERSION.split(".")
    assert len(parts) == 3 and all(p.isdigit() for p in parts)


def test_changelog_has_a_section_for_the_current_version() -> None:
    notes = release_notes.notes_for(f"v{VERSION}")
    assert "### " in notes


@pytest.mark.parametrize(
    ("tag", "error"),
    [("1.1.0", "must look like"), ("v1.1", "must look like"), ("v9.9.9", "core/version.py")],
)
def test_bad_tags_are_refused(tag: str, error: str) -> None:
    with pytest.raises(ValueError, match=error):
        release_notes.notes_for(tag)


def test_cli_exit_codes(capsys: pytest.CaptureFixture[str]) -> None:
    assert release_notes.main([f"v{VERSION}"]) == 0
    assert release_notes.main(["v0.0.1"]) == 1
    assert "error:" in capsys.readouterr().err


def test_server_info_reports_the_version(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from core.config import get_settings
    from tools.meta.server_info import ServerInfoInput, server_info

    monkeypatch.setenv("GH_PROJECT_ORG_NAME", "octo-org")
    monkeypatch.setenv("GH_PROJECT_REPO_NAME", "octo-repo")
    monkeypatch.setenv("GH_PROJECT_PROJECT_NUMBER", "7")
    get_settings.cache_clear()
    try:
        result = asyncio.run(server_info(ServerInfoInput()))
    finally:
        get_settings.cache_clear()
    assert result["data"]["version"] == VERSION
