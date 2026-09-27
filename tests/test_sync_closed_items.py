"""Tests for the sync_closed_items_to_done board-reconciliation tool.

Verifies the decision logic: an item is moved to Done only when it is NOT in a
terminal column and its linked issue/PR content state is CLOSED or MERGED.
dry_run must not mutate, and issue_or_pr_number must scope to a single item.

The GitHub-facing services (discovery + project) are mocked, so no token or
network is required.
"""

import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("GH_PROJECT_ORG_NAME", "TestOrg")
os.environ.setdefault("GH_PROJECT_REPO_NAME", "TestRepo")
os.environ.setdefault("GH_PROJECT_PROJECT_NUMBER", "1")

from models.items import ProjectItem
from tools.pull_requests.pr_issue_lifecycle import (
    SyncClosedItemsToDoneInput,
    sync_closed_items_to_done,
)


def _item(node_id, number, status, content_state, content_type="PullRequest"):
    return ProjectItem(
        node_id=node_id,
        content_type=content_type,
        title=f"Item {number}",
        issue_number=number,
        status=status,
        content_state=content_state,
    )


# A representative board: a merged PR and a closed issue in In Progress (should
# move), an open PR in In Progress (skip), a merged PR already in Done (skip),
# and a merged PR in Trash (skip — terminal).
SAMPLE_ITEMS = [
    _item("PVTI_1", 101, "🛠 In Progress", "MERGED"),
    _item("PVTI_2", 102, "🛠 In Progress", "CLOSED", content_type="Issue"),
    _item("PVTI_3", 103, "🛠 In Progress", "OPEN"),
    _item("PVTI_4", 104, "✅ Done", "MERGED"),
    _item("PVTI_5", 105, "🗑️ Trash", "MERGED"),
]


def _run(params):
    """Invoke the tool with discovery + project services mocked."""
    fake_project = MagicMock()
    fake_project.list_all_items = AsyncMock(return_value=SAMPLE_ITEMS)
    fake_project.update_field = AsyncMock(return_value=None)
    fake_discovery = MagicMock()
    fake_discovery.get_cached_or_discover = AsyncMock(return_value=MagicMock())

    with patch("core.auth.resolve_token", new=AsyncMock(return_value="tok")), \
         patch("services.project_service.ProjectService", return_value=fake_project), \
         patch("services.discovery_service.DiscoveryService", return_value=fake_discovery), \
         patch("clients.graphql_client.GraphQLClient", return_value=MagicMock()), \
         patch("clients.cache_manager.CacheManager", return_value=MagicMock()), \
         patch("clients.gh_cli_client.GHCLIClient", return_value=MagicMock()):
        result = asyncio.run(sync_closed_items_to_done(params))
    return result, fake_project


def test_moves_only_closed_or_merged_non_terminal_items():
    result, project = _run(SyncClosedItemsToDoneInput())
    data = result["data"]
    assert data["moved_count"] == 2, data
    moved_numbers = sorted(m["number"] for m in data["moved"])
    assert moved_numbers == [101, 102], moved_numbers
    # update_field called exactly twice, both to Done.
    assert project.update_field.await_count == 2
    for call in project.update_field.await_args_list:
        assert call.kwargs["field_name"] == "Status"
        assert call.kwargs["value"] == "✅ Done"
    print("✅ test_moves_only_closed_or_merged_non_terminal_items")


def test_dry_run_does_not_mutate():
    result, project = _run(SyncClosedItemsToDoneInput(dry_run=True))
    data = result["data"]
    assert data["dry_run"] is True
    assert data["would_move_count"] == 2, data
    assert project.update_field.await_count == 0, "dry_run must not call update_field"
    print("✅ test_dry_run_does_not_mutate")


def test_scopes_to_single_number():
    result, project = _run(SyncClosedItemsToDoneInput(issue_or_pr_number=101))
    data = result["data"]
    assert data["moved_count"] == 1
    assert data["moved"][0]["number"] == 101
    assert project.update_field.await_count == 1
    print("✅ test_scopes_to_single_number")


