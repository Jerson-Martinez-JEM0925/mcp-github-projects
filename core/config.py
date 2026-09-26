"""Configuration settings for the GitHub Project Management MCP Server.

Uses Pydantic v2 BaseSettings for validated, type-safe configuration.
All values can be overridden via environment variables prefixed with
GH_PROJECT_.

Target fields (org_name, repo_name, project_number) are MANDATORY --
the server will refuse to start without them, providing an actionable
error message.
"""

import os
import sys
import tempfile
from functools import lru_cache
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_cache_path() -> Path:
    """Return a writable, user-private default cache path.

    The path is namespaced by org/repo/project to prevent cross-target
    pollution when the same host runs multiple MCP instances.
    """
    configured = os.environ.get("GH_PROJECT_CACHE_PATH", "").strip()
    if configured:
        return Path(configured).expanduser()

    # Build target-specific subdirectory
    org = os.environ.get("GH_PROJECT_ORG_NAME", "default").strip() or "default"
    repo = os.environ.get("GH_PROJECT_REPO_NAME", "default").strip() or "default"
    project = os.environ.get("GH_PROJECT_PROJECT_NUMBER", "0").strip() or "0"
    namespace = f"{org}/{repo}/project-{project}"

    cache_home = os.environ.get("XDG_CACHE_HOME", "").strip()
    if cache_home:
        return Path(cache_home).expanduser() / "github-project-mcp" / namespace / "metadata.json"

    home = Path.home()
    if home.exists() and os.access(home, os.W_OK):
        return home / ".cache" / "github-project-mcp" / namespace / "metadata.json"

    return Path(tempfile.gettempdir()) / "github-project-mcp" / namespace / "metadata.json"


_MISSING_TARGET_MSG = """\
ERROR: GitHub Project MCP Server — missing required configuration.

The following environment variables MUST be set:

  GH_PROJECT_ORG_NAME       — GitHub owner (organization or user login)
  GH_PROJECT_REPO_NAME      — Repository name
  GH_PROJECT_PROJECT_NUMBER — GitHub Project V2 number (1–100000)

Example (.env file or environment):

  GH_PROJECT_ORG_NAME=my-org
  GH_PROJECT_REPO_NAME=my-repo
  GH_PROJECT_PROJECT_NUMBER=1

For a quick start with an existing profile, copy one of the bundled
.env examples:

  cp profiles/factib.env .env

See docs/SETUP.md for full configuration reference.
"""


