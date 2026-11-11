"""list_sub_issues follows the GraphQL cursor and returns every sub-issue."""

import pytest

import tools.issues.advanced_operations as ops
from tools.issues.advanced_operations import ListSubIssuesInput, list_sub_issues


class _Result:
    def __init__(self, stdout: str) -> None:
        self.stdout = stdout


class _FakeGH:
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    async def run(self, _argv):
        return _Result("I_parent\n")

    async def api_graphql(self, query, variables):
        self.calls.append(dict(variables))
        return self.pages.pop(0)


class _Factory:
    def __init__(self, gh):
        self._gh = gh

    async def ensure_auth(self):
        return None

    def gh(self):
        return self._gh


class _Settings:
    org_name = "octo-org"
    repo_name = "octo-repo"


def _page(numbers, has_next, cursor=None, total=None):
    return {
        "data": {
            "node": {
                "subIssues": {
                    "totalCount": total,
                    "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
                    "nodes": [
                        {"number": n, "title": f"Task {n}", "state": "OPEN", "assignees": {"nodes": [{"login": "octocat"}]}}
                        for n in numbers
                    ],
                }
            }
        }
    }


@pytest.fixture
def patch(monkeypatch):
    def _apply(gh):
        monkeypatch.setattr(ops, "get_service_factory", lambda: _Factory(gh))
        monkeypatch.setattr(ops, "get_settings", lambda: _Settings())
        return gh
    return _apply


@pytest.mark.asyncio
async def test_follows_cursor_across_pages(patch) -> None:
    gh = patch(_FakeGH([
        _page(range(1, 101), True, "CUR1", total=130),
        _page(range(101, 131), False, None, total=130),
    ]))

    result = await list_sub_issues(ListSubIssuesInput(issue_number=7))

    data = result["data"]
    assert [s["number"] for s in data["sub_issues"]] == list(range(1, 131))
    assert data["count"] == 130 and data["total_count"] == 130
    assert "after" not in gh.calls[0] and gh.calls[1]["after"] == "CUR1"


@pytest.mark.asyncio
async def test_single_page_keeps_previous_shape(patch) -> None:
    patch(_FakeGH([_page([3, 4], False, None, total=2)]))

    data = (await list_sub_issues(ListSubIssuesInput(issue_number=7)))["data"]

    assert data["parent_issue"] == 7
    assert data["count"] == 2 and data["total_count"] == 2
    assert data["sub_issues"][0] == {"number": 3, "title": "Task 3", "state": "OPEN", "assignees": ["octocat"]}


@pytest.mark.asyncio
async def test_no_sub_issues(patch) -> None:
    patch(_FakeGH([{"data": {"node": {"subIssues": None}}}]))

    data = (await list_sub_issues(ListSubIssuesInput(issue_number=7)))["data"]

    assert data["sub_issues"] == [] and data["count"] == 0
