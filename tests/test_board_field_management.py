"""Tests for board field management (issues #14, #17, #18).

Covers:
- #14: field-type value building (SINGLE_SELECT / NUMBER / DATE), default
  computation, Work Type inference, and strict-mode unset detection.
- #17: owner-type-aware item fetching + resolve_item_id on a USER board.
- #18: add_item_by_content_id via addProjectV2ItemById.

All GitHub interaction is mocked — no live token or network required.
"""

from __future__ import annotations

import os
import sys
from datetime import date, datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("GH_PROJECT_ORG_NAME", "octocat")
os.environ.setdefault("GH_PROJECT_REPO_NAME", "demo")
os.environ.setdefault("GH_PROJECT_PROJECT_NUMBER", "11")

from core.config import GitHubProjectSettings  # noqa: E402
from models.context import GitHubContext, ProjectTarget  # noqa: E402
from models.metadata import FieldOption, ProjectField, ProjectMetadata  # noqa: E402
from services import field_defaults  # noqa: E402
from services.project_service import ProjectService  # noqa: E402


# ── Fixtures ────────────────────────────────────────────────────────────────


def _metadata() -> ProjectMetadata:
    """A board with all the field types the task requires."""
    return ProjectMetadata(
        project_id="PVT_test",
        owner="octocat",
        project_number=11,
        fields={
            "Status": ProjectField(
                id="F_status",
                name="Status",
                data_type="SINGLE_SELECT",
                options=[
                    FieldOption(id="opt_todo", name="Todo"),
                    FieldOption(id="opt_prog", name="In Progress"),
                    FieldOption(id="opt_done", name="Done"),
                ],
            ),
            "Priority": ProjectField(
                id="F_prio",
                name="Priority",
                data_type="SINGLE_SELECT",
                options=[
                    FieldOption(id="p_urgent", name="Urgent"),
                    FieldOption(id="p_high", name="High"),
                    FieldOption(id="p_medium", name="Medium"),
                    FieldOption(id="p_low", name="Low"),
                ],
            ),
            "Area": ProjectField(
                id="F_area",
                name="Area",
                data_type="SINGLE_SELECT",
                options=[
                    FieldOption(id="a_server", name="server"),
                    FieldOption(id="a_tools", name="tools"),
                    FieldOption(id="a_docs", name="docs"),
                ],
            ),
            "Work Type": ProjectField(
                id="F_wt",
                name="Work Type",
                data_type="SINGLE_SELECT",
                options=[
                    FieldOption(id="wt_bug", name="Bug"),
                    FieldOption(id="wt_feat", name="Feature"),
                    FieldOption(id="wt_docs", name="Docs"),
                ],
            ),
            "Estimate": ProjectField(
                id="F_est", name="Estimate", data_type="NUMBER",
            ),
            "Due date": ProjectField(
                id="F_due", name="Due date", data_type="DATE",
            ),
        },
        discovered_at=datetime(2026, 1, 1),
    )


def _settings(**overrides) -> GitHubProjectSettings:
    base = dict(
        org_name="octocat",
        repo_name="demo",
        project_number=11,
        owner_type="user",
    )
    base.update(overrides)
    return GitHubProjectSettings(**base)


# ── #14: field value building for all types ─────────────────────────────────


class TestFieldValueBuilding:
    def _svc(self) -> ProjectService:
        return ProjectService(graphql_client=object(), gh_client=object())

    def test_single_select_resolves_option_id(self) -> None:
        svc = self._svc()
        md = _metadata()
        payload = svc._build_field_value(md.fields["Priority"], "High")
        assert payload == {"singleSelectOptionId": "p_high"}

    def test_single_select_area(self) -> None:
        svc = self._svc()
        md = _metadata()
        payload = svc._build_field_value(md.fields["Area"], "tools")
        assert payload == {"singleSelectOptionId": "a_tools"}

    def test_number_estimate(self) -> None:
        svc = self._svc()
        md = _metadata()
        payload = svc._build_field_value(md.fields["Estimate"], 3)
        assert payload == {"number": 3.0}

    def test_date_due(self) -> None:
        svc = self._svc()
        md = _metadata()
        payload = svc._build_field_value(md.fields["Due date"], "2026-01-01")
        assert payload == {"date": "2026-01-01"}

    def test_invalid_option_raises(self) -> None:
        from core.exceptions import ValidationError

        svc = self._svc()
        md = _metadata()
        with pytest.raises(ValidationError):
            svc._build_field_value(md.fields["Work Type"], "Nonexistent")


# ── #14: defaults computation ───────────────────────────────────────────────


