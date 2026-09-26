"""Tests for the create_epic workflow tool.

Verifies that create_epic:
  * resolves project/field/option IDs at RUNTIME from the discovered board
    metadata (no hardcoded Factib board IDs or "📢 Proposal" literal),
  * defaults the Status to the board's FIRST Status option when the caller
    passes none,
  * sets Status/Priority via ProjectService.update_field (name-based, so the
    correct field/option IDs are resolved from metadata),
  * links EXISTING issues as sub-issues via the link_existing param, in
    addition to creating new sub-issues from titles.

DiscoveryService, ProjectService, token resolution, and the GH CLI are all
mocked so no network / gh call is made.
"""

import os
import sys
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

# Simulate test environment (needed before importing config).
os.environ.setdefault("GH_PROJECT_ORG_NAME", "jersonmartinez")
os.environ.setdefault("GH_PROJECT_REPO_NAME", "mcp-github-projects")
os.environ.setdefault("GH_PROJECT_PROJECT_NUMBER", "11")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from clients.gh_cli_client import CommandResult  # noqa: E402
from models.metadata import (  # noqa: E402
    FieldOption,
    ProjectField,
    ProjectMetadata,
)
from tools.workflows import CreateEpicInput, create_epic  # noqa: E402


def _cmd_result(stdout: str) -> CommandResult:
    return CommandResult(stdout=stdout, stderr="", return_code=0)


def _metadata() -> ProjectMetadata:
    """A board whose Status field's first option is NOT '📢 Proposal'."""
    return ProjectMetadata(
        project_id="PVT_kwHOAG9VGc4Bixfp",
        owner="jersonmartinez",
        project_number=11,
        fields={
            "Status": ProjectField(
                id="PVTSSF_status",
                name="Status",
                data_type="SINGLE_SELECT",
                options=[
                    FieldOption(id="opt_triage", name="Triage"),
                    FieldOption(id="opt_backlog", name="Backlog"),
                ],
            ),
            "Priority": ProjectField(
                id="PVTSSF_priority",
                name="Priority",
                data_type="SINGLE_SELECT",
                options=[FieldOption(id="opt_high", name="High")],
            ),
        },
        discovered_at=datetime.now(timezone.utc),
    )


def _gh_run_factory():
    """Return an AsyncMock for GHCLIClient.run that answers by argv shape.

    * `issue create` -> prints the new issue URL (parent #100, subs #101+).
    * `api .../issues/<n> --jq .node_id` -> prints a node id for that issue.
    """
    created = {"next": 100}

    async def _run(argv, *args, **kwargs):
        if "create" in argv and "issue" in argv:
            num = created["next"]
            created["next"] += 1
            return _cmd_result(
                f"https://github.com/jersonmartinez/mcp-github-projects/issues/{num}"
            )
        if "api" in argv:
            # Extract the issue number from repos/.../issues/<n>.
            target = next((a for a in argv if "/issues/" in a), "")
            num = target.rstrip("/").split("/")[-1] or "0"
            return _cmd_result(f"NODE_{num}")
        return _cmd_result("")

    return AsyncMock(side_effect=_run)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_create_epic_resolves_status_at_runtime() -> None:
    """With no status passed, the board's FIRST Status option is applied."""
    meta = _metadata()
    run_mock = _gh_run_factory()
    add_item_mock = AsyncMock(return_value="ITEM_1")
    update_field_mock = AsyncMock()
    graphql_mock = AsyncMock(return_value={"data": {}})

    with patch("core.auth.resolve_token", new=AsyncMock(return_value="tok")), \
            patch("clients.gh_cli_client.GHCLIClient.run", new=run_mock), \
            patch("clients.gh_cli_client.GHCLIClient.api_graphql", new=graphql_mock), \
            patch(
                "services.discovery_service.DiscoveryService.get_cached_or_discover",
                new=AsyncMock(return_value=meta),
            ), \
            patch("services.project_service.ProjectService.add_item", new=add_item_mock), \
            patch("services.project_service.ProjectService.update_field", new=update_field_mock):
        result = await create_epic(
            CreateEpicInput(title="🏔️ [Epic] Runtime IDs", body="Epic body")
        )

    assert result["ok"] is True
    # Defaulted to the board's first Status option — NOT a Factib label.
    assert result["data"]["status_applied"] == "Triage"
    assert result["data"]["parent_issue"] == 100

    # update_field was called by NAME with the runtime-resolved metadata.
    status_calls = [
        c for c in update_field_mock.await_args_list
        if c.kwargs.get("field_name") == "Status"
    ]
    assert status_calls, "Status must be set via update_field"
    assert status_calls[0].kwargs["value"] == "Triage"
    assert status_calls[0].kwargs["metadata"] is meta


@pytest.mark.anyio
async def test_create_epic_creates_and_links() -> None:
    """sub_tasks create new issues; link_existing links existing ones."""
    meta = _metadata()
    run_mock = _gh_run_factory()
    graphql_mock = AsyncMock(return_value={"data": {}})

    with patch("core.auth.resolve_token", new=AsyncMock(return_value="tok")), \
            patch("clients.gh_cli_client.GHCLIClient.run", new=run_mock), \
            patch("clients.gh_cli_client.GHCLIClient.api_graphql", new=graphql_mock), \
            patch(
                "services.discovery_service.DiscoveryService.get_cached_or_discover",
                new=AsyncMock(return_value=meta),
            ), \
            patch("services.project_service.ProjectService.add_item", new=AsyncMock(return_value="ITEM")), \
            patch("services.project_service.ProjectService.update_field", new=AsyncMock()):
        result = await create_epic(
            CreateEpicInput(
                title="🏔️ [Epic] Both",
                body="body",
                sub_tasks=["New task A", "New task B"],
                link_existing=[42, 43],
                status="Backlog",
                priority="High",
            )
        )

    assert result["ok"] is True
    assert result["data"]["status_applied"] == "Backlog"
    assert result["data"]["sub_issues_count"] == 2
    assert result["data"]["linked_existing_count"] == 2
    assert [e["number"] for e in result["data"]["linked_existing"]] == [42, 43]

    # addSubIssue mutation fired for 2 new subs + 2 existing = 4 links.
    assert graphql_mock.await_count == 4
    # Existing issues #42/#43 were linked by their node ids (not recreated).
    linked_sub_nodes = {
        c.args[1]["subIssueId"] for c in graphql_mock.await_args_list
    }
    assert "NODE_42" in linked_sub_nodes
    assert "NODE_43" in linked_sub_nodes


@pytest.mark.anyio
async def test_create_epic_no_hardcoded_factib_ids() -> None:
    """create_epic must not reference the old hardcoded Factib board IDs."""
    import tools.workflows as wf

    source = wf.create_epic.__doc__ or ""
    # The module no longer defines the Factib board constants.
    assert not hasattr(wf, "_PROJECT_ID")
    assert not hasattr(wf, "_STATUS_FIELD_ID")
    assert not hasattr(wf, "_PRIORITY_FIELD_ID")
    assert "📢 Proposal" not in source
