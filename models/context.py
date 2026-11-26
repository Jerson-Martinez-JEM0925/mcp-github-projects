"""Explicit GitHub target and runtime context abstractions.

The models in this module are the compatibility boundary between the MCP
business logic and deployment-specific settings. They contain no token value;
only the context's non-serializable token provider can resolve credentials at
request time.
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Literal

OwnerType = Literal["organization", "user"]
TokenProvider = Callable[[], str | Awaitable[str]]
_LOGIN_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
_REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_NAMESPACE_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,160}$")


@dataclass(frozen=True, slots=True)
class ProjectTarget:
    """Immutable identity of one GitHub Project V2 destination."""

    owner_type: OwnerType
    owner_login: str
    project_number: int
    repository: str | None = None
    cache_namespace: str | None = None

    def __post_init__(self) -> None:
        if self.owner_type not in ("organization", "user"):
            raise ValueError("owner_type must be 'organization' or 'user'")
        if not _LOGIN_PATTERN.fullmatch(self.owner_login):
            raise ValueError("owner_login must be a valid GitHub login")
        if self.project_number < 1:
            raise ValueError("project_number must be positive")
        if self.repository is not None and not _REPOSITORY_PATTERN.fullmatch(self.repository):
            raise ValueError("repository must be a valid GitHub repository name")

        namespace = self.cache_namespace or self._build_cache_namespace()
        if not _NAMESPACE_PATTERN.fullmatch(namespace):
            raise ValueError("cache_namespace contains unsupported characters")
        object.__setattr__(self, "cache_namespace", namespace)

    def _build_cache_namespace(self) -> str:
        """Build a stable, secret-free namespace for target-local metadata."""
        repository = self.repository or "project"
        return f"{self.owner_type}-{self.owner_login}-{self.project_number}-{repository}"

    @property
    def repository_full_name(self) -> str | None:
        """Return ``owner/repository`` when this target has a repository."""
        if self.repository is None:
            return None
        return f"{self.owner_login}/{self.repository}"

    def public_dict(self) -> dict[str, Any]:
        """Return serializable target metadata without credentials."""
        return {
            "owner_type": self.owner_type,
            "owner_login": self.owner_login,
            "project_number": self.project_number,
            "repository": self.repository,
            "cache_namespace": self.cache_namespace,
        }


@dataclass(frozen=True, slots=True, repr=False)
class GitHubContext:
    """Runtime context for one target and its deferred credential provider."""

    target: ProjectTarget
    token_provider: TokenProvider = field(
        repr=False,
        compare=False,
        metadata={"serialize": False, "sensitive": True},
    )
    capabilities: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if not callable(self.token_provider):
            raise TypeError("token_provider must be callable")
        object.__setattr__(self, "capabilities", frozenset(self.capabilities))

    def __repr__(self) -> str:
        """Render context identity without the provider or token."""
        return (
            "GitHubContext(target="
            f"{self.target!r}, capabilities={sorted(self.capabilities)!r})"
        )

    async def resolve_token(self) -> str:
        """Resolve and validate a credential without storing it in context."""
        result = self.token_provider()
        token = await result if inspect.isawaitable(result) else result
        if not isinstance(token, str) or not token.strip():
            raise ValueError("token_provider returned an empty credential")
        return token.strip()

    def public_dict(self) -> dict[str, Any]:
        """Return context metadata safe for logs, diagnostics and telemetry."""
        return {
            "target": self.target.public_dict(),
            "capabilities": sorted(self.capabilities),
        }

    @classmethod
    def from_settings(
        cls,
        settings: Any | None = None,
        token_provider: TokenProvider | None = None,
        capabilities: Iterable[str] = (),
        project_number: int | None = None,
    ) -> "GitHubContext":
        """Adapt legacy settings into an explicit context.

        The import of ``resolve_token`` is deferred to avoid coupling the
        value model to the authentication module and to keep tests injectable.
        """
        if settings is None:
            from core.config import get_settings

            settings = get_settings()
        if token_provider is None:
            from core.auth import resolve_token

            token_provider = resolve_token

        owner_login = settings.org_name
        owner_type = getattr(settings, "owner_type", "auto")
        # 'auto' (or any unrecognised value) triggers runtime detection so a
        # user-owned board works without GH_PROJECT_OWNER_TYPE=user. Explicit
        # 'organization'/'user' short-circuit detection (backward compatible).
        if owner_type not in ("organization", "user"):
            from services.owner_type_resolver import resolve_owner_type_sync

            owner_type = resolve_owner_type_sync(owner_login)

        target = ProjectTarget(
            owner_type=owner_type,
            owner_login=owner_login,
            project_number=(
                settings.project_number if project_number is None else int(project_number)
            ),
            repository=settings.repo_name,
        )
        return cls(
            target=target,
            token_provider=token_provider,
            capabilities=frozenset(capabilities),
        )