class TestComputeDefaults:
    def test_fills_all_omitted_board_fields(self) -> None:
        md = _metadata()
        settings = _settings(default_area="server")
        resolved = field_defaults.compute_defaults(
            metadata=md,
            settings=settings,
            provided={},
            labels=None,
            today=date(2026, 1, 1),
        )
        assert resolved["Status"] == "Todo"           # first option
        assert resolved["Priority"] == "Medium"       # configured default
        assert resolved["Area"] == "server"           # configured, valid
        assert resolved["Estimate"] == 3.0            # configured default
        assert resolved["Due date"] == "2026-01-08"   # +7 days
        assert resolved["Work Type"] == "Feature"     # no bug label

    def test_provided_values_not_overridden(self) -> None:
        md = _metadata()
        settings = _settings()
        resolved = field_defaults.compute_defaults(
            metadata=md,
            settings=settings,
            provided={"Priority": "Urgent", "Estimate": 8},
            labels=None,
            today=date(2026, 1, 1),
        )
        assert resolved["Priority"] == "Urgent"
        assert resolved["Estimate"] == 8

    def test_work_type_inferred_from_bug_label(self) -> None:
        md = _metadata()
        settings = _settings()
        resolved = field_defaults.compute_defaults(
            metadata=md,
            settings=settings,
            provided={},
            labels=["bug", "backend"],
            today=date(2026, 1, 1),
        )
        assert resolved["Work Type"] == "Bug"

    def test_custom_due_days(self) -> None:
        md = _metadata()
        settings = _settings(default_due_days=14)
        resolved = field_defaults.compute_defaults(
            metadata=md,
            settings=settings,
            provided={},
            today=date(2026, 1, 1),
        )
        assert resolved["Due date"] == "2026-01-15"

    def test_invalid_configured_area_dropped(self) -> None:
        md = _metadata()
        settings = _settings(default_area="nonexistent-area")
        resolved = field_defaults.compute_defaults(
            metadata=md, settings=settings, provided={},
            today=date(2026, 1, 1),
        )
        assert "Area" not in resolved  # invalid default not applied

    def test_absent_board_field_skipped(self) -> None:
        # A board without Estimate should not get one defaulted.
        md = _metadata()
        del md.fields["Estimate"]
        settings = _settings()
        resolved = field_defaults.compute_defaults(
            metadata=md, settings=settings, provided={},
            today=date(2026, 1, 1),
        )
        assert "Estimate" not in resolved


class TestEnforcement:
    def test_unset_board_fields_detected(self) -> None:
        md = _metadata()
        # Only Status set — the rest are unset.
        missing = field_defaults.unset_board_fields(md, {"Status": "Todo"})
        assert "Priority" in missing
        assert "Estimate" in missing
        assert "Due date" in missing
        assert "Work Type" in missing
        assert "Area" in missing
        assert "Status" not in missing

    def test_nothing_missing_when_all_set(self) -> None:
        md = _metadata()
        settings = _settings(default_area="server")
        resolved = field_defaults.compute_defaults(
            metadata=md, settings=settings, provided={},
            today=date(2026, 1, 1),
        )
        assert field_defaults.unset_board_fields(md, resolved) == []


class TestInferWorkType:
    def test_bug(self) -> None:
        assert field_defaults.infer_work_type(["Bug"], _settings()) == "Bug"

    def test_non_bug_default(self) -> None:
        assert field_defaults.infer_work_type(["enhancement"], _settings()) == "Feature"

    def test_configured_default(self) -> None:
        s = _settings(default_work_type="Docs")
        assert field_defaults.infer_work_type(["question"], s) == "Docs"


# ── #17: owner-type aware item fetching + resolve_item_id ────────────────────


class _FakeGraphQL:
    """Records the last query/variables and returns a canned user-board page."""

    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.last_query: str | None = None
        self.last_variables: dict | None = None

    async def execute_with_retry(self, query, variables, is_mutation=False):
        self.last_query = query
        self.last_variables = variables
        return self.payload


def _user_context() -> GitHubContext:
    target = ProjectTarget(
        owner_type="user",
        owner_login="octocat",
        project_number=11,
        repository="demo",
    )
    return GitHubContext(target=target, token_provider=lambda: "t")


