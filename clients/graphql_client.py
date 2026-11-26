"""GraphQL client for the GitHub Projects V2 API.

Provides async HTTP communication with the GitHub GraphQL endpoint
using httpx. Handles authentication, timeouts, rate-limit detection,
retry logic, and structured error propagation.

Security: The token value is NEVER logged or included in error messages.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import threading
from datetime import UTC, datetime

import httpx

from core.circuit_breaker import CircuitOpenError, github_circuit_breaker
from core.config import get_settings
from core.exceptions import (
    AuthenticationError,
    GraphQLError,
    RateLimitError,
    TimeoutError,
)
from core.hardening import bounded_text
from core.metrics import record_rate_limit
from models.context import GitHubContext

logger = logging.getLogger(__name__)

GITHUB_GRAPHQL_URL = "https://api.github.com/graphql"
_CLIENTS_LOCK = threading.Lock()
_SHARED_CLIENTS: dict[tuple[asyncio.AbstractEventLoop, str, float], httpx.AsyncClient] = {}


def _token_fingerprint(token: str) -> str:
    """Return a non-reversible cache key fragment for a credential."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _shared_http_client(token: str, timeout: float) -> httpx.AsyncClient:
    """Return a pooled AsyncClient scoped to the event loop and credential.

    Authentication is still sent per request; credential fingerprinting only
    prevents connection reuse across different users in multi-tenant HTTP mode.
    Closed event-loop entries are evicted before lookup so pytest loops and
    short-lived embedding applications do not accumulate clients.
    """
    loop = asyncio.get_running_loop()
    fingerprint = _token_fingerprint(token)
    key = (loop, fingerprint, timeout)
    with _CLIENTS_LOCK:
        for cached_key, cached_client in list(_SHARED_CLIENTS.items()):
            cached_loop = cached_key[0]
            if cached_loop.is_closed() or cached_client.is_closed:
                _SHARED_CLIENTS.pop(cached_key, None)
        client = _SHARED_CLIENTS.get(key)
        if client is None:
            client = httpx.AsyncClient(
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


class GraphQLClient:
    """Async client for GitHub's GraphQL API v4.

    Handles authentication via Bearer token, timeout enforcement,
    rate-limit detection, and retry logic for read operations.

    Args:
        token: GitHub personal access token. Never logged or exposed.
    """

    def __init__(
        self,
        token: str | None = None,
        *,
        context: GitHubContext | None = None,
    ) -> None:
        if token is None and context is None:
            raise ValueError("GraphQLClient requires token or GitHubContext")
        self._token = token
        self._context = context
        settings = get_settings()
        self._timeout = settings.timeout_seconds
        self._retry_delay = settings.retry_delay_seconds
        self._retry_attempts = settings.retry_attempts

    async def execute(self, query: str, variables: dict | None = None) -> dict:
        """Execute a GraphQL query or mutation against the GitHub API.

        Args:
            query: GraphQL query or mutation string.
            variables: Variables to pass with the query.

        Returns:
            Parsed JSON response data (the "data" key from the response).

        Raises:
            AuthenticationError: On HTTP 401 responses.
            RateLimitError: On HTTP 403 with rate-limit headers.
            TimeoutError: When the request exceeds the configured timeout.
            GraphQLError: When the response contains GraphQL-level errors.
        """
        variables = variables or {}
        token = self._token
        if token is None:
            if self._context is None:
                raise ValueError("GraphQLClient has no credential context")
            token = await self._context.resolve_token()
        payload = {"query": query, "variables": variables}
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        breaker = github_circuit_breaker(token)
        try:
            breaker.before_call()
        except CircuitOpenError as exc:
            raise GraphQLError(str(exc), retryable=False, retry_after=exc.retry_after) from exc

        try:
            client = _shared_http_client(token, self._timeout)
            response = await client.post(
                GITHUB_GRAPHQL_URL,
                json=payload,
                headers=headers,
            )
        except httpx.TimeoutException as exc:
            breaker.record_failure()
            raise TimeoutError(
                f"Request timed out after {self._timeout}s"
            ) from exc
        except httpx.RequestError as exc:
            breaker.record_failure()
            raise GraphQLError(
                f"GitHub API request failed: {bounded_text(exc)}"
            ) from exc

        request_id = response.headers.get("X-Request-Id")

        # GitHub can return transient gateway failures without GraphQL errors.
        if response.status_code in {408, 425, 429} or response.status_code >= 500:
            breaker.record_failure()
            if response.status_code == 429:
                record_rate_limit("github_api")
            retry_after_header = response.headers.get("Retry-After")
            retry_after: float | None = None
            if retry_after_header:
                try:
                    retry_after = max(0.0, float(retry_after_header))
                except ValueError:
                    retry_after = None
            detail = f" HTTP {response.status_code}."
            if retry_after_header:
                detail += f" Retry-After: {retry_after_header}s."
            raise GraphQLError(
                f"GitHub API is temporarily unavailable.{detail}",
                request_id=request_id,
                retryable=True,
                retry_after=retry_after,
            )

        breaker.record_success()

        # Handle authentication errors
        if response.status_code == 401:
            raise AuthenticationError(
                "Authentication failed. The provided token is invalid or expired. "
                "Verify GITHUB_TOKEN or GH_TOKEN environment variable.",
                request_id=request_id,
            )

        # Handle rate-limit errors
        if response.status_code == 403:
            if (
                response.headers.get("X-RateLimit-Remaining") == "0"
                or response.headers.get("Retry-After")
            ):
                record_rate_limit("github_api")
            remaining = response.headers.get("X-RateLimit-Remaining")
            try:
                remaining_count = int(remaining) if remaining is not None else None
            except ValueError:
                remaining_count = None
            if remaining_count == 0:
                reset_timestamp = response.headers.get("X-RateLimit-Reset", "")
                reset_at = _format_reset_timestamp(reset_timestamp)
                raise RateLimitError(
                    f"GitHub API rate limit exceeded. Resets at {reset_at}.",
                    reset_at=reset_at,
                    remaining=0,
                    request_id=request_id,
                )
            # 403 without rate-limit headers is a permission/auth issue
            raise AuthenticationError(
                "Access denied. The provided token lacks required permissions. "
                "Ensure the token has repo, project, and read:org scopes.",
                request_id=request_id,
            )

        # Parse response body
        try:
            body = response.json()
        except Exception as exc:
            raise GraphQLError(
                f"Failed to parse API response (HTTP {response.status_code})",
                request_id=request_id,
            ) from exc

        # Handle GraphQL-level errors
        if "errors" in body:
            errors = body["errors"]
            if not isinstance(errors, list):
                errors = []
            message = (
                errors[0].get("message", "Unknown GraphQL error")
                if errors and isinstance(errors[0], dict)
                else "Unknown GraphQL error"
            )
            raise GraphQLError(
                f"GraphQL error: {bounded_text(message)}",
                errors=errors,
                request_id=request_id,
            )

        return {"data": body.get("data", {}), "request_id": request_id}

    async def execute_with_retry(
        self,
        query: str,
        variables: dict | None = None,
        *,
        is_mutation: bool = False,
    ) -> dict:
        """Execute a GraphQL operation with conditional retry logic.

        Retry policy:
        - Mutations (is_mutation=True): NEVER retry, return error immediately.
        - Reads: retry transient timeouts and retryable HTTP failures up to the
          configured attempt budget, using exponential backoff, jitter, and
          Retry-After when supplied by GitHub.

        Args:
            query: GraphQL query or mutation string.
            variables: Variables to pass with the query.
            is_mutation: If True, never retry on failure.

        Returns:
            Parsed JSON response data.

        Raises:
            AuthenticationError: On HTTP 401 responses.
            RateLimitError: On HTTP 403 with rate-limit headers.
            TimeoutError: When the request exceeds timeout (after retry for reads).
            GraphQLError: When the response contains GraphQL-level errors.
        """
        if is_mutation:
            try:
                return await self.execute(query, variables)
            except TimeoutError as exc:
                raise TimeoutError(
                    f"Mutation timed out after {self._timeout}s. "
                    "Operation status is unknown. Do not retry mutations."
                ) from exc

        for attempt in range(self._retry_attempts + 1):
            try:
                return await self.execute(query, variables)
            except (TimeoutError, GraphQLError) as exc:
                retryable = isinstance(exc, TimeoutError) or exc.retryable
                if not retryable or attempt >= self._retry_attempts:
                    raise
                backoff = self._retry_delay * (2**attempt)
                retry_after = getattr(exc, "retry_after", None)
                if retry_after is not None:
                    delay = max(backoff, retry_after)
                else:
                    delay = backoff + random.uniform(0.0, min(1.0, max(backoff, 0.1) * 0.25))
                logger.info(
                    "Retryable read failure (%s); retry %d/%d after %.2fs",
                    type(exc).__name__,
                    attempt + 1,
                    self._retry_attempts,
                    delay,
                )
                await asyncio.sleep(delay)

        raise AssertionError("GraphQL retry loop completed without a result")


def _format_reset_timestamp(reset_unix: str) -> str:
    """Convert a Unix timestamp string to ISO 8601 format.

    Args:
        reset_unix: Unix timestamp as string from X-RateLimit-Reset header.

    Returns:
        ISO 8601 formatted datetime string, or the raw value if parsing fails.
    """
    try:
        reset_dt = datetime.fromtimestamp(int(reset_unix), tz=UTC)
        return reset_dt.isoformat()
    except (ValueError, TypeError, OSError):
        return reset_unix
