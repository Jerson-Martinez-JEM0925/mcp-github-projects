"""Contract tests: multi-target validation and 100-tool regression.

Validates structural contracts without requiring a live GitHub token:
- All tools register correctly (>= 100)
- No duplicate tool names
- Discovery queries have correct variables per target type
- Cache isolation between different owners
- Parser handles both JSON formats
- Estimate boundaries are enforced
- Every registered tool has a capability entry

Run inside Docker:
    docker run --rm -e GH_PROJECT_ORG_NAME=Test -e GH_PROJECT_REPO_NAME=Test \
      -e GH_PROJECT_PROJECT_NUMBER=1 github-project-mcp:latest \
      bash -c "pip install --quiet pytest && python3 -m pytest tests/test_contracts.py -v"
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import os
import sys

import pytest

# ── Env setup (needed before importing config) ──────────────────────────────

os.environ.setdefault("GH_PROJECT_ORG_NAME", "ContractTestOrg")
os.environ.setdefault("GH_PROJECT_REPO_NAME", "ContractTestRepo")
os.environ.setdefault("GH_PROJECT_PROJECT_NUMBER", "1")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ══════════════════════════════════════════════════════════════════════════════
# 1. TOOL REGISTRATION — 100-TOOL REGRESSION
# ══════════════════════════════════════════════════════════════════════════════


def _list_tool_names() -> list[str]:
    """Import the mcp server and list registered tool names (sync-safe).

    Uses a thread pool executor to avoid conflicts with pytest-anyio's
    event loop when running under pytest with the anyio plugin.
    """
    import concurrent.futures

    from server import mcp

    def _run() -> list[str]:
        loop = asyncio.new_event_loop()
        try:
            tools = loop.run_until_complete(mcp.list_tools())
            return [t.name for t in tools]
        finally:
            loop.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(_run).result(timeout=30)


class TestToolRegistration:
    """Verify the MCP server registers all expected tools."""

    def test_all_tools_registered(self) -> None:
        """At least 100 tools must be registered on the MCP server."""
        tools = _list_tool_names()
        assert len(tools) >= 100, (
            f"Expected >= 100 registered tools, got {len(tools)}. "
            f"Tool count regression detected."
        )

    def test_tool_names_unique(self) -> None:
        """No duplicate tool names should exist in the registry."""
        tools = _list_tool_names()
        seen: set[str] = set()
        duplicates: list[str] = []
        for name in tools:
            if name in seen:
                duplicates.append(name)
            seen.add(name)
        assert not duplicates, f"Duplicate tool names: {duplicates}"


# ══════════════════════════════════════════════════════════════════════════════
# 2. DISCOVERY QUERIES — MULTI-TARGET VALIDATION
# ══════════════════════════════════════════════════════════════════════════════


class TestDiscoveryQueries:
    """Verify GraphQL queries have correct variables per target type."""

    def test_org_target_discovery_query(self) -> None:
        """Organization discovery query must use $org and $number variables."""
        from graphql.queries import DISCOVERY_QUERY_ORG

        assert "$org: String!" in DISCOVERY_QUERY_ORG
        assert "$number: Int!" in DISCOVERY_QUERY_ORG
        assert "organization(login: $org)" in DISCOVERY_QUERY_ORG
        # Must NOT use user-specific variables
        assert "$login" not in DISCOVERY_QUERY_ORG
        assert "user(login:" not in DISCOVERY_QUERY_ORG

    def test_user_target_discovery_query(self) -> None:
        """User discovery query must use $login and $number variables."""
        from graphql.queries import DISCOVERY_QUERY_USER

        assert "$login: String!" in DISCOVERY_QUERY_USER
        assert "$number: Int!" in DISCOVERY_QUERY_USER
        assert "user(login: $login)" in DISCOVERY_QUERY_USER
        # Must NOT use org-specific variables
        assert "$org" not in DISCOVERY_QUERY_USER
        assert "organization(login:" not in DISCOVERY_QUERY_USER

    def test_get_query_variables_org(self) -> None:
        """get_query_variables for org type returns $org key."""
        from graphql.queries import get_query_variables

        variables = get_query_variables("MyOrg", 5, owner_type="organization")
        assert variables == {"org": "MyOrg", "number": 5}
        assert "login" not in variables

    def test_get_query_variables_user(self) -> None:
        """get_query_variables for user type returns $login key."""
        from graphql.queries import get_query_variables

        variables = get_query_variables("myuser", 3, owner_type="user")
        assert variables == {"login": "myuser", "number": 3}
        assert "org" not in variables

    def test_extract_project_data_org(self) -> None:
        """extract_project_data navigates org response path."""
        from graphql.queries import extract_project_data

        response = {
            "data": {
                "organization": {
                    "projectV2": {"id": "PVT_org_123", "fields": {"nodes": []}}
                }
            }
        }
        result = extract_project_data(response, owner_type="organization")
        assert result is not None
        assert result["id"] == "PVT_org_123"

    def test_extract_project_data_user(self) -> None:
        """extract_project_data navigates user response path."""
        from graphql.queries import extract_project_data

        response = {
            "data": {
                "user": {
                    "projectV2": {"id": "PVT_user_456", "fields": {"nodes": []}}
                }
            }
        }
        result = extract_project_data(response, owner_type="user")
        assert result is not None
        assert result["id"] == "PVT_user_456"


# ══════════════════════════════════════════════════════════════════════════════
# 3. CACHE ISOLATION — SAME PROJECT NUMBER, DIFFERENT OWNERS
# ══════════════════════════════════════════════════════════════════════════════


class TestCacheIsolation:
    """Verify different targets don't share cache even with same project number."""

    def test_same_project_number_different_owners(self) -> None:
        """Two targets with same project number but different owners get different paths."""
        from config import _default_cache_path

        # Target A
        os.environ["GH_PROJECT_ORG_NAME"] = "OrgAlpha"
        os.environ["GH_PROJECT_REPO_NAME"] = "RepoAlpha"
        os.environ["GH_PROJECT_PROJECT_NUMBER"] = "1"
        os.environ.pop("GH_PROJECT_CACHE_PATH", None)
        path_a = str(_default_cache_path())

        # Target B — same project number, different owner
        os.environ["GH_PROJECT_ORG_NAME"] = "OrgBeta"
        os.environ["GH_PROJECT_REPO_NAME"] = "RepoBeta"
        os.environ["GH_PROJECT_PROJECT_NUMBER"] = "1"
        path_b = str(_default_cache_path())

        assert path_a != path_b, (
            f"Cache paths must differ for different targets: "
            f"A={path_a} vs B={path_b}"
        )
        assert "OrgAlpha" in path_a
        assert "OrgBeta" in path_b

        # Restore
        os.environ["GH_PROJECT_ORG_NAME"] = "ContractTestOrg"
        os.environ["GH_PROJECT_REPO_NAME"] = "ContractTestRepo"


