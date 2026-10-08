"""HTTP middleware for request-scoped bearer authentication."""

from __future__ import annotations

import hmac
from collections.abc import Awaitable, Callable
from typing import Any

from starlette.responses import JSONResponse

from core.auth import request_token_scope

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
    ) -> None:
        self.app = app
        self.protected_path = protected_path
        self.client_key = client_key
        self.allowed_prefixes = allowed_prefixes

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
        if self.client_key and not hmac.compare_digest(
            self.client_key,
            headers.get(b"x-mcp-client-key", b"").decode("utf-8", errors="replace"),
        ):
            return await self._unauthorized(scope, receive, send)

        token = self._bearer_token(headers.get(b"authorization", b""))
        if token is None:
            return await self._unauthorized(scope, receive, send)
        if self.allowed_prefixes and not token.startswith(self.allowed_prefixes):
            return await self._unauthorized(scope, receive, send)

        with request_token_scope(token):
            return await self.app(scope, receive, send)

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
