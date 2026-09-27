"""Single construction point for clients and services (HARDENING_200 item 6).

Before this module every tool built its own ``GraphQLClient(token=...)``,
``GHCLIClient()``, ``CacheManager()`` and services inline — the same six lines
repeated 69 times across 23 files. Tools now ask the active
:class:`ServiceFactory` instead::

    factory = get_service_factory()
    discovery = await factory.discovery_service()
    project = await factory.project_service()

The default factory builds fresh objects on every call, exactly as the inline
code did, so runtime behaviour is unchanged. Tests install a factory with
injected fakes through :func:`use_service_factory`; the fakes only need to
satisfy the :mod:`core.protocols` interfaces, so no module attribute has to be
patched. Imports of the concrete classes are deferred to call time to keep
``core`` free of import cycles with ``clients`` and ``services``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from core.protocols import GHCLIRunner, GraphQLExecutor

if TYPE_CHECKING:
    from clients.cache_manager import CacheManager
    from services.discovery_service import DiscoveryService
    from services.field_service import FieldService
    from services.issue_service import IssueService
    from services.project_service import ProjectService


class ServiceFactory:
    """Builds GitHub clients and services, with optional injected overrides.

    Args:
        graphql_client: Use this object for every GraphQL call instead of a
            real ``GraphQLClient``. Token resolution is skipped for it.
        gh_client: Use this object for every ``gh`` call instead of a real
            ``GHCLIClient``.
        cache_manager: Use this cache manager instead of a real one.
        token_resolver: Coroutine returning the GitHub token. Defaults to
            ``core.auth.resolve_token``.
    """

    def __init__(
        self,
        *,
        graphql_client: GraphQLExecutor | None = None,
        gh_client: GHCLIRunner | None = None,
        cache_manager: CacheManager | None = None,
        token_resolver: Callable[[], Awaitable[str]] | None = None,
    ) -> None:
        self._graphql_override = graphql_client
        self._gh_override = gh_client
        self._cache_override = cache_manager
        self._token_resolver = token_resolver

    # ── Auth ────────────────────────────────────────────────────────────────

    async def token(self) -> str:
        """Resolve the GitHub token (exits cleanly when none is configured)."""
        if self._token_resolver is not None:
            return await self._token_resolver()
        from core.auth import resolve_token

        return await resolve_token()

    async def ensure_auth(self) -> None:
        """Fail fast when no token is available, without building a client."""
        if self._graphql_override is None and self._gh_override is None:
            await self.token()

    # ── Transports ──────────────────────────────────────────────────────────

    async def graphql(self) -> GraphQLExecutor:
        """Return a GraphQL executor, resolving the token when needed."""
        if self._graphql_override is not None:
            return self._graphql_override
        from clients.graphql_client import GraphQLClient

        return GraphQLClient(token=await self.token())

    def gh(self) -> GHCLIRunner:
        """Return a ``gh`` CLI runner (``gh`` reads the token from the env)."""
        if self._gh_override is not None:
            return self._gh_override
        from clients.gh_cli_client import GHCLIClient

        return GHCLIClient()

    def cache_manager(self) -> CacheManager:
        """Return the project-metadata cache manager."""
        if self._cache_override is not None:
            return self._cache_override
        from clients.cache_manager import CacheManager

        return CacheManager()

    # ── Services ────────────────────────────────────────────────────────────

    async def discovery_service(self, graphql: GraphQLExecutor | None = None) -> DiscoveryService:
        """Return a ``DiscoveryService`` sharing ``graphql`` when given."""
        from services.discovery_service import DiscoveryService

        return DiscoveryService(
            graphql_client=graphql or await self.graphql(),
            cache_manager=self.cache_manager(),
        )

    async def project_service(
        self,
        graphql: GraphQLExecutor | None = None,
        gh: GHCLIRunner | None = None,
    ) -> ProjectService:
        """Return a ``ProjectService`` sharing the given transports."""
        from services.project_service import ProjectService

        return ProjectService(
            graphql_client=graphql or await self.graphql(),
            gh_client=gh or self.gh(),
        )

    async def field_service(self, graphql: GraphQLExecutor | None = None) -> FieldService:
        """Return a ``FieldService`` sharing ``graphql`` when given."""
        from services.field_service import FieldService

        return FieldService(graphql_client=graphql or await self.graphql())

    def issue_service(self, gh: GHCLIRunner | None = None) -> IssueService:
        """Return an ``IssueService`` sharing ``gh`` when given."""
        from services.issue_service import IssueService

        return IssueService(gh_client=gh or self.gh())


_default = ServiceFactory()
_active: ServiceFactory = _default


def get_service_factory() -> ServiceFactory:
    """Return the factory tools should use for the current process."""
    return _active


def set_service_factory(factory: ServiceFactory | None) -> None:
    """Install ``factory`` process-wide; ``None`` restores the default."""
    global _active
    _active = factory or _default


@contextmanager
def use_service_factory(factory: ServiceFactory | None = None, **overrides: Any) -> Iterator[ServiceFactory]:
    """Temporarily install a factory (or one built from ``overrides``)."""
    installed = factory or ServiceFactory(**overrides)
    previous = _active
    set_service_factory(installed)
    try:
        yield installed
    finally:
        set_service_factory(previous)


__all__ = [
    "ServiceFactory",
    "get_service_factory",
    "set_service_factory",
    "use_service_factory",
]