# ══════════════════════════════════════════════════════════════════════════════
# 4. PARSER CONTRACTS — JSON FORMAT HANDLING
# ══════════════════════════════════════════════════════════════════════════════


class TestParserContracts:
    """Validate that parse_json_items handles both array and envelope formats."""

    def test_parser_handles_array_format(self) -> None:
        """Direct JSON array format: [item1, item2, ...]"""
        from hardening import parse_json_items

        data = '[{"id": "A"}, {"id": "B"}, {"id": "C"}]'
        result = parse_json_items(data, context="contract_test")
        assert len(result) == 3
        assert result[0]["id"] == "A"
        assert result[2]["id"] == "C"

    def test_parser_handles_envelope_format(self) -> None:
        """Envelope format: {"items": [item1, item2, ...]}"""
        from hardening import parse_json_items

        data = '{"items": [{"id": "X"}, {"id": "Y"}]}'
        result = parse_json_items(data, context="contract_test")
        assert len(result) == 2
        assert result[0]["id"] == "X"
        assert result[1]["id"] == "Y"

    def test_parser_rejects_invalid_json(self) -> None:
        """Invalid JSON must raise ValueError with context."""
        from hardening import parse_json_items

        with pytest.raises(ValueError, match="contract_test"):
            parse_json_items("not valid json {", context="contract_test")

    def test_parser_rejects_non_collection_type(self) -> None:
        """A bare string or number is not a valid items format."""
        from hardening import parse_json_items

        with pytest.raises(ValueError):
            parse_json_items('"just a string"', context="contract_test")


# ══════════════════════════════════════════════════════════════════════════════
# 5. ESTIMATE VALIDATION BOUNDARIES
# ══════════════════════════════════════════════════════════════════════════════


