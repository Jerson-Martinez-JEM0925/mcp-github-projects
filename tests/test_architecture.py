"""Architecture tests for HARDENING_200 items 6-8 (issue #35).

* item 6 — ServiceFactory is the single construction point for clients and
  services, and tools can be driven by injected fakes without patching;
* item 7 — every tool call runs in its own RequestContext whose correlation ID
  reaches logs and error envelopes;
* item 8 — GraphQLExecutor / GHCLIRunner protocols describe the transports and
  the real clients satisfy them.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import re
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from clients.cache_manager import CacheManager  # noqa: E402
from clients.gh_cli_client import CLIError, CommandResult, GHCLIClient  # noqa: E402
from clients.graphql_client import GraphQLClient  # noqa: E402
from core.config import get_settings  # noqa: E402
from core.context import (  # noqa: E402
    RequestContextFilter,
    current_request,
    request_scope,
    with_request_context,
)
from core.error_handling import build_error_response  # noqa: E402
from core.factory import (  # noqa: E402
    ServiceFactory,
    get_service_factory,
    use_service_factory,
)
from core.protocols import GHCLIRunner, GraphQLExecutor  # noqa: E402
from services.discovery_service import DiscoveryService  # noqa: E402
from services.issue_service import IssueService  # noqa: E402
from services.project_service import ProjectService  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def configured_target(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GH_PROJECT_ORG_NAME", "octo-org")
    monkeypatch.setenv("GH_PROJECT_REPO_NAME", "octo-repo")
    monkeypatch.setenv("GH_PROJECT_PROJECT_NUMBER", "7")
    monkeypatch.delenv("GH_PROJECT_SCOPE_LOCK", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class FakeGH:
    """Minimal GHCLIRunner: records argv, returns canned output or raises."""

    def __init__(self, stdout: str = "", error: CLIError | None = None) -> None:
        self.calls: list[list[str]] = []
        self._stdout = stdout
        self._error = error

    async def run(self, args: list[str]) -> CommandResult:
        self.calls.append(args)
        if self._error is not None:
            raise self._error
        return CommandResult(stdout=self._stdout, stderr="", return_code=0)

    async def api_graphql(self, query: str, variables: dict) -> dict:
        return {}


class FakeGraphQL:
    async def execute(self, query: str, variables: dict | None = None) -> dict:
        return {}

    async def execute_with_retry(self, query, variables=None, *, is_mutation=False) -> dict:
        return {}


# ── Item 8: protocols ───────────────────────────────────────────────────────


def test_real_clients_satisfy_protocols() -> None:
    assert isinstance(GraphQLClient(token="t"), GraphQLExecutor)
    assert isinstance(GHCLIClient(), GHCLIRunner)


def test_fakes_satisfy_protocols() -> None:
    assert isinstance(FakeGraphQL(), GraphQLExecutor)
    assert isinstance(FakeGH(), GHCLIRunner)


def test_protocol_signatures_match_real_clients() -> None:
    """A renamed/removed client method must break this test, not production."""
    pairs = [
        (GraphQLExecutor, GraphQLClient, ["execute", "execute_with_retry"]),
        (GHCLIRunner, GHCLIClient, ["run", "api_graphql"]),
    ]
    for proto, impl, methods in pairs:
        for name in methods:
            assert list(inspect.signature(getattr(proto, name)).parameters) == list(
                inspect.signature(getattr(impl, name)).parameters
            ), f"{impl.__name__}.{name} drifted from {proto.__name__}"


# ── Item 6: ServiceFactory ──────────────────────────────────────────────────


@pytest.mark.anyio
async def test_default_factory_builds_real_objects_with_resolved_token() -> None:
    factory = ServiceFactory(token_resolver=AsyncMock(return_value="tok"))
    graphql = await factory.graphql()
    assert isinstance(graphql, GraphQLClient)
    assert isinstance(factory.gh(), GHCLIClient)
    assert isinstance(factory.cache_manager(), CacheManager)
    assert isinstance(await factory.discovery_service(), DiscoveryService)
    assert isinstance(await factory.project_service(), ProjectService)
    assert isinstance(factory.issue_service(), IssueService)


@pytest.mark.anyio
async def test_default_factory_resolves_token_via_core_auth() -> None:
    with patch("core.auth.resolve_token", new=AsyncMock(return_value="tok")) as resolver:
        await ServiceFactory().graphql()
        await ServiceFactory().ensure_auth()
    assert resolver.await_count == 2


@pytest.mark.anyio
async def test_overrides_skip_token_resolution() -> None:
    resolver = AsyncMock(side_effect=AssertionError("token must not be resolved"))
    gql, gh = FakeGraphQL(), FakeGH()
    factory = ServiceFactory(graphql_client=gql, gh_client=gh, token_resolver=resolver)
    assert await factory.graphql() is gql
    assert factory.gh() is gh
    await factory.ensure_auth()
    resolver.assert_not_awaited()


def test_use_service_factory_restores_previous() -> None:
    before = get_service_factory()
    with use_service_factory(gh_client=FakeGH()) as installed:
        assert get_service_factory() is installed
    assert get_service_factory() is before


@pytest.mark.anyio
async def test_tool_runs_against_injected_fake_without_patching() -> None:
    from tools.issues.close import CloseIssueInput, close_issue

    gh = FakeGH()
    with use_service_factory(gh_client=gh):
        result = await close_issue(CloseIssueInput(issue_number=12))
    assert result["ok"] is True
    assert gh.calls == [["issue", "close", "12", "--repo", "octo-org/octo-repo"]]


def test_tools_do_not_construct_clients_or_services_inline() -> None:
    """Guard for item 6: construction goes through core.factory only."""
    forbidden = re.compile(
        r"\b(GraphQLClient|GHCLIClient|CacheManager|DiscoveryService|ProjectService|"
        r"IssueService|FieldService)\("
    )
    offenders = [
        f"{path.relative_to(ROOT)}:{lineno}"
        for path in (ROOT / "tools").rglob("*.py")
        for lineno, line in enumerate(path.read_text().splitlines(), 1)
        if forbidden.search(line)
    ]
    assert offenders == [], f"build clients via get_service_factory(): {offenders}"


# ── Item 7: RequestContext ──────────────────────────────────────────────────


def test_request_scope_binds_and_resets() -> None:
    assert current_request() is None
    with request_scope("list_labels") as ctx:
        assert current_request() is ctx
        assert ctx.tool == "list_labels"
        assert re.fullmatch(r"[0-9a-f]{12}", ctx.correlation_id)
    assert current_request() is None


@pytest.mark.anyio
async def test_concurrent_calls_get_isolated_contexts() -> None:
    seen: dict[str, str] = {}

    async def tool(name: str) -> None:
        await asyncio.sleep(0)  # force interleaving
        ctx = current_request()
        assert ctx is not None
        seen[name] = ctx.correlation_id

    wrapped = with_request_context(tool, "concurrent_tool")
    await asyncio.gather(wrapped("a"), wrapped("b"))
    assert seen["a"] != seen["b"]
    assert current_request() is None


@pytest.mark.anyio
async def test_with_request_context_preserves_identity_and_scopes_call() -> None:
    async def sample_tool(issue_number: int, confirm: bool = False) -> dict:
        """Sample docstring."""
        ctx = current_request()
        return {"tool": ctx.tool, "cid": ctx.correlation_id}

    wrapped = with_request_context(sample_tool)
    assert wrapped.__name__ == "sample_tool"
    assert wrapped.__doc__ == "Sample docstring."
    assert inspect.signature(wrapped) == inspect.signature(sample_tool)
    assert inspect.iscoroutinefunction(wrapped)
    first, second = await wrapped(1), await wrapped(2)
    assert first["tool"] == "sample_tool"
    assert first["cid"] != second["cid"]
    assert current_request() is None


def test_error_envelope_carries_correlation_id_only_inside_a_call() -> None:
    assert build_error_response("validation", "bad")["correlation_id"] is None
    with request_scope("edit_issue") as ctx:
        envelope = build_error_response("validation", "bad")
    assert envelope["correlation_id"] == ctx.correlation_id
    assert envelope["request_id"] is None  # GitHub's ID stays a separate field


def test_log_filter_stamps_records() -> None:
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "msg", None, None)
    RequestContextFilter().filter(record)
    assert (record.correlation_id, record.tool) == ("-", "-")
    with request_scope("comment_issue") as ctx:
        RequestContextFilter().filter(record)
    assert (record.correlation_id, record.tool) == (ctx.correlation_id, "comment_issue")


def test_server_registers_tools_wrapped_in_request_context() -> None:
    import server

    source = inspect.getsource(server._register_tools)
    assert "with_request_context(" in source