class GitHubProjectSettings(BaseSettings):
    """Settings for the GitHub Project Management MCP Server.

    Target fields (org_name, repo_name, project_number) have no defaults
    and must be provided via environment variables or .env file.
    """

    model_config = SettingsConfigDict(
        env_prefix="GH_PROJECT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Profile selection ────────────────────────────────────────
    profile: str = Field(
        default="",
        max_length=100,
        description=(
            "Named profile to load from profiles/<name>.env. "
            "When empty (default), settings are read from env vars / root .env."
        ),
    )

    # ── Target (MANDATORY — no defaults) ─────────────────────────
    org_name: str = Field(
        default="",
        min_length=0,
        max_length=100,
        description="GitHub owner: organization or user login",
    )
    repo_name: str = Field(
        default="",
        min_length=0,
        max_length=100,
        description="Repository name within the owner",
    )
    project_number: int = Field(
        default=0,
        ge=0,
        le=100_000,
        description="GitHub Project V2 board number",
    )
    owner_type: str = Field(
        default="auto",
        description=(
            "Owner type: 'auto' (default — detect at runtime), "
            "'organization', or 'user'. When 'auto', the server queries "
            "GitHub for the login's type so user-owned boards work without "
            "any manual override. Set explicitly to short-circuit detection."
        ),
    )

    # ── Access level & scope lock (issue #34) ────────────────────
    # access_level reads MCP_ACCESS_LEVEL (NO GH_PROJECT_ prefix — parity with
    # mcp-monday-projects, whose write-policy vars are MCP_-prefixed). It
    # governs which tools the server registers: read < write < full.
    access_level: str = Field(
        default="write",
        validation_alias="MCP_ACCESS_LEVEL",
        description=(
            "Server access level: 'read' (only read tools exposed), 'write' "
            "(DEFAULT — today's create/update/close/archive surface, no "
            "permanent deletes), or 'full' (also exposes permanent-delete "
            "tools, each requiring confirm=true). Set via MCP_ACCESS_LEVEL."
        ),
    )
    # scope_lock reads GH_PROJECT_SCOPE_LOCK (prefixed) — parity concept with
    # mcp-monday-projects' MONDAY_WORKSPACE_ID. When true, every tool is
    # confined to the configured org/repo/project target.
    scope_lock: bool = Field(
        default=False,
        description=(
            "When true (GH_PROJECT_SCOPE_LOCK=true), confine every tool to the "
            "configured GH_PROJECT_ORG_NAME / GH_PROJECT_REPO_NAME / "
            "GH_PROJECT_PROJECT_NUMBER; a call targeting any other "
            "owner/repo/project is refused with a typed error naming the "
            "variable, before any mutation. Default false (unrestricted)."
        ),
    )

    # ── Timeouts & Retry ─────────────────────────────────────────
    timeout_seconds: int = Field(default=10, ge=1, le=120)
    retry_delay_seconds: float = Field(default=2.0, ge=0, le=60)
    retry_attempts: int = Field(default=1, ge=0, le=5)

    # ── Cache ────────────────────────────────────────────────────
    cache_ttl_hours: int = Field(default=24, ge=1, le=720)
    cache_path: Path = Field(default_factory=_default_cache_path)

    # ── Pagination & payload limits ──────────────────────────────
    max_items: int = Field(default=200, ge=1, le=1_000)
    page_size: int = Field(default=100, ge=1, le=100)
    max_cli_output_chars: int = Field(default=1_000_000, ge=10_000, le=10_000_000)

    # ── Estimate Validation ──────────────────────────────────────
    estimate_min: float = Field(default=0.25, ge=0)
    estimate_max: float = Field(default=9999.0, gt=0)
    estimate_granularity: float = Field(default=0.25, gt=0)

    # ── Board field defaults & enforcement (issue #14) ───────────
    # Applied by create_project_item / update_project_item_fields when a
    # field is omitted, so a board item never lands with empty custom
    # fields. All are overridable via GH_PROJECT_DEFAULT_* env vars.
    default_due_days: int = Field(
        default=7,
        ge=0,
        le=3650,
        description=(
            "Days from today used for the default Due date when omitted "
            "(GH_PROJECT_DEFAULT_DUE_DAYS)."
        ),
    )
    default_estimate: float = Field(
        default=3.0,
        ge=0,
        description=(
            "Default Estimate (NUMBER field) applied when omitted "
            "(GH_PROJECT_DEFAULT_ESTIMATE)."
        ),
    )
    default_priority: str = Field(
        default="Medium",
        max_length=100,
        description=(
            "Default Priority single-select option when omitted "
            "(GH_PROJECT_DEFAULT_PRIORITY)."
        ),
    )
    default_area: str = Field(
        default="",
        max_length=100,
        description=(
            "Default Area single-select option when omitted; empty means "
            "no default (GH_PROJECT_DEFAULT_AREA)."
        ),
    )
    default_work_type: str = Field(
        default="",
        max_length=100,
        description=(
            "Default Work Type single-select when it cannot be inferred "
            "from labels; empty falls back to 'Feature' "
            "(GH_PROJECT_DEFAULT_WORK_TYPE)."
        ),
    )
    enforce_fields: bool = Field(
        default=False,
        description=(
            "Strict mode: when true, update_project_item_fields returns an "
            "error listing any board fields left unset after applying "
            "defaults (GH_PROJECT_ENFORCE_FIELDS)."
        ),
    )

    @model_validator(mode="after")
    def _validate_target_fields(self) -> "GitHubProjectSettings":
        """Ensure mandatory target fields are set and owner_type is valid."""
        normalized = (self.owner_type or "auto").strip().lower()
        if normalized not in ("auto", "organization", "user"):
            raise ValueError(
                "GH_PROJECT_OWNER_TYPE must be 'auto', 'organization', or "
                f"'user' (got '{self.owner_type}')."
            )
        self.owner_type = normalized

        # Validate & normalize the access level (read | write | full).
        access = (self.access_level or "write").strip().lower()
        if access not in ("read", "write", "full"):
            raise ValueError(
                "MCP_ACCESS_LEVEL must be 'read', 'write', or 'full' "
                f"(got '{self.access_level}')."
            )
        self.access_level = access

        missing = []
        if not self.org_name:
            missing.append("GH_PROJECT_ORG_NAME")
        if not self.repo_name:
            missing.append("GH_PROJECT_REPO_NAME")
        if self.project_number < 1:
            missing.append("GH_PROJECT_PROJECT_NUMBER")

        if missing:
            print(_MISSING_TARGET_MSG, file=sys.stderr)
            raise ValueError(
                f"Missing required configuration: {', '.join(missing)}. "
                "Set them via environment variables or .env file."
            )
        return self


@lru_cache
def get_settings() -> GitHubProjectSettings:
    """Return cached GitHubProjectSettings instance."""
    return GitHubProjectSettings()