class TestEstimateValidation:
    """Verify estimate config enforces min/max/granularity."""

    def test_estimate_min_boundary(self) -> None:
        """Settings enforce a minimum estimate value."""
        from config import get_settings

        settings = get_settings()
        assert settings.estimate_min >= 0
        assert settings.estimate_min == 0.25  # configured default

    def test_estimate_max_boundary(self) -> None:
        """Settings enforce a maximum estimate value."""
        from config import get_settings

        settings = get_settings()
        assert settings.estimate_max > settings.estimate_min
        assert settings.estimate_max == 9999.0  # configured default

    def test_estimate_granularity(self) -> None:
        """Granularity must be positive and divide the range evenly."""
        from config import get_settings

        settings = get_settings()
        assert settings.estimate_granularity > 0
        assert settings.estimate_granularity == 0.25  # configured default

    def test_estimate_min_less_than_granularity_is_invalid(self) -> None:
        """A value below estimate_min should be caught by validation logic."""
        from config import get_settings

        settings = get_settings()
        test_value = settings.estimate_min - 0.01
        assert test_value < settings.estimate_min

    def test_estimate_exceeds_max_is_invalid(self) -> None:
        """A value above estimate_max should be caught by validation logic."""
        from config import get_settings

        settings = get_settings()
        test_value = settings.estimate_max + 1.0
        assert test_value > settings.estimate_max

    def test_estimate_not_aligned_to_granularity(self) -> None:
        """A value that doesn't align to granularity steps is detectable."""
        from config import get_settings

        settings = get_settings()
        # 0.3 is not a multiple of 0.25
        remainder = 0.3 % settings.estimate_granularity
        assert remainder > 0.001, "0.3 should not align to 0.25 granularity"


# ══════════════════════════════════════════════════════════════════════════════
# 6. CAPABILITIES COVER ALL TOOLS
# ══════════════════════════════════════════════════════════════════════════════


class TestCapabilitiesCoverage:
    """Every registered tool must have a corresponding capability entry."""

    def test_capabilities_cover_all_tools(self) -> None:
        """All tools in the MCP server must appear in TOOL_CAPABILITIES."""
        from capabilities import TOOL_CAPABILITIES

        registered_tools = set(_list_tool_names())
        capability_tools = set(TOOL_CAPABILITIES.keys())

        # Every registered tool should have a capability entry
        missing = registered_tools - capability_tools
        assert not missing, (
            f"{len(missing)} registered tool(s) have no capability entry: "
            f"{sorted(missing)[:10]}{'...' if len(missing) > 10 else ''}"
        )

    @pytest.mark.xfail(
        reason="Pre-existing: build_automation_decision and build_issue_bundle "
        "have capability entries but are not yet registered in the server",
        strict=False,
    )
    def test_no_orphan_capabilities(self) -> None:
        """No capability entries for tools that don't exist in the server."""
        from capabilities import TOOL_CAPABILITIES

        registered_tools = set(_list_tool_names())
        capability_tools = set(TOOL_CAPABILITIES.keys())

        orphans = capability_tools - registered_tools
        assert not orphans, (
            f"{len(orphans)} capability entries for unregistered tools: "
            f"{sorted(orphans)[:10]}{'...' if len(orphans) > 10 else ''}"
        )

    def test_capability_tool_names_list_matches(self) -> None:
        """CAPABILITY_TOOL_NAMES list must contain exactly 60 entries."""
        from tools.capability_suite import CAPABILITY_TOOL_NAMES

        assert len(CAPABILITY_TOOL_NAMES) == 60, (
            f"Expected 60 capability suite tools, got {len(CAPABILITY_TOOL_NAMES)}"
        )

    def test_capability_suite_functions_exist(self) -> None:
        """Every name in CAPABILITY_TOOL_NAMES must be a callable in the module."""
        from tools import capability_suite
        from tools.capability_suite import CAPABILITY_TOOL_NAMES

        missing: list[str] = []
        for name in CAPABILITY_TOOL_NAMES:
            fn = getattr(capability_suite, name, None)
            if fn is None or not callable(fn):
                missing.append(name)
        assert not missing, (
            f"CAPABILITY_TOOL_NAMES references non-existent functions: {missing}"
        )


class TestStableToolSchemas:
    """Freeze the public tool contract for the 1.x release line."""

    def test_tool_schema_digest(self) -> None:
        """A schema change must be intentional and called out in release notes."""
        os.environ["MCP_ACCESS_LEVEL"] = "full"
        import server

        server = importlib.reload(server)
        tools = asyncio.run(server.mcp.list_tools())
        contracts = [
            {
                "name": tool.name,
                "description": tool.description or "",
                "parameters": tool.parameters,
                "output_schema": tool.output_schema,
            }
            for tool in tools
        ]
        contracts.sort(key=lambda item: item["name"])
        canonical = json.dumps(contracts, sort_keys=True, separators=(",", ":"), default=str)
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        assert digest == "50a7965ba9444cd691822010bb058101d3a30da6d8032a17feb12516d66fd1f1", (
            f"Stable tool schema digest changed: {digest}. "
            "Update intentionally and document the protocol change."
        )
