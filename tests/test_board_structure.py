"""Board structure tools (#26) and option-name matching (#37 audit).

Fakes are injected through the ServiceFactory; the discovery service is a
stub returning fixed metadata, so no cache or network is touched.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.access import ToolAccess, classify  # noqa: E402
from core.config import get_settings  # noqa: E402
from core.exceptions import ValidationError  # noqa: E402
from core.factory import ServiceFactory, use_service_factory  # noqa: E402
from models.metadata import FieldOption, ProjectField, ProjectMetadata  # noqa: E402
from services.project_service import ProjectService, bare_option_name  # noqa: E402
from tools.projects.board_structure import (  # noqa: E402
    CreateProjectViewInput,
    ListProjectViewsInput,
    OptionSpec,
    SetFieldOptionsInput,
    create_project_view,
    list_project_views,
    set_field_options,
)

CURRENT = [
    {"id": "o1", "name": "📌 To Do", "color": "BLUE", "description": "queued"},
    {"id": "o2", "name": "🛠 In Progress", "color": "YELLOW", "description": ""},
    {"id": "o3", "name": "✅ Done", "color": "GREEN", "description": ""},
]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def configured_target(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GH_PROJECT_ORG_NAME", "octo-org")
    monkeypatch.setenv("GH_PROJECT_REPO_NAME", "octo-repo")
    monkeypatch.setenv("GH_PROJECT_PROJECT_NUMBER", "7")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _metadata() -> ProjectMetadata:
    return ProjectMetadata(
        project_id="PVT_1",
        owner="octo-org",
        project_number=7,
        fields={
            "Status": ProjectField(
                id="F_status",
                name="Status",
                data_type="SINGLE_SELECT",
                options=[FieldOption(id=o["id"], name=o["name"]) for o in CURRENT],
            ),
            "Title": ProjectField(id="F_title", name="Title", data_type="TEXT", options=[]),
        },
        discovered_at=datetime.now(timezone.utc),
    )


class StubDiscovery:
    def __init__(self) -> None:
        self.forced = 0

    async def get_cached_or_discover(self) -> ProjectMetadata:
        return _metadata()

    async def discover(self, force: bool = False) -> ProjectMetadata:
        self.forced += 1
        return _metadata()


class FakeGraphQL:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict, bool]] = []

    async def execute(self, query, variables=None):
        return await self.execute_with_retry(query, variables)

    async def execute_with_retry(self, query, variables=None, *, is_mutation=False):
        self.calls.append((query, variables or {}, is_mutation))
        if "SingleSelectFieldOptions" in query:
            return {"data": {"node": {"id": "F_status", "options": CURRENT}}}
        if "UpdateSingleSelectOptions" in query:
            opts = [{"id": o.get("id", "new"), **{k: o[k] for k in ("name", "color", "description")}} for o in variables["options"]]
            return {"data": {"updateProjectV2Field": {"projectV2Field": {"options": opts}}}}
        if "ProjectViews" in query:
            return {"data": {"node": {"url": "https://x", "views": {"nodes": [
                {"id": "V1", "number": 1, "name": "Board", "layout": "BOARD_LAYOUT", "filter": ""}]}}}}
        if "CreateProjectView" in query:
            return {"data": {"createProjectV2View": {"projectV2View": {
                "id": "V2", "number": 2, "name": variables["name"], "layout": variables["layout"], "filter": ""}}}}
        if "UpdateProjectViewFilter" in query:
            return {"data": {"updateProjectV2View": {"projectV2View": {
                "id": "V2", "number": 2, "name": "x", "layout": "BOARD_LAYOUT", "filter": variables["filter"]}}}}
        return {"data": {}}


class Factory(ServiceFactory):
    def __init__(self, gql: FakeGraphQL, discovery: StubDiscovery) -> None:
        super().__init__(graphql_client=gql)
        self._discovery = discovery

    async def discovery_service(self, graphql_client=None):  # noqa: D401 - test stub
        return self._discovery


async def _run(fn, params, gql=None, discovery=None):
    gql = gql or FakeGraphQL()
    discovery = discovery or StubDiscovery()
    with use_service_factory(Factory(gql, discovery)):
        return await fn(params), gql, discovery


def _mutations(gql: FakeGraphQL) -> list[dict]:
    return [v for q, v, m in gql.calls if m]


# ── set_field_options ───────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_dry_run_is_default_and_changes_nothing() -> None:
    params = SetFieldOptionsInput(options=[OptionSpec(name="📢 Proposal"), OptionSpec(name="📌 To Do")])
    result, gql, _ = await _run(set_field_options, params)
    data = result["data"]
    assert data["dry_run"] is True
    assert data["after"] == ["📢 Proposal", "📌 To Do", "🛠 In Progress", "✅ Done"]
    assert data["added"] == ["📢 Proposal"] and data["removed"] == []
    assert _mutations(gql) == []


@pytest.mark.anyio
async def test_apply_preserves_ids_colours_and_refreshes_cache() -> None:
    params = SetFieldOptionsInput(
        options=[OptionSpec(name="To Do"), OptionSpec(name="🧪 Review", color="PURPLE"), OptionSpec(name="Done")],
        dry_run=False,
    )
    result, gql, discovery = await _run(set_field_options, params)
    assert result["ok"] is True
    sent = _mutations(gql)[0]["options"]
    assert sent[0] == {"id": "o1", "name": "To Do", "color": "BLUE", "description": "queued"}
    assert sent[1] == {"name": "🧪 Review", "color": "PURPLE", "description": ""}
    assert sent[2]["id"] == "o3"
    assert sent[3]["id"] == "o2", "unlisted option is kept, not dropped"
    assert discovery.forced == 1


@pytest.mark.anyio
async def test_rename_with_match_keeps_identity() -> None:
    params = SetFieldOptionsInput(options=[OptionSpec(name="🚧 Doing", match="🛠 In Progress")], dry_run=False)
    result, gql, _ = await _run(set_field_options, params)
    assert _mutations(gql)[0]["options"][0] == {"id": "o2", "name": "🚧 Doing", "color": "YELLOW", "description": ""}


@pytest.mark.anyio
async def test_removal_needs_confirm() -> None:
    params = SetFieldOptionsInput(options=[OptionSpec(name="Done")], remove_missing=True, dry_run=False)
    result, gql, _ = await _run(set_field_options, params)
    assert result["ok"] is False and "cannot be undone" in result["message"]
    assert _mutations(gql) == []

    confirmed = params.model_copy(update={"confirm": True})
    result, gql, _ = await _run(set_field_options, confirmed)
    assert result["ok"] is True
    assert result["data"]["removed"] == ["📌 To Do", "🛠 In Progress"]
    assert [o["id"] for o in _mutations(gql)[0]["options"]] == ["o3"]


@pytest.mark.anyio
async def test_duplicate_names_and_non_select_fields_are_rejected() -> None:
    dup = SetFieldOptionsInput(options=[OptionSpec(name="Done"), OptionSpec(name="✅ Done", match="x")])
    result, _, _ = await _run(set_field_options, dup)
    assert result["ok"] is False and "duplicate" in result["message"]
    text = SetFieldOptionsInput(field_name="Title", options=[OptionSpec(name="a")])
    result, _, _ = await _run(set_field_options, text)
    assert result["ok"] is False and "not a single-select" in result["message"]


# ── views ───────────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_list_views_strips_layout_suffix() -> None:
    result, _, _ = await _run(list_project_views, ListProjectViewsInput())
    assert result["data"]["views"][0]["layout"] == "BOARD"
    assert result["data"]["count"] == 1


@pytest.mark.anyio
async def test_create_view_with_filter() -> None:
    params = CreateProjectViewInput(name="Bugs", layout="TABLE", filter="label:bug")
    result, gql, _ = await _run(create_project_view, params)
    create, update = _mutations(gql)
    assert create == {"projectId": "PVT_1", "name": "Bugs", "layout": "TABLE_LAYOUT"}
    assert update == {"viewId": "V2", "filter": "label:bug"}
    assert result["data"]["filter"] == "label:bug"


def test_access_classification() -> None:
    assert classify("list_project_views") is ToolAccess.READ
    assert classify("set_field_options") is ToolAccess.WRITE
    assert classify("create_project_view") is ToolAccess.WRITE


# ── option-name matching used by move_to_status / move_to_done ──────────────


def _field() -> ProjectField:
    return _metadata().fields["Status"]


def test_bare_option_name() -> None:
    assert bare_option_name("✅ Done") == "done"
    assert bare_option_name("🗑️ Trash") == "trash"
    assert bare_option_name("In Progress") == "in progress"


def test_resolve_option_exact_then_bare() -> None:
    service = ProjectService.__new__(ProjectService)
    assert service._resolve_option_id(_field(), "✅ Done") == "o3"
    assert service._resolve_option_id(_field(), "Done") == "o3"
    assert service._resolve_option_id(_field(), "in progress") == "o2"
    with pytest.raises(ValidationError, match="Valid options"):
        service._resolve_option_id(_field(), "Trash")


def test_resolve_option_refuses_ambiguous_bare_match() -> None:
    field = ProjectField(
        id="F", name="Status", data_type="SINGLE_SELECT",
        options=[FieldOption(id="a", name="✅ Done"), FieldOption(id="b", name="☑️ Done")],
    )
    service = ProjectService.__new__(ProjectService)
    with pytest.raises(ValidationError):
        service._resolve_option_id(field, "Done")


# ── suggest_issue_assignee: logins come from configuration ──────────────────


@pytest.mark.anyio
async def test_assignee_suggestion_is_configured_not_hardcoded(monkeypatch) -> None:
    from tools.meta.capability_suite import IssueTextInput, suggest_issue_assignee

    result = await suggest_issue_assignee(IssueTextInput(issue_number=1, text="fix the API"))
    assert result["data"] == {"issue_number": 1, "area": "backend", "suggested_assignee": None, "confidence": "heuristic"}

    monkeypatch.setenv("GH_PROJECT_BACKEND_ASSIGNEE", "octocat")
    get_settings.cache_clear()
    result = await suggest_issue_assignee(IssueTextInput(issue_number=1, text="fix the API"))
    assert result["data"]["suggested_assignee"] == "octocat"
