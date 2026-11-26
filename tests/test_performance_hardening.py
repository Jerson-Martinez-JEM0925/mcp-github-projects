"""Regression tests for the hardening/performance overhaul."""

from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError as PydanticValidationError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("GH_PROJECT_ORG_NAME", "TestOrg")
os.environ.setdefault("GH_PROJECT_REPO_NAME", "TestRepo")
os.environ.setdefault("GH_PROJECT_PROJECT_NUMBER", "1")
os.environ.setdefault("GH_PROJECT_OWNER_TYPE", "organization")

from clients.gh_cli_client import _rest_issue_json
from core.capabilities import PROFILES, validate_capabilities
from graphql.queries import LIST_ITEMS_QUERY_ORG, LIST_ITEMS_QUERY_USER
from services.project_service import ProjectService
from tools.discovery.list_items import ListProjectItemsInput


def test_list_project_items_due_date_operator_is_part_of_schema() -> None:
    """Regression: due_date_op was documented/used but absent from the model."""
    params = ListProjectItemsInput(due_date="2026-01-31", due_date_op="before")
    assert params.due_date_op == "before"
    assert params.include_body is False
    with pytest.raises(PydanticValidationError):
        ListProjectItemsInput(due_date="2026-01-31", due_date_op="around")


def test_project_item_queries_support_conditional_body_projection() -> None:
    """List queries can omit large bodies without changing result shape."""
    for query in (LIST_ITEMS_QUERY_ORG, LIST_ITEMS_QUERY_USER):
        assert "$includeBody: Boolean!" in query
        assert "body @include(if: $includeBody)" in query


def test_project_service_passes_include_body_variable() -> None:
    """A compact board listing asks GraphQL not to materialize bodies."""
    gql = MagicMock()
    gql.execute_with_retry = AsyncMock(return_value={
        "data": {
            "organization": {
                "projectV2": {
                    "items": {
                        "nodes": [],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    }
                }
            }
        }
    })
    service = ProjectService(graphql_client=gql, gh_client=MagicMock())
    service._context = MagicMock()
    service._context.target.owner_type = "organization"
    metadata = MagicMock(owner="TestOrg", project_number=1)

    asyncio.run(service.list_items(metadata=metadata, include_body=False))

    variables = gql.execute_with_retry.await_args.args[1]
    assert variables["includeBody"] is False


def test_rest_issue_projection_matches_gh_camelcase_contract() -> None:
    """The REST fast path preserves the shape consumed from gh issue list."""
    projected = _rest_issue_json(
        {
            "number": 7,
            "title": "Example",
            "state": "closed",
            "created_at": "2026-01-01T00:00:00Z",
            "closed_at": "2026-01-02T00:00:00Z",
            "labels": [{"name": "bug", "color": "ff0000"}],
            "assignees": [{"login": "octocat"}],
            "milestone": {"title": "v1"},
        },
        ["number", "state", "createdAt", "closedAt", "labels", "assignees", "milestone"],
    )
    assert projected["state"] == "CLOSED"
    assert projected["createdAt"] == "2026-01-01T00:00:00Z"
    assert projected["labels"][0]["name"] == "bug"
    assert projected["assignees"][0]["login"] == "octocat"
    assert projected["milestone"]["title"] == "v1"


def test_role_profiles_have_meaningful_capability_boundaries() -> None:
    """Developer, DevOps and PM profiles expose distinct operational surfaces."""
    assert validate_capabilities("create_pull_request", PROFILES["developer"])[0]
    assert validate_capabilities("dispatch_workflow", PROFILES["devops"])[0]
    assert not validate_capabilities("dispatch_workflow", PROFILES["project_manager"])[0]
    assert validate_capabilities("close_sprint", PROFILES["project_manager"])[0]
    assert not validate_capabilities("commit_files", PROFILES["read_only"])[0]


def test_resource_cache_is_short_lived_and_credential_scoped() -> None:
    """Stable-resource cache reuses per key and separates credentials."""
    from clients.resource_cache import cache_key, clear, get_or_load

    clear()
    calls = 0

    async def loader() -> list[str]:
        nonlocal calls
        calls += 1
        return ["bug"]

    async def exercise() -> None:
        key_one = cache_key("labels", "octo/repo", "token-one", "50")
        key_two = cache_key("labels", "octo/repo", "token-two", "50")
        assert key_one != key_two
        first, hit_one = await get_or_load(key_one, ttl_seconds=30, loader=loader)
        second, hit_two = await get_or_load(key_one, ttl_seconds=30, loader=loader)
        third, hit_three = await get_or_load(key_two, ttl_seconds=30, loader=loader)
        assert first == second == third == ["bug"]
        assert (hit_one, hit_two, hit_three) == (False, True, False)

    asyncio.run(exercise())
    assert calls == 2
    clear()


