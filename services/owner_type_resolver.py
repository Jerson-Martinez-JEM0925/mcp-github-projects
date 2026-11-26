"""Runtime auto-detection of a GitHub owner's type (user vs organization).

GitHub Project V2 GraphQL queries differ by owner type: an organization
board is reached via ``organization(login:)`` while a user board is reached
via ``user(login:)``. Historically the server assumed ``organization`` and
required the caller to set ``GH_PROJECT_OWNER_TYPE=user`` for a personal
board (see issue #8).

This module resolves the owner type at runtime by asking GitHub what the
login actually is — ``repositoryOwner(login:) { __typename }`` returns
``User`` or ``Organization`` — so a user-owned board just works without any
manual override. The result is cached per-login for the process lifetime.

Resolution precedence (handled by callers):
    explicit ``organization`` / ``user`` → short-circuits detection
    ``auto`` / unset / unknown           → this module

Security: the token value is never logged or included in any output.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess  # nosec B404 - used only for the pinned `gh api graphql` call
from typing import Literal

logger = logging.getLogger(__name__)

OwnerType = Literal["organization", "user"]

# GraphQL: resolve what a login actually is. ``repositoryOwner`` is the
# interface implemented by both User and Organization, so a single query
# works for either without knowing the answer in advance.
_TYPENAME_QUERY = (
    "query($login:String!){repositoryOwner(login:$login){__typename}}"
)

# Process-lifetime cache: login (lowercased) → resolved owner type.
_CACHE: dict[str, OwnerType] = {}

_GH_TIMEOUT_SECONDS = 10


def _typename_to_owner_type(typename: str) -> OwnerType | None:
    """Map a GraphQL ``__typename`` to our owner_type literal."""
    if typename == "Organization":
        return "organization"
    if typename == "User":
        return "user"
    return None


def _detect_via_gh(login: str) -> OwnerType | None:
    """Detect owner type by shelling out to ``gh api graphql``.

    Uses the GitHub CLI's ambient authentication, so no token needs to be
    threaded through the synchronous settings path. Returns ``None`` when
    ``gh`` is unavailable, unauthenticated, times out, or the login is not
    resolvable — the caller then falls back to a safe default.
    """
    try:
        completed = subprocess.run(  # nosec B603 B607 - fixed argv, no shell
            [
                "gh",
                "api",
                "graphql",
                "-f",
                f"query={_TYPENAME_QUERY}",
                "-F",
                f"login={login}",
                "--jq",
                ".data.repositoryOwner.__typename",
            ],
            capture_output=True,
            text=True,
            timeout=_GH_TIMEOUT_SECONDS,
            check=False,
            env=os.environ.copy(),
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None

    if completed.returncode != 0:
        return None

    typename = (completed.stdout or "").strip()
    return _typename_to_owner_type(typename)


def resolve_owner_type_sync(
    login: str,
    *,
    default: OwnerType = "organization",
) -> OwnerType:
    """Resolve an owner's type synchronously, with caching and a safe default.

    Attempts ``gh api graphql`` detection. On any failure (no ``gh``, no
    auth, network/timeout, unresolvable login) the ``default`` is returned so
    the server keeps its historical behaviour instead of crashing.

    Args:
        login: The GitHub owner login (user or organization).
        default: Owner type to assume when detection is not possible.

    Returns:
        ``"user"`` or ``"organization"``.
    """
    if not login:
        return default

    key = login.lower()
    cached = _CACHE.get(key)
    if cached is not None:
        return cached

    try:
        asyncio.get_running_loop()
        inside_event_loop = True
    except RuntimeError:
        inside_event_loop = False

    # Never block an active event loop with subprocess-based detection. Server
    # startup primes the cache asynchronously in env mode; request-scoped HTTP
    # deployments should set GH_PROJECT_OWNER_TYPE explicitly when they cannot
    # prime the target owner.
    detected = None if inside_event_loop else _detect_via_gh(login)
    if detected is not None:
        logger.info("Auto-detected owner type for '%s': %s", login, detected)
        _CACHE[key] = detected
        return detected

    logger.warning(
        "Could not auto-detect owner type for '%s'; defaulting to '%s'. "
        "Set GH_PROJECT_OWNER_TYPE=user for a personal (user-owned) board.",
        login,
        default,
    )
    return default


async def resolve_owner_type_via_graphql(
    login: str,
    graphql_client: object,
    *,
    default: OwnerType = "organization",
) -> OwnerType:
    """Resolve an owner's type using an already-authenticated GraphQL client.

    This is the async counterpart to :func:`resolve_owner_type_sync` for
    callers that already hold a :class:`GraphQLClient` (and therefore a
    resolved token). Results share the same process-lifetime cache.

    Args:
        login: The GitHub owner login.
        graphql_client: An object exposing ``execute(query, variables)``.
        default: Owner type to assume when detection fails.

    Returns:
        ``"user"`` or ``"organization"``.
    """
    if not login:
        return default

    key = login.lower()
    cached = _CACHE.get(key)
    if cached is not None:
        return cached

    try:
        response = await graphql_client.execute(  # type: ignore[attr-defined]
            _TYPENAME_QUERY,
            {"login": login},
        )
    except Exception as exc:  # noqa: BLE001 - detection must never hard-fail
        logger.warning(
            "GraphQL owner-type detection for '%s' failed (%s); defaulting to '%s'.",
            login,
            type(exc).__name__,
            default,
        )
        return default

    typename = (
        (response or {})
        .get("data", {})
        .get("repositoryOwner", {})
        or {}
    ).get("__typename", "")
    detected = _typename_to_owner_type(typename)
    if detected is not None:
        logger.info("Auto-detected owner type for '%s': %s", login, detected)
        _CACHE[key] = detected
        return detected

    logger.warning(
        "Owner '%s' not resolvable via GraphQL; defaulting to '%s'.",
        login,
        default,
    )
    return default


async def prime_owner_type_cache(login: str, token: str) -> OwnerType | None:
    """Resolve and cache an owner type asynchronously during server startup.

    Returns the detected type, or ``None`` when detection is not possible.
    The token is used only for this request and is never logged.
    """
    if not login or login.lower() in _CACHE:
        return _CACHE.get(login.lower())
    from clients.graphql_client import GraphQLClient

    client = GraphQLClient(token=token)
    detected = await resolve_owner_type_via_graphql(login, client, default="organization")
    return detected if login.lower() in _CACHE else None


def clear_cache() -> None:
    """Clear the process-lifetime owner-type cache (used by tests)."""
    _CACHE.clear()
