"""Multi-target profile system for credential isolation.

Each profile declares a distinct GitHub target (org, repo, project)
and resolves to its own GitHubProjectSettings instance with an isolated
cache path.  Tokens NEVER appear in profile files — they come from
environment variables or `gh auth token` at runtime.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic_settings import SettingsConfigDict

if TYPE_CHECKING:
    from core.config import GitHubProjectSettings

_PROFILES_DIR = Path(__file__).resolve().parent / "profiles"

# Keys that must never appear in a profile .env file
_FORBIDDEN_KEYS = frozenset({
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "GH_PROJECT_TOKEN",
})


@dataclass(frozen=True, slots=True)
class ProfileConfig:
    """Descriptor for one named target profile."""

    name: str
    env_file_path: Path
    _settings: GitHubProjectSettings | None = field(
        default=None, repr=False, compare=False, init=False
    )

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("Profile name must be non-empty")
        if not self.env_file_path.is_file():
            raise FileNotFoundError(
                f"Profile env file not found: {self.env_file_path}"
            )

    def load_settings(self) -> "GitHubProjectSettings":
        """Load and return GitHubProjectSettings from this profile's env file.

        Validates that no token keys are present in the file.
        """
        self._validate_no_tokens()
        return _build_settings_from_env_file(self.env_file_path)

    def _validate_no_tokens(self) -> None:
        """Raise ValueError if the profile file contains token definitions."""
        content = self.env_file_path.read_text(encoding="utf-8")
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or not stripped:
                continue
            key = stripped.split("=", 1)[0].strip()
            if key in _FORBIDDEN_KEYS:
                raise ValueError(
                    f"Profile '{self.name}' must not contain token key "
                    f"'{key}' — tokens come from environment variables or "
                    f"`gh auth token` only."
                )


class ProfileRegistry:
    """Registry that loads and validates multiple target profiles."""

    def __init__(self, profiles_dir: Path | None = None) -> None:
        self._profiles_dir = profiles_dir or _PROFILES_DIR
        self._loaded: dict[str, ProfileConfig] = {}

    @property
    def profiles_dir(self) -> Path:
        return self._profiles_dir

    def load(self, name: str) -> ProfileConfig:
        """Load a named profile from the profiles directory.

        Args:
            name: Profile name (filename without .env extension, or with it).

        Returns:
            ProfileConfig instance.

        Raises:
            FileNotFoundError: If the profile env file does not exist.
        """
        if name in self._loaded:
            return self._loaded[name]

        env_filename = name if name.endswith(".env") else f"{name}.env"
        env_path = self._profiles_dir / env_filename

        profile = ProfileConfig(name=name, env_file_path=env_path)
        self._loaded[name] = profile
        return profile

    def load_all(self) -> list[ProfileConfig]:
        """Discover and load all .env profiles in the profiles directory."""
        profiles = []
        if not self._profiles_dir.is_dir():
            return profiles
        for env_file in sorted(self._profiles_dir.glob("*.env")):
            name = env_file.stem
            profiles.append(self.load(name))
        return profiles

    def validate_cache_isolation(self) -> None:
        """Ensure no two loaded profiles share the same cache path.

        Raises:
            ValueError: If cache path collision is detected.
        """
        cache_paths: dict[Path, str] = {}
        for name, profile in self._loaded.items():
            settings = profile.load_settings()
            cache_path = settings.cache_path.resolve()
            if cache_path in cache_paths:
                raise ValueError(
                    f"Cache path collision: profiles '{cache_paths[cache_path]}' "
                    f"and '{name}' resolve to the same cache: {cache_path}"
                )
            cache_paths[cache_path] = name


def get_active_profile() -> str:
    """Read the active profile name from GH_PROJECT_PROFILE env var.

    Returns:
        Profile name string, or empty string if no profile is set
        (meaning: use environment variables directly).
    """
    return os.environ.get("GH_PROJECT_PROFILE", "").strip()


def get_settings_for_profile(profile_name: str = "") -> "GitHubProjectSettings":
    """Resolve settings from a named profile or fall back to env vars.

    Args:
        profile_name: Profile to load. Empty string means use env vars
                      directly (legacy behavior).

    Returns:
        GitHubProjectSettings instance.
    """
    if not profile_name:
        from core.config import get_settings
        return get_settings()

    registry = ProfileRegistry()
    profile = registry.load(profile_name)
    return profile.load_settings()


def _build_settings_from_env_file(env_file: Path) -> "GitHubProjectSettings":
    """Construct settings from a profile file without process-env leakage.

    Named profiles are explicit targets. Process environment variables must not
    silently override their owner, repository, project, or cache namespace.
    Tokens remain forbidden in profile files and are still resolved separately.
    """
    from dotenv import dotenv_values
    from core.config import GitHubProjectSettings

    raw_values = dotenv_values(env_file)
    values: dict[str, object] = {}
    for key, value in raw_values.items():
        if value is None or not key.startswith("GH_PROJECT_"):
            continue
        field_name = key.removeprefix("GH_PROJECT_").lower()
        values[field_name] = value

    if "cache_path" not in values:
        org = str(values.get("org_name", "default")).strip() or "default"
        repo = str(values.get("repo_name", "default")).strip() or "default"
        project = str(values.get("project_number", "0")).strip() or "0"
        namespace = f"{org}/{repo}/project-{project}"
        cache_home = os.environ.get("XDG_CACHE_HOME", "").strip()
        if cache_home:
            values["cache_path"] = (
                Path(cache_home).expanduser()
                / "github-project-mcp"
                / namespace
                / "metadata.json"
            )
        else:
            home = Path.home()
            if home.exists() and os.access(home, os.W_OK):
                values["cache_path"] = (
                    home
                    / ".cache"
                    / "github-project-mcp"
                    / namespace
                    / "metadata.json"
                )

    class _ProfileSettings(GitHubProjectSettings):
        model_config = SettingsConfigDict(
            env_prefix="GH_PROJECT_",
            env_file=None,
            extra="ignore",
        )

    return _ProfileSettings(**values)
