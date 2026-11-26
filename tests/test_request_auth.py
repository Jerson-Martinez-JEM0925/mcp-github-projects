"""Contract tests for request-scoped bearer authentication."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from clients.gh_cli_client import CLIError, _safe_command  # noqa: E402
from core.auth import request_token_scope, resolve_token  # noqa: E402
from core.config import GitHubProjectSettings, get_settings  # noqa: E402
from core.http_auth import RequestBearerAuthMiddleware  # noqa: E402


def _configure_target(monkeypatch: pytest.MonkeyPatch, **extra: str) -> None:
    """Set the minimum target configuration and clear cached settings."""
    monkeypatch.setenv("GH_PROJECT_ORG_NAME", "ExampleOrg")
    monkeypatch.setenv("GH_PROJECT_REPO_NAME", "ExampleRepo")
    monkeypatch.setenv("GH_PROJECT_PROJECT_NUMBER", "1")
    monkeypatch.setenv("GH_PROJECT_OWNER_TYPE", "organization")
    for key in (
        "MCP_TRANSPORT",
        "MCP_AUTH_MODE",
        "MCP_ALLOW_SHARED_TOKEN",
        "MCP_CLIENT_KEY",
        "MCP_ALLOWED_TOKEN_PREFIXES",
    ):
        monkeypatch.delenv(key, raising=False)
    for key, value in extra.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()


def test_auth_mode_defaults_and_shared_token_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stdio keeps env auth while HTTP defaults to request auth and fails closed."""
    _configure_target(monkeypatch)
    assert GitHubProjectSettings().auth_mode == "env"

    monkeypatch.setenv("MCP_TRANSPORT", "streamable-http")
    settings = GitHubProjectSettings()
    assert settings.auth_mode == "request"
    assert settings.token_prefixes() == ("ghu_", "github_pat_")

    monkeypatch.setenv("MCP_AUTH_MODE", "env")
    with pytest.raises(ValueError, match="MCP_ALLOW_SHARED_TOKEN=true"):
        GitHubProjectSettings()

    monkeypatch.setenv("MCP_ALLOW_SHARED_TOKEN", "true")
    assert GitHubProjectSettings().auth_mode == "env"


def _auth_app() -> Starlette:
    """Build a minimal protected app whose handler reads the active token."""
    async def echo_token(_request):
        token = await resolve_token()
        return JSONResponse({"token": token})

    app = Starlette(routes=[Route("/mcp", echo_token, methods=["POST"])])
    app.add_middleware(
        RequestBearerAuthMiddleware,
        protected_path="/mcp",
        client_key="client-secret",
        allowed_prefixes=("ghu_", "github_pat_"),
    )
    return app


def test_request_auth_rejects_missing_malformed_key_and_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every auth failure has the same token-free 401 contract."""
    _configure_target(monkeypatch, MCP_TRANSPORT="streamable-http")
    app = _auth_app()

    async def exercise() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            cases = [
                {},
                {"Authorization": "Basic abc"},
                {"Authorization": "Bearer ghp_classic", "X-MCP-Client-Key": "client-secret"},
                {"Authorization": "Bearer ghu_valid"},
            ]
            for headers in cases:
                response = await client.post("/mcp", headers=headers)
                assert response.status_code == 401
                assert response.headers["WWW-Authenticate"] == "Bearer"
                assert response.json() == {"error": "unauthorized"}
                assert "ghp_classic" not in response.text

    asyncio.run(exercise())


def test_request_auth_isolated_for_concurrent_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    """Concurrent requests keep their own bearer token context."""
    _configure_target(monkeypatch, MCP_TRANSPORT="streamable-http")
    app = _auth_app()

    async def exercise() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            async def call(token: str) -> str:
                response = await client.post(
                    "/mcp",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "X-MCP-Client-Key": "client-secret",
                    },
                )
                assert response.status_code == 200
                return response.json()["token"]

            result = await asyncio.gather(call("ghu_first"), call("github_pat_second"))
            assert result == ["ghu_first", "github_pat_second"]

    asyncio.run(exercise())


def test_request_mode_never_falls_back_to_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Global GitHub credentials cannot satisfy an unbound request-mode call."""
    _configure_target(monkeypatch, MCP_TRANSPORT="streamable-http")
    monkeypatch.setenv("GITHUB_TOKEN", "ghu_global")
    monkeypatch.setenv("GH_TOKEN", "ghu_fallback")
    with pytest.raises(ValueError, match="request credential") as exc_info:
        asyncio.run(resolve_token())
    assert "ghu_global" not in str(exc_info.value)
    assert "ghu_fallback" not in str(exc_info.value)


def test_request_token_is_redacted_from_commands_and_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """The active bearer is included in centralized diagnostic redaction."""
    _configure_target(monkeypatch, MCP_TRANSPORT="streamable-http")
    token = "ghu_secret_value"
    with request_token_scope(token):
        assert token not in _safe_command(["api", token])
        error = CLIError(f"command failed with {token}", 1, f"stderr: {token}")
        assert token not in str(error)
        assert token not in error.stderr


