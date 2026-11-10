"""ProjectItem.fields exposes every project field value, including custom fields."""

from services.project_service import ProjectService


def _node(field_values):
    return {
        "id": "ITEM_1",
        "content": {
            "__typename": "Issue",
            "number": 7,
            "title": "Custom fields",
            "body": "",
            "url": "https://github.com/octo-org/octo-repo/issues/7",
            "assignees": {"nodes": []},
            "labels": {"nodes": []},
            "state": "OPEN",
        },
        "fieldValues": {"nodes": field_values},
    }


def _parse(field_values):
    svc = ProjectService(graphql_client=object(), gh_client=object())
    return svc._parse_item_node(_node(field_values))


def test_custom_fields_of_every_type_are_returned_by_name() -> None:
    item = _parse(
        [
            {"field": {"name": "Status"}, "name": "In Progress"},
            {"field": {"name": "Phase"}, "name": "F3"},
            {"field": {"name": "Start"}, "date": "2026-01-05"},
            {"field": {"name": "Estimate (h)"}, "number": 4.0},
            {"field": {"name": "External ticket"}, "text": "https://example.com/t/1"},
            {"field": {"name": "Milestone"}, "milestone": {"title": "Sprint 1"}},
        ]
    )

    assert item is not None
    assert item.fields == {
        "Status": "In Progress",
        "Phase": "F3",
        "Start": "2026-01-05",
        "Estimate (h)": 4.0,
        "External ticket": "https://example.com/t/1",
        "Milestone": "Sprint 1",
    }


def test_dedicated_attributes_are_unchanged() -> None:
    item = _parse(
        [
            {"field": {"name": "Status"}, "name": "Done"},
            {"field": {"name": "Priority"}, "name": "High"},
            {"field": {"name": "Estimate"}, "number": 3},
            {"field": {"name": "Due date"}, "date": "2026-01-09"},
        ]
    )

    assert item is not None
    assert (item.status, item.priority, item.estimate, item.due_date) == ("Done", "High", 3, "2026-01-09")
    assert item.fields["Priority"] == "High"


def test_values_without_field_name_and_empty_nodes_are_ignored() -> None:
    item = _parse([{}, None, {"field": {}, "name": "orphan"}, {"name": "no field"}])

    assert item is not None
    assert item.fields == {}


def test_fields_default_to_empty_dict_when_no_values() -> None:
    item = _parse([])

    assert item is not None
    assert item.fields == {}
