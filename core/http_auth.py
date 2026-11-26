"""HTTP middleware for request-scoped bearer authentication."""

from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Awaitable, Callable
from typing import Any

from starlette.responses import JSONResponse

from core.auth import request_token_scope
from core.metrics import record_rate_limit

ASGIApp = Callable[[dict[str, Any], Callable[..., Awaitable[Any]], Callable[..., Awaitable[Any]]], Awaitable[Any]]


class RequestBearerAuthMiddleware:
    """Require and bind one bearer credential for every MCP HTTP request.

    Health endpoints are intentionally outside the protected MCP path so
    liveness/readiness probes do not need credentials. The bearer value is
    never copied to process environment or included in an HTTP error.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        protected_path: str,
        client_key: str = "",
        allowed_prefixes: tuple[str, ...] = (),
        max_body_bytes: int = 1_000_000,
        rate_limit_per_minute: int = 240,
        validate_tokens: bool = False,
        auth_cache_ttl_seconds: int = 60,
        owner_login: str = "",
        require_tls: bool = False,
    ) -> None:
        self.app = app
        self.protected_path = protected_path
        self.client_key = client_key
        self.allowed_prefixes = allowed_prefixes
        self.max_body_bytes = max_body_bytes
        self.rate_limit_per_minute = rate_limit_per_minute
        self.validate_tokens = validate_tokens
        self.auth_cache_ttl_seconds = auth_cache_ttl_seconds
        self.owner_login = owner_login
        self.require_tls = require_tls
        self._rate_windows: dict[str, tuple[float, int]] = {}
        self._token_validation: dict[str, tuple[float, bool]] = {}

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[..., Awaitable[Any]],
        send: Callable[..., Awaitable[Any]],
    ) -> Any:
        """Authenticate the protected HTTP path and run it in token scope."""
        if scope.get("type") != "http" or scope.get("path") != self.protected_path:
            return await self.app(scope, receive, send)

        headers = {
            key.lower(): value
            for key, value in scope.get("headers", [])
        }
        if self.require_tls and not self._tls_or_loopback(scope, headers):
            return await self._json_error(scope, receive, send, 403, "tls_required")

        content_length = headers.get(b"content-length", b"").decode("ascii", errors="ignore")
        if content_length.isdigit() and int(content_length) > self.max_body_bytes:
            return await self._json_error(scope, receive, send, 413, "payload_too_large")

        token = self._bearer_token(headers.get(b"authorization", b""))
        client_host = ""
        client = scope.get("client")
        if isinstance(client, (tuple, list)) and client:
            client_host = str(client[0])
        rate_key = client_host or "unknown"
        if token:
            rate_key = "token:" + hashlib.sha256(token.encode("utf-8")).hexdigest()
        if not self._consume_rate_budget(rate_key):
            record_rate_limit("http_client")
            return await self._json_error(scope, receive, send, 429, "rate_limited")

        if self.client_key and not hmac.compare_digest(
            self.client_key,
            headers.get(b"x-mcp-client-key", b"").decode("utf-8", errors="replace"),
        ):
            return await self._unauthorized(scope, receive, send)

        if token is None:
            return await self._unauthorized(scope, receive, send)
        if self.allowed_prefixes and not token.startswith(self.allowed_prefixes):
            return await self._unauthorized(scope, receive, send)
        if self.validate_tokens and not await self._token_is_valid(token):
            return await self._unauthorized(scope, receive, send)
        if self.owner_login:
            try:
                from services.owner_type_resolver import prime_owner_type_cache

                await prime_owner_type_cache(self.owner_login, token)
            except Exception:  # noqa: BLE001 - owner detection remains best-effort
                pass

        with request_token_scope(token):
            return await self.app(scope, receive, send)

    async def _token_is_valid(self, token: str) -> bool:
        """Validate a bearer with GitHub, caching only its fingerprint and verdict.

        Positive results are cached for the configured TTL. Negative results use
        a shorter TTL so a newly provisioned token or transient GitHub failure
        does not remain pinned for the full positive-cache window. The token
        itself is never used as a dictionary key or logged.
        """
        fingerprint = hashlib.sha256(token.encode("utf-8")).hexdigest()
        now = time.monotonic()
        cached = self._token_validation.get(fingerprint)
        if cached is not None and now < cached[0]:
            return cached[1]

        valid = False
        try:
            from clients.rest_client import GitHubRESTClient

            payload = await GitHubRESTClient(token=token).request("GET", "/user")
            valid = isinstance(payload, dict) and bool(payload.get("login"))
        except Exception:  # noqa: BLE001 - fail closed without exposing detail
            valid = False

        ttl = self.auth_cache_ttl_seconds if valid else min(10, self.auth_cache_ttl_seconds)
        self._token_validation[fingerprint] = (now + ttl, valid)
        if len(self._token_validation) > 10_000:
            self._token_validation = {
                key: value for key, value in self._token_validation.items()
                if value[0] > now
            }
        return valid

    def _tls_or_loopback(self, scope: dict[str, Any], headers: dict[bytes, bytes]) -> bool:
        """Return true for TLS, a trusted TLS proxy signal, or loopback development."""
        if str(scope.get("scheme", "")).lower() == "https":
            return True
        forwarded = headers.get(b"x-forwarded-proto", b"").decode("ascii", errors="ignore")
        first_hop = forwarded.split(",", 1)[0].strip().lower()
        if first_hop == "https":
            return True
        client = scope.get("client")
        host = str(client[0]) if isinstance(client, (tuple, list)) and client else ""
        return host in {"127.0.0.1", "::1", "localhost"}

    def _consume_rate_budget(self, key: str) -> bool:
        """Consume one fixed-window request slot for a client/token fingerprint."""
        now = time.monotonic()
        if len(self._rate_windows) > 10_000:
            self._rate_windows = {
                name: window for name, window in self._rate_windows.items()
                if now - window[0] < 60.0
            }
        started, count = self._rate_windows.get(key, (now, 0))
        if now - started >= 60.0:
            started, count = now, 0
        if count >= self.rate_limit_per_minute:
            self._rate_windows[key] = (started, count)
            return False
        self._rate_windows[key] = (started, count + 1)
        return True

    @staticmethod
    async def _json_error(
        scope: dict[str, Any],
        receive: Callable[..., Awaitable[Any]],
        send: Callable[..., Awaitable[Any]],
        status_code: int,
        error: str,
    ) -> None:
        """Return a small token-free JSON error response."""
        response = JSONResponse({"error": error}, status_code=status_code)
        await response(scope, receive, send)

    @staticmethod
    def _bearer_token(raw_header: bytes) -> str | None:
        """Extract a non-empty bearer token without retaining malformed input."""
        try:
            parts = raw_header.decode("ascii").split()
        except UnicodeDecodeError:
            return None
        if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1]:
            return None
        return parts[1]

    @staticmethod
    async def _unauthorized(
        scope: dict[str, Any],
        receive: Callable[..., Awaitable[Any]],
        send: Callable[..., Awaitable[Any]],
    ) -> None:
        """Return the same generic 401 response for every auth failure."""
        response = JSONResponse(
            {"error": "unauthorized"},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )
        await response(scope, receive, send)


__all__ = ["RequestBearerAuthMiddleware"]
