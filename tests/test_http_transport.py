"""Contract tests for the stateless Streamable HTTP transport."""

from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.config import GitHubProjectSettings, get_settings  # noqa: E402


def _configure_target(monkeypatch: pytest.MonkeyPatch, **extra: str) -> None:
    """Set the minimum target configuration and clear cached settings."""
    monkeypatch.setenv("GH_PROJECT_ORG_NAME", "ExampleOrg")
    monkeypatch.setenv("GH_PROJECT_REPO_NAME", "ExampleRepo")
    monkeypatch.setenv("GH_PROJECT_PROJECT_NUMBER", "1")
    monkeypatch.delenv("MCP_TRANSPORT", raising=False)
    for key, value in extra.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()


def test_http_transport_defaults_and_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default is unchanged stdio and HTTP settings have documented defaults."""
    _configure_target(monkeypatch)
    settings = GitHubProjectSettings()
    assert settings.transport == "stdio"
    assert settings.http_host == "127.0.0.1"
    assert settings.http_port == 8080
    assert settings.http_path == "/mcp"

    monkeypatch.setenv("MCP_TRANSPORT", "invalid")
    with pytest.raises(ValueError, match="MCP_TRANSPORT must be 'stdio' or 'streamable-http'"):
        GitHubProjectSettings()


def test_stdio_main_remains_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default startup still selects FastMCP's existing stdio runner."""
    _configure_target(monkeypatch)
    server = importlib.import_module("server")
    server = importlib.reload(server)
    with (
        patch.object(server, "_validate_auth_on_startup", new=AsyncMock()),
        patch.object(server.mcp, "run") as run,
        patch.object(server, "_serve_http", new=AsyncMock()) as serve_http,
    ):
        server.main()
    run.assert_called_once_with(transport="stdio")
    serve_http.assert_not_awaited()


def test_http_app_health_and_mcp_protocol(monkeypatch: pytest.MonkeyPatch) -> None:
    """Health probes and initialize/tools/list work over stateless HTTP."""
    _configure_target(monkeypatch, MCP_TRANSPORT="streamable-http")
    server = importlib.import_module("server")
    server = importlib.reload(server)
    app = server.create_http_app()

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=app)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                for path in ("/healthz", "/readyz"):
                    response = await client.get(path)
                    assert response.status_code == 200
                    assert response.json() == {"status": "ok"}

                headers = {
                    "Accept": "application/json, text/event-stream",
                    "Content-Type": "application/json",
                    "Authorization": "Bearer ghu_contract_test",
                }
                initialize = await client.post(
                    "/mcp",
                    headers=headers,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2025-06-18",
                            "capabilities": {},
                            "clientInfo": {"name": "contract-test", "version": "1.0"},
                        },
                    },
                )
                assert initialize.status_code == 200
                assert initialize.json()["result"]["serverInfo"]["name"] == "github-project-management"

                listed = await client.post(
                    "/mcp",
                    headers=headers,
                    json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                )
                assert listed.status_code == 200
                assert listed.json()["result"]["tools"]

    asyncio.run(exercise())