def test_terminal_and_open_items_are_skipped():
    result, _ = _run(SyncClosedItemsToDoneInput())
    data = result["data"]
    # 105 (Trash) and 104 (Done) never move; 103 (open) is skipped_open.
    moved_numbers = {m["number"] for m in data["moved"]}
    assert 104 not in moved_numbers and 105 not in moved_numbers
    assert data["skipped_open"] == 1  # only #103
    print("✅ test_terminal_and_open_items_are_skipped")


def test_custom_done_status_is_idempotent():
    """A card already at a CUSTOM done_status must not be moved again.

    Regression for PR #20 review (Codex P2 + confirmed): the destination status
    is always treated as terminal even when the caller does not list it in
    keep_statuses, so repeated runs are idempotent.
    """
    items = [_item("PVTI_9", 109, "Closed", "MERGED")]
    fake_project = MagicMock()
    fake_project.list_all_items = AsyncMock(return_value=items)
    fake_project.update_field = AsyncMock(return_value=None)
    fake_discovery = MagicMock()
    fake_discovery.get_cached_or_discover = AsyncMock(return_value=MagicMock())

    with patch("core.auth.resolve_token", new=AsyncMock(return_value="tok")), \
         patch("services.project_service.ProjectService", return_value=fake_project), \
         patch("services.discovery_service.DiscoveryService", return_value=fake_discovery), \
         patch("clients.graphql_client.GraphQLClient", return_value=MagicMock()), \
         patch("clients.cache_manager.CacheManager", return_value=MagicMock()), \
         patch("clients.gh_cli_client.GHCLIClient", return_value=MagicMock()):
        result = asyncio.run(
            sync_closed_items_to_done(SyncClosedItemsToDoneInput(done_status="Closed"))
        )
    assert result["data"]["moved_count"] == 0, result["data"]
    assert fake_project.update_field.await_count == 0
    print("✅ test_custom_done_status_is_idempotent")


def test_bare_done_column_is_terminal():
    """A board whose columns carry no emoji ("Done", "Trash") must be idempotent.

    Regression: terminal columns were compared by exact name, so on a board
    with a bare "Done" column every finished card was re-proposed for
    "✅ Done" on each run. Terminal matching now ignores emoji and case.
    """
    items = [
        _item("PVTI_A", 201, "Done", "MERGED"),
        _item("PVTI_B", 202, "trash", "CLOSED", content_type="Issue"),
        _item("PVTI_C", 203, "In Progress", "CLOSED", content_type="Issue"),
    ]
    fake_project = MagicMock()
    fake_project.list_all_items = AsyncMock(return_value=items)
    fake_project.update_field = AsyncMock(return_value=None)
    fake_discovery = MagicMock()
    fake_discovery.get_cached_or_discover = AsyncMock(return_value=MagicMock())

    with patch("core.auth.resolve_token", new=AsyncMock(return_value="tok")), \
         patch("services.project_service.ProjectService", return_value=fake_project), \
         patch("services.discovery_service.DiscoveryService", return_value=fake_discovery), \
         patch("clients.graphql_client.GraphQLClient", return_value=MagicMock()), \
         patch("clients.cache_manager.CacheManager", return_value=MagicMock()), \
         patch("clients.gh_cli_client.GHCLIClient", return_value=MagicMock()):
        result = asyncio.run(
            sync_closed_items_to_done(SyncClosedItemsToDoneInput(dry_run=True))
        )
    numbers = [m["number"] for m in result["data"]["would_move"]]
    assert numbers == [203], result["data"]
    print("✅ test_bare_done_column_is_terminal")


if __name__ == "__main__":
    test_moves_only_closed_or_merged_non_terminal_items()
    test_dry_run_does_not_mutate()
    test_scopes_to_single_number()
    test_terminal_and_open_items_are_skipped()
    test_custom_done_status_is_idempotent()
    test_bare_done_column_is_terminal()
    print("All sync_closed_items_to_done tests passed")
