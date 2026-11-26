"""Tests for multi-target profile system and credential isolation."""

from pathlib import Path

import pytest


@pytest.fixture
def tmp_profiles_dir(tmp_path: Path) -> Path:
    """Create a temporary profiles directory with test env files."""
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    return profiles


@pytest.fixture
def profile_alpha(tmp_profiles_dir: Path) -> Path:
    """Profile targeting org-alpha/repo-alpha/project-1."""
    env_file = tmp_profiles_dir / "alpha.env"
    env_file.write_text(
        "GH_PROJECT_ORG_NAME=org-alpha\n"
        "GH_PROJECT_REPO_NAME=repo-alpha\n"
        "GH_PROJECT_PROJECT_NUMBER=1\n"
        "GH_PROJECT_OWNER_TYPE=organization\n"
    )
    return env_file


@pytest.fixture
def profile_beta(tmp_profiles_dir: Path) -> Path:
    """Profile targeting org-beta/repo-beta/project-2."""
    env_file = tmp_profiles_dir / "beta.env"
    env_file.write_text(
        "GH_PROJECT_ORG_NAME=org-beta\n"
        "GH_PROJECT_REPO_NAME=repo-beta\n"
        "GH_PROJECT_PROJECT_NUMBER=2\n"
        "GH_PROJECT_OWNER_TYPE=organization\n"
    )
    return env_file


@pytest.fixture
def profile_with_token(tmp_profiles_dir: Path) -> Path:
    """Profile that illegally contains a GITHUB_TOKEN."""
    env_file = tmp_profiles_dir / "bad.env"
    env_file.write_text(
        "GH_PROJECT_ORG_NAME=evil-org\n"
        "GH_PROJECT_REPO_NAME=evil-repo\n"
        "GH_PROJECT_PROJECT_NUMBER=1\n"
        "GITHUB_TOKEN=ghp_should_not_be_here\n"
    )
    return env_file


class TestTwoProfilesDifferentCache:
    """Two profiles must produce different cache paths."""

    def test_two_profiles_different_cache(
        self, tmp_profiles_dir: Path, profile_alpha: Path, profile_beta: Path
    ) -> None:
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from profiles import ProfileRegistry

        registry = ProfileRegistry(profiles_dir=tmp_profiles_dir)
        alpha = registry.load("alpha")
        beta = registry.load("beta")

        settings_a = alpha.load_settings()
        settings_b = beta.load_settings()

        assert settings_a.cache_path != settings_b.cache_path
        assert settings_a.org_name == "org-alpha"
        assert settings_b.org_name == "org-beta"


class TestProfileLoadsFromEnvFile:
    """Named profile loads correct settings from its env file."""

    def test_profile_loads_from_env_file(
        self, tmp_profiles_dir: Path, profile_alpha: Path
    ) -> None:
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from profiles import ProfileRegistry

        registry = ProfileRegistry(profiles_dir=tmp_profiles_dir)
        profile = registry.load("alpha")
        settings = profile.load_settings()

        assert settings.org_name == "org-alpha"
        assert settings.repo_name == "repo-alpha"
        assert settings.project_number == 1
        assert settings.owner_type == "organization"


class TestTokenNotInProfileFile:
    """Profile containing a token key must raise ValueError."""

    def test_token_not_in_profile_file(
        self, tmp_profiles_dir: Path, profile_with_token: Path
    ) -> None:
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from profiles import ProfileConfig

        profile = ProfileConfig(name="bad", env_file_path=profile_with_token)

        with pytest.raises(ValueError, match="must not contain token key"):
            profile.load_settings()


class TestDefaultProfileUsesEnv:
    """Empty GH_PROJECT_PROFILE falls back to env vars directly."""

    def test_default_profile_uses_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

        monkeypatch.delenv("GH_PROJECT_PROFILE", raising=False)

        from profiles import get_active_profile

        assert get_active_profile() == ""

    def test_profile_env_var_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

        monkeypatch.setenv("GH_PROJECT_PROFILE", "alpha")

        from profiles import get_active_profile

        assert get_active_profile() == "alpha"