def test_empty_prefix_override_disables_default_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit empty prefix list is supported for non-GitHub token formats."""
    _configure_target(
        monkeypatch,
        MCP_TRANSPORT="streamable-http",
        MCP_ALLOWED_TOKEN_PREFIXES="",
    )
    assert GitHubProjectSettings().token_prefixes() == ()


def test_http_bearer_validation_is_cached_by_fingerprint(monkeypatch: pytest.MonkeyPatch) -> None:
    """Validated HTTP metadata exposure does not re-call GitHub for every request."""
    _configure_target(monkeypatch, MCP_TRANSPORT="streamable-http")
    from clients.rest_client import GitHubRESTClient

    calls: list[str] = []

    async def fake_request(self, method: str, path: str, **kwargs):
        calls.append(path)
        return {"login": "octocat"}

    monkeypatch.setattr(GitHubRESTClient, "request", fake_request)

    async def echo_token(_request):
        token = await resolve_token()
        return JSONResponse({"token": token})

    app = Starlette(routes=[Route("/mcp", echo_token, methods=["POST"])])
    app.add_middleware(
        RequestBearerAuthMiddleware,
        protected_path="/mcp",
        client_key="client-secret",
        allowed_prefixes=("ghu_",),
        validate_tokens=True,
        auth_cache_ttl_seconds=60,
    )

    async def exercise() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            headers = {
                "Authorization": "Bearer ghu_validated",
                "X-MCP-Client-Key": "client-secret",
            }
            first = await client.post("/mcp", headers=headers)
            second = await client.post("/mcp", headers=headers)
            assert first.status_code == second.status_code == 200
            assert first.json()["token"] == "ghu_validated"

    asyncio.run(exercise())
    assert calls == ["/user"]


def test_http_bearer_validation_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A token rejected by GitHub cannot enumerate MCP metadata."""
    _configure_target(monkeypatch, MCP_TRANSPORT="streamable-http")
    from clients.rest_client import GitHubRESTClient, RESTError

    async def fake_request(self, method: str, path: str, **kwargs):
        raise RESTError("bad credentials", status_code=401)

    monkeypatch.setattr(GitHubRESTClient, "request", fake_request)

    async def echo_token(_request):
        return JSONResponse({"token": "unreachable"})

    app = Starlette(routes=[Route("/mcp", echo_token, methods=["POST"])])
    app.add_middleware(
        RequestBearerAuthMiddleware,
        protected_path="/mcp",
        client_key="client-secret",
        allowed_prefixes=("ghu_",),
        validate_tokens=True,
    )

    async def exercise() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            response = await client.post(
                "/mcp",
                headers={
                    "Authorization": "Bearer ghu_invalid",
                    "X-MCP-Client-Key": "client-secret",
                },
            )
            assert response.status_code == 401
            assert response.json() == {"error": "unauthorized"}

    asyncio.run(exercise())


def test_http_auto_owner_type_is_primed_with_request_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """HTTP request auth supports owner-type auto-detection without process env auth."""
    _configure_target(monkeypatch, MCP_TRANSPORT="streamable-http")
    from services import owner_type_resolver

    calls: list[tuple[str, str]] = []

    async def fake_prime(login: str, token: str):
        calls.append((login, token))
        return "organization"

    monkeypatch.setattr(owner_type_resolver, "prime_owner_type_cache", fake_prime)

    async def echo_token(_request):
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/mcp", echo_token, methods=["POST"])])
    app.add_middleware(
        RequestBearerAuthMiddleware,
        protected_path="/mcp",
        client_key="client-secret",
        allowed_prefixes=("ghu_",),
        owner_login="ExampleOrg",
    )

    async def exercise() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            response = await client.post(
                "/mcp",
                headers={
                    "Authorization": "Bearer ghu_valid",
                    "X-MCP-Client-Key": "client-secret",
                },
            )
            assert response.status_code == 200

    asyncio.run(exercise())
    assert calls == [("ExampleOrg", "ghu_valid")]


def test_http_tls_requirement_allows_loopback_and_proxy_https_only() -> None:
    """Optional TLS enforcement rejects plaintext non-loopback remote deployment."""
    async def echo(_request):
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/mcp", echo, methods=["POST"])])
    middleware = RequestBearerAuthMiddleware(app, protected_path="/mcp", require_tls=True)

    assert middleware._tls_or_loopback({"scheme": "https", "client": ("10.0.0.1", 1)}, {})
    assert middleware._tls_or_loopback({"scheme": "http", "client": ("127.0.0.1", 1)}, {})
    assert middleware._tls_or_loopback(
        {"scheme": "http", "client": ("10.0.0.1", 1)},
        {b"x-forwarded-proto": b"https,http"},
    )
    assert not middleware._tls_or_loopback({"scheme": "http", "client": ("10.0.0.1", 1)}, {})