def test_gh_api_fast_path_preserves_method_endpoint_and_typed_body() -> None:
    """Generic gh api commands use direct REST without losing JSON field semantics."""
    from clients.gh_cli_client import GHCLIClient

    rest = MagicMock()
    rest.request = AsyncMock(return_value={"number": 3, "state": "open"})
    client = GHCLIClient(context=MagicMock())

    result = asyncio.run(client._rest_api(
        rest,
        [
            "api", "--method", "PATCH", "repos/octo/repo/milestones/3",
            "-f", "title=Sprint 1", "-F", "due_on=null", "-f", "inputs[mode]=fast",
        ],
    ))

    assert result is not None
    rest.request.assert_awaited_once_with(
        "PATCH",
        "/repos/octo/repo/milestones/3",
        json_body={"title": "Sprint 1", "due_on": None, "inputs": {"mode": "fast"}},
    )
    assert result.stdout == '{"number":3,"state":"open"}'


def test_gh_api_fast_path_falls_back_for_output_shaping_options() -> None:
    """Options whose output semantics differ stay on the real gh subprocess."""
    from clients.gh_cli_client import GHCLIClient

    client = GHCLIClient(context=MagicMock())
    result = asyncio.run(client._rest_api(
        MagicMock(),
        ["api", "repos/octo/repo/issues", "--jq", ".[].number"],
    ))
    assert result is None


def test_graphql_http_pool_is_credential_and_loop_scoped(monkeypatch: pytest.MonkeyPatch) -> None:
    """GraphQL pooling reuses keep-alive clients only for the same credential."""
    import clients.graphql_client as graphql_module

    created: list[object] = []

    class FakeAsyncClient:
        is_closed = False

        def __init__(self, **kwargs):
            self.kwargs = kwargs
            created.append(self)

    monkeypatch.setattr(graphql_module.httpx, "AsyncClient", FakeAsyncClient)
    graphql_module._SHARED_CLIENTS.clear()

    async def exercise() -> None:
        first = graphql_module._shared_http_client("token-a", 10)
        assert graphql_module._shared_http_client("token-a", 10) is first
        second = graphql_module._shared_http_client("token-b", 10)
        assert second is not first

    asyncio.run(exercise())
    assert len(created) == 2
    graphql_module._SHARED_CLIENTS.clear()


def test_graphql_read_retries_transient_http_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A retryable 503 read is retried; the successful response is returned."""
    import clients.graphql_client as graphql_module
    from clients.graphql_client import GraphQLClient
    from core.config import get_settings

    class Response:
        def __init__(self, status_code: int, payload: dict | None = None):
            self.status_code = status_code
            self.headers = {"Retry-After": "0"} if status_code >= 500 else {}
            self.reason_phrase = "x"
            self._payload = payload or {}

        def json(self):
            return self._payload

    class FakeClient:
        def __init__(self):
            self.post = AsyncMock(side_effect=[
                Response(503),
                Response(200, {"data": {"viewer": {"login": "octocat"}}}),
            ])

    fake = FakeClient()
    monkeypatch.setenv("GH_PROJECT_RETRY_DELAY_SECONDS", "0")
    get_settings.cache_clear()
    monkeypatch.setattr(graphql_module, "_shared_http_client", lambda token, timeout: fake)

    result = asyncio.run(GraphQLClient(token="token").execute_with_retry("query { viewer { login } }"))
    assert result["data"]["viewer"]["login"] == "octocat"
    assert fake.post.await_count == 2
    get_settings.cache_clear()


def test_observability_disabled_by_default_and_span_is_noop() -> None:
    """OpenTelemetry is opt-in; the default path has no exporter or span overhead."""
    from core.observability import configure_observability, observability_enabled, tool_span

    settings = MagicMock(otel_enabled=False)
    assert configure_observability(settings) is False
    assert observability_enabled() is False
    with tool_span("server_info", correlation_id="abc123", access_tier="read") as span:
        assert span is None


def test_hash_lock_parser_and_renderer_emit_pip_hash_format(tmp_path) -> None:
    """Hash locks are exact, deterministic and consumable by pip --require-hashes."""
    from scripts.generate_hash_locks import read_pins, render_lock

    source = tmp_path / "pins.txt"
    source.write_text("# comment\nexample-package==1.2.3\n", encoding="utf-8")
    pins = read_pins(source)
    assert pins == [("example-package", "1.2.3")]
    rendered = render_lock(pins, {pins[0]: ["a" * 64, "b" * 64]}, str(source))
    assert "example-package==1.2.3" in rendered
    assert f"--hash=sha256:{'a' * 64}" in rendered
    assert f"--hash=sha256:{'b' * 64}" in rendered


def test_gh_api_graphql_and_unknown_flags_fall_back_to_real_cli() -> None:
    """GraphQL and unsupported output flags must never be silently reinterpreted."""
    from clients.gh_cli_client import GHCLIClient

    client = GHCLIClient(context=MagicMock())
    assert asyncio.run(client._rest_api(
        MagicMock(), ["api", "graphql", "-f", "query=query { viewer { login } }"]
    )) is None
    assert asyncio.run(client._rest_issue_comment(
        MagicMock(), ["issue", "comment", "1", "--repo", "octo/repo", "--body", "x", "--edit-last"]
    )) is None
    assert asyncio.run(client._rest_label_list(
        MagicMock(), ["label", "list", "--repo", "octo/repo", "--sort", "created"]
    )) is None


def test_metrics_registry_records_latency_success_and_error_envelopes() -> None:
    """Tool metrics expose aggregate latency metadata without arguments or payloads."""
    from core.context import with_request_context
    from core.metrics import metrics_snapshot, reset_metrics

    reset_metrics()

    async def successful() -> dict:
        return {"ok": True, "data": {"secret_argument": "not-stored"}}

    async def app_error() -> dict:
        return {"ok": False, "message": "sensitive detail must not be stored"}

    async def exercise() -> None:
        await with_request_context(successful, "metric_success", "read")()
        await with_request_context(app_error, "metric_error", "read")()

    asyncio.run(exercise())
    snapshot = metrics_snapshot(limit=10)
    assert snapshot["totals"]["calls"] == 2
    assert snapshot["totals"]["errors"] == 1
    by_name = {row["tool"]: row for row in snapshot["tools"]}
    assert by_name["metric_success"]["errors"] == 0
    assert by_name["metric_error"]["errors"] == 1
    assert by_name["metric_success"]["calls"] == 1
    assert "secret_argument" not in str(snapshot)
    assert "sensitive detail" not in str(snapshot)
    reset_metrics()


def test_circuit_breaker_opens_and_admits_half_open_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Repeated transient failures open the circuit and prevent a request storm."""
    from core.circuit_breaker import CircuitBreaker, CircuitOpenError

    breaker = CircuitBreaker("github-test", failure_threshold=2, reset_timeout_seconds=30)
    breaker.before_call()
    breaker.record_failure()
    breaker.before_call()
    breaker.record_failure()
    assert breaker.snapshot()["open"] is True
    with pytest.raises(CircuitOpenError):
        breaker.before_call()

    monkeypatch.setattr(breaker, "reset_timeout_seconds", 0.001)
    import time

    time.sleep(0.002)
    breaker.before_call()  # half-open probe
    breaker.record_success()
    assert breaker.snapshot()["open"] is False


