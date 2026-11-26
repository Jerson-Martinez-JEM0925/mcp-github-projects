"""Pooled async REST client for high-frequency GitHub API operations.

The MCP historically proxied most REST operations through the ``gh`` CLI.
That is flexible, but every call pays process creation, authentication and
TLS setup costs. This client provides a small, credential-isolated HTTP
transport used as a fast path by ``GHCLIClient`` for commands whose exact CLI
semantics are implemented locally. Less common operations still use ``gh``.
"""

from __future__ import annotations

import asyncio
import hashlib
import threading
from typing import Any

import httpx

from core.circuit_breaker import CircuitOpenError, github_circuit_breaker
from core.config import get_settings
from core.hardening import bounded_text
from core.metrics import record_rate_limit
from models.context import GitHubContext

_GITHUB_API_URL = "https://api.github.com"
_CLIENTS_LOCK = threading.Lock()
_SHARED_CLIENTS: dict[tuple[asyncio.AbstractEventLoop, str, float], httpx.AsyncClient] = {}


class RESTError(Exception):
    """Raised when a direct GitHub REST request fails."""

    def __init__(self, message: str, *, status_code: int = 0, retry_after: float | None = None) -> None:
        super().__init__(bounded_text(message))
        self.status_code = status_code
        self.retry_after = retry_after


def _fingerprint(token: str) -> str:
    """Return a non-reversible cache key fragment for a credential."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _shared_client(token: str, timeout: float) -> httpx.AsyncClient:
    """Return a pooled client scoped to the current loop and credential."""
    loop = asyncio.get_running_loop()
    key = (loop, _fingerprint(token), timeout)
    with _CLIENTS_LOCK:
        for cached_key, cached_client in list(_SHARED_CLIENTS.items()):
            if cached_key[0].is_closed() or cached_client.is_closed:
                _SHARED_CLIENTS.pop(cached_key, None)
        client = _SHARED_CLIENTS.get(key)
        if client is None:
            client = httpx.AsyncClient(
                base_url=_GITHUB_API_URL,
                timeout=timeout,
                limits=httpx.Limits(
                    max_connections=20,
                    max_keepalive_connections=10,
                    keepalive_expiry=30.0,
                ),
                follow_redirects=False,
            )
            _SHARED_CLIENTS[key] = client
        return client


class GitHubRESTClient:
    """Minimal async GitHub REST client with connection pooling."""

    def __init__(self, *, token: str | None = None, context: GitHubContext | None = None) -> None:
        if token is None and context is None:
            raise ValueError("GitHubRESTClient requires token or GitHubContext")
        self._token = token
        self._context = context

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        """Execute one REST request and return decoded JSON or text."""
        token = self._token
        if token is None:
            if self._context is None:
                raise RESTError("GitHubRESTClient has no credential context")
            token = await self._context.resolve_token()
        settings = get_settings()
        client = _shared_client(token, settings.timeout_seconds)
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        breaker = github_circuit_breaker(token)
        try:
            breaker.before_call()
        except CircuitOpenError as exc:
            raise RESTError(str(exc), retry_after=exc.retry_after) from exc
        try:
            response = await client.request(
                method.upper(),
                path,
                params=params,
                json=json_body,
                headers=headers,
            )
        except httpx.TimeoutException as exc:
            breaker.record_failure()
            raise RESTError(f"GitHub REST request timed out after {settings.timeout_seconds}s") from exc
        except httpx.RequestError as exc:
            breaker.record_failure()
            raise RESTError(f"GitHub REST request failed: {bounded_text(exc)}") from exc

        retry_after: float | None = None
        if response.headers.get("Retry-After"):
            try:
                retry_after = max(0.0, float(response.headers["Retry-After"]))
            except ValueError:
                retry_after = None
        if response.status_code in {408, 425, 429} or response.status_code >= 500:
            breaker.record_failure()
            if response.status_code == 429:
                record_rate_limit("github_api")
        else:
            breaker.record_success()
        if response.status_code == 403 and (
            response.headers.get("X-RateLimit-Remaining") == "0" or response.headers.get("Retry-After")
        ):
            record_rate_limit("github_api")
        if response.status_code >= 400:
            detail = ""
            try:
                payload = response.json()
                if isinstance(payload, dict):
                    detail = str(payload.get("message") or "")
            except ValueError:
                detail = response.text[:200]
            raise RESTError(
                f"GitHub REST HTTP {response.status_code}: {detail or response.reason_phrase}",
                status_code=response.status_code,
                retry_after=retry_after,
            )
        if not response.content:
            return None
        content_type = response.headers.get("content-type", "")
        if "json" in content_type:
            return response.json()
        return response.text


__all__ = ["GitHubRESTClient", "RESTError"]
