"""Small in-process cache for stable repository resources.

Labels, milestones and workflow definitions are read frequently and mutate
rarely compared with issue/project-item state. This cache avoids repeated
network calls during interactive MCP sessions while remaining conservative:
entries are credential-scoped, short-lived, and explicitly invalidated by the
mutation tools in this repository.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

T = TypeVar("T")

_ENTRIES: dict[tuple[str, str, str], tuple[float, Any]] = {}
_LOCKS: dict[tuple[str, str, str], asyncio.Lock] = {}
_GUARD = asyncio.Lock()


def credential_fingerprint(token: str) -> str:
    """Return a secret-free cache key fragment for a credential."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def cache_key(kind: str, target: str, token: str, discriminator: str = "") -> tuple[str, str, str]:
    """Build a process-local key that cannot leak credentials or targets."""
    identity = hashlib.sha256(f"{target}\0{discriminator}".encode("utf-8")).hexdigest()
    return (kind, credential_fingerprint(token), identity)


async def get_or_load(
    key: tuple[str, str, str],
    *,
    ttl_seconds: float,
    loader: Callable[[], Awaitable[T]],
) -> tuple[T, bool]:
    """Return a cached value or load it once per key.

    The boolean is ``True`` when the returned value came from cache. Locks are
    per key, so different repositories/resources can refresh concurrently
    without duplicating simultaneous refreshes of the same resource.
    """
    now = time.monotonic()
    entry = _ENTRIES.get(key)
    if entry is not None and now - entry[0] < ttl_seconds:
        return entry[1], True

    async with _GUARD:
        lock = _LOCKS.setdefault(key, asyncio.Lock())
    async with lock:
        now = time.monotonic()
        entry = _ENTRIES.get(key)
        if entry is not None and now - entry[0] < ttl_seconds:
            return entry[1], True
        value = await loader()
        _ENTRIES[key] = (time.monotonic(), value)
        return value, False


def invalidate(kind: str | None = None, target: str | None = None) -> None:
    """Invalidate entries by resource kind and/or target text.

    Targets are hashed into cache keys, so exact target-aware invalidation is
    not possible without retaining sensitive target text. The cache is small;
    clearing a resource kind after a mutation is cheap and safer than stale
    reads. The ``target`` argument is accepted for forward-compatible callers.
    """
    del target  # kind-wide invalidation is intentionally conservative today
    for key in list(_ENTRIES):
        if kind is None or key[0] == kind:
            _ENTRIES.pop(key, None)


def clear() -> None:
    """Clear all resource cache entries (used by tests)."""
    _ENTRIES.clear()
    _LOCKS.clear()


async def scoped_key(factory: Any, kind: str, target: str, discriminator: str = "") -> tuple[str, str, str] | None:
    """Build a credential-scoped key, returning None for injected fake transports."""
    try:
        token = await factory.token()
    except (Exception, SystemExit):  # noqa: BLE001 - injected transports need no credential
        return None
    return cache_key(kind, target, token, discriminator)


__all__ = ["cache_key", "clear", "credential_fingerprint", "get_or_load", "invalidate", "scoped_key"]
