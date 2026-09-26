"""Structural interfaces for the two GitHub transports (HARDENING_200 item 8).

Services and tools depend on these ``Protocol`` types instead of the concrete
``GraphQLClient`` / ``GHCLIClient`` classes. Any object with matching methods
satisfies them, so tests can inject a small fake without ``unittest.mock``
patching of module attributes, and a contract test can assert that the real
clients still honour the interface (``isinstance`` works because the protocols
are ``runtime_checkable``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from clients.gh_cli_client import CommandResult


@runtime_checkable
class GraphQLExecutor(Protocol):
    """Executes GraphQL operations against the GitHub API."""

    async def execute(self, query: str, variables: dict | None = None) -> dict:
        """Run one operation and return the ``data`` payload."""
        ...

    async def execute_with_retry(
        self,
        query: str,
        variables: dict | None = None,
        *,
        is_mutation: bool = False,
    ) -> dict:
        """Run one operation, retrying reads (never mutations) on timeout."""
        ...


@runtime_checkable
class GHCLIRunner(Protocol):
    """Runs ``gh`` CLI commands."""

    async def run(self, args: list[str]) -> CommandResult:
        """Execute ``gh <args>`` and return its captured output."""
        ...

    async def api_graphql(self, query: str, variables: dict[str, Any]) -> dict:
        """Execute a GraphQL query through ``gh api graphql``."""
        ...


__all__ = ["GHCLIRunner", "GraphQLExecutor"]