def test_operational_metrics_tool_returns_compact_snapshot() -> None:
    """The metrics MCP surface reports counters without leaking invocation data."""
    from core.metrics import record_rate_limit, record_tool_invocation, reset_metrics
    from tools.meta.operational_metrics import GetOperationalMetricsInput, get_operational_metrics

    reset_metrics()
    record_tool_invocation("list_project_items", 42, ok=True)
    record_rate_limit("github_api")
    result = asyncio.run(get_operational_metrics(GetOperationalMetricsInput(limit=5)))
    assert result["ok"] is True
    assert result["data"]["rate_limits"]["github_api"] == 1
    assert result["data"]["tools"][0]["tool"] == "list_project_items"
    reset_metrics()


def test_search_tools_uses_live_registry_and_returns_compact_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    """Compact discovery searches registered names without serializing schemas."""
    from tools.meta.tool_discovery import SearchToolsInput, search_tools

    class FakeMCP:
        async def list_tools(self):
            return [
                SimpleNamespace(name="list_labels", description="List repository labels."),
                SimpleNamespace(name="create_label", description="Create one repository label."),
            ]

    monkeypatch.delitem(sys.modules, "server", raising=False)
    monkeypatch.setitem(sys.modules, "__main__", SimpleNamespace(mcp=FakeMCP()))

    result = asyncio.run(search_tools(SearchToolsInput(query="label", access_tier="read", limit=10)))
    assert result["ok"] is True
    assert result["data"]["count"] == 1
    assert result["data"]["tools"] == [
        {
            "name": "list_labels",
            "access_tier": "read",
            "capabilities": ["labels.read"],
            "description": "List repository labels.",
        }
    ]


def test_circuit_breaker_registry_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Credential-scoped breaker state cannot grow without bound in HTTP mode."""
    import core.circuit_breaker as circuit_module

    circuit_module.reset_circuit_breakers()
    monkeypatch.setattr(circuit_module, "_MAX_BREAKERS", 2)
    first = circuit_module.github_circuit_breaker("token-a")
    circuit_module.github_circuit_breaker("token-b")
    circuit_module.github_circuit_breaker("token-c")
    assert len(circuit_module._BREAKERS) == 2
    assert first.name not in circuit_module._BREAKERS
    circuit_module.reset_circuit_breakers()