@pytest.mark.anyio
async def test_fetch_items_uses_user_query() -> None:
    """On a user board, the list query hits user(login:) with $login."""
    payload = {
        "data": {
            "user": {
                "projectV2": {
                    "items": {
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": [
                            {
                                "id": "ITEM_1",
                                "content": {
                                    "__typename": "Issue",
                                    "number": 42,
                                    "title": "hello",
                                    "body": "",
                                    "url": "u",
                                    "assignees": {"nodes": []},
                                    "labels": {"nodes": []},
                                },
                                "fieldValues": {"nodes": []},
                            }
                        ],
                    }
                }
            }
        }
    }
    fake = _FakeGraphQL(payload)
    svc = ProjectService(graphql_client=fake, gh_client=object(), context=_user_context())
    md = _metadata()

    items = await svc._fetch_all_items(md, max_items=50)

    assert len(items) == 1
    assert items[0].issue_number == 42
    assert "user(login:" in fake.last_query
    assert fake.last_variables["login"] == "octocat"
    assert "org" not in fake.last_variables


@pytest.mark.anyio
async def test_resolve_item_id_on_user_board() -> None:
    payload = {
        "data": {
            "user": {
                "projectV2": {
                    "items": {
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": [
                            {
                                "id": "ITEM_XYZ",
                                "content": {
                                    "__typename": "Issue",
                                    "number": 7,
                                    "title": "t",
                                    "body": "",
                                    "url": "u",
                                    "assignees": {"nodes": []},
                                    "labels": {"nodes": []},
                                },
                                "fieldValues": {"nodes": []},
                            }
                        ],
                    }
                }
            }
        }
    }
    svc = ProjectService(
        graphql_client=_FakeGraphQL(payload),
        gh_client=object(),
        context=_user_context(),
    )
    md = _metadata()
    assert await svc.resolve_item_id(md, 7) == "ITEM_XYZ"
    assert await svc.resolve_item_id(md, 999) is None


# ── #18: add_item_by_content_id ──────────────────────────────────────────────


@pytest.mark.anyio
async def test_add_item_by_content_id() -> None:
    payload = {
        "data": {"addProjectV2ItemById": {"item": {"id": "NEW_ITEM"}}}
    }
    svc = ProjectService(
        graphql_client=_FakeGraphQL(payload),
        gh_client=object(),
        context=_user_context(),
    )
    md = _metadata()
    item_id = await svc.add_item_by_content_id(md, "CONTENT_NODE_ID")
    assert item_id == "NEW_ITEM"


# Provide the anyio backend for the async tests above.
@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class TestTolerantFieldNameResolution:
    """Field names with emoji/symbol prefixes resolve from canonical names."""

    def _emoji_metadata(self) -> ProjectMetadata:
        """A board whose single-select fields carry emoji prefixes, like a
        real Projects V2 board configured with decorated field titles."""
        return ProjectMetadata(
            project_id="PVT_emoji",
            owner="octocat",
            project_number=1,
            fields={
                "Status": ProjectField(
                    id="F_status", name="Status", data_type="SINGLE_SELECT",
                    options=[FieldOption(id="s_todo", name="📌 To Do")],
                ),
                "📊 Priority": ProjectField(
                    id="F_prio", name="📊 Priority", data_type="SINGLE_SELECT",
                    options=[FieldOption(id="p_imp", name="⚡ Important")],
                ),
                "📅 Start date": ProjectField(
                    id="F_start", name="📅 Start date", data_type="DATE",
                ),
            },
            discovered_at=datetime(2026, 1, 1),
        )

    def test_canonical_name_resolves_emoji_field(self) -> None:
        from services.project_service import _resolve_field_tolerant

        md = self._emoji_metadata()
        field = _resolve_field_tolerant(md, "Priority")
        assert field is not None
        assert field.id == "F_prio"

    def test_multiword_canonical_name_resolves(self) -> None:
        from services.project_service import _resolve_field_tolerant

        md = self._emoji_metadata()
        assert _resolve_field_tolerant(md, "start date").id == "F_start"

    def test_no_match_returns_none(self) -> None:
        from services.project_service import _resolve_field_tolerant

        md = self._emoji_metadata()
        assert _resolve_field_tolerant(md, "Nonexistent") is None

    def test_exact_field_still_resolves_via_get(self) -> None:
        # Exact names must keep working through the normal dict lookup, so the
        # fallback never shadows an unambiguous exact match.
        md = self._emoji_metadata()
        assert md.fields.get("Status") is not None

    def test_ambiguous_match_returns_none(self) -> None:
        from services.project_service import _resolve_field_tolerant

        md = ProjectMetadata(
            project_id="PVT_dup", owner="octocat", project_number=1,
            fields={
                "📊 Priority": ProjectField(
                    id="F_a", name="📊 Priority", data_type="SINGLE_SELECT",
                ),
                "⭐ Priority": ProjectField(
                    id="F_b", name="⭐ Priority", data_type="SINGLE_SELECT",
                ),
            },
            discovered_at=datetime(2026, 1, 1),
        )
        assert _resolve_field_tolerant(md, "Priority") is None
