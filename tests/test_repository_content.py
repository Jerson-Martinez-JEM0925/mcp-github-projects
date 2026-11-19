"""Repository contents, PR listing, governed commits, write allowlist and
server instructions. No network: gh / GraphQL are faked."""
from __future__ import annotations

import base64
import importlib
import json
import os

import pytest

from clients.gh_cli_client import CommandResult
from core.config import get_settings
from core.factory import use_service_factory
from tools.pull_requests.listing import (
    ListPullRequestsInput,
    PullRequestDetailInput,
    get_pull_request_detail,
    list_pull_requests,
)
from tools.repository_content import (
    CommitFilesInput,
    CreateBranchInput,
    FileChange,
    GetFileInput,
    ListDirectoryInput,
    SearchCodeInput,
    commit_files,
    create_branch,
    get_repository_file,
    list_repository_directory,
    search_repository_code,
    _apply_hunks,
    _parse_unified_patch,
)

REPO = "repos/octo-org/octo-repo"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def configured_target(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GH_PROJECT_ORG_NAME", "octo-org")
    monkeypatch.setenv("GH_PROJECT_REPO_NAME", "octo-repo")
    monkeypatch.setenv("GH_PROJECT_PROJECT_NUMBER", "7")
    monkeypatch.setenv("GH_PROJECT_WRITE_BRANCH_PREFIXES", "docs/architecture/adr-,chatbot/")
    monkeypatch.setenv("GH_PROJECT_WRITE_PATH_PREFIXES", "docs/")
    monkeypatch.setenv("GH_PROJECT_ALLOWED_GITHUB_WORKFLOW_PATHS", "")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class FakeGH:
    """Answers by the first matching path prefix; records every call."""

    def __init__(self, routes: dict[str, object]):
        self.routes = routes
        self.calls: list[list[str]] = []

    async def run(self, args: list[str]) -> CommandResult:
        self.calls.append(args)
        target = next((a for a in args[1:] if a.startswith(("repos/", "search/"))), "")
        payload: object = {}
        for prefix, value in self.routes.items():
            if target.split("?")[0] == prefix or target.startswith(prefix + "?"):
                payload = value
                break
        text = payload if isinstance(payload, str) else json.dumps(payload)
        return CommandResult(stdout=text, stderr="", return_code=0)

    async def api_graphql(self, query: str, variables: dict) -> dict:
        return {}


class FakeGraphQL:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def execute(self, query, variables=None):
        return await self.execute_with_retry(query, variables)

    async def execute_with_retry(self, query, variables=None, *, is_mutation=False):
        self.calls.append((query, variables or {}))
        return {"createCommitOnBranch": {"commit": {"oid": "c" * 40, "url": "https://github.com/octo-org/octo-repo/commit/ccc"}}}


REPO_META = {REPO: {"default_branch": "main"}}


# ── Read tools ──────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_recursive_listing_filters_by_prefix_and_glob() -> None:
    tree = {"tree": [
        {"path": "docs/architecture/adrs/ADR-0001-governance-repo.md", "type": "blob", "size": 10},
        {"path": "docs/architecture/adr-template.md", "type": "blob", "size": 5},
        {"path": "docs/devops/naming.md", "type": "blob", "size": 5},
        {"path": "src/app.js", "type": "blob", "size": 5},
    ]}
    gh = FakeGH({**REPO_META, f"{REPO}/git/trees/main": tree})
    with use_service_factory(gh_client=gh):
        result = await list_repository_directory(ListDirectoryInput(path="docs/architecture", recursive=True, name_contains="*adr*.md"))
    paths = [e["path"] for e in result["data"]["entries"]]
    assert paths == ["docs/architecture/adr-template.md", "docs/architecture/adrs/ADR-0001-governance-repo.md"]
    assert result["data"]["truncated"] is False
    assert result["data"]["entries"][0]["url"].startswith("https://github.com/octo-org/octo-repo/blob/main/")


@pytest.mark.anyio
async def test_listing_reports_truncation() -> None:
    tree = {"tree": [{"path": f"docs/f{n}.md", "type": "blob"} for n in range(5)]}
    gh = FakeGH({**REPO_META, f"{REPO}/git/trees/main": tree})
    with use_service_factory(gh_client=gh):
        result = await list_repository_directory(ListDirectoryInput(recursive=True, limit=2))
    assert result["data"]["count"] == 2 and result["data"]["matched"] == 5
    assert result["data"]["truncated"] is True


@pytest.mark.anyio
@pytest.mark.parametrize("bad", ["../etc/passwd", "docs/../../x", "docs\\x.md", "docs//x"])
async def test_paths_with_traversal_are_refused(bad: str) -> None:
    gh = FakeGH(REPO_META)
    with use_service_factory(gh_client=gh):
        result = await get_repository_file(GetFileInput(path=bad))
    assert result["ok"] is False
    assert not any("contents" in " ".join(c) for c in gh.calls)


@pytest.mark.anyio
async def test_file_is_paged_by_characters_and_url_encoded() -> None:
    gh = FakeGH({**REPO_META, f"{REPO}/contents/docs/gu%C3%ADa%20GCP.md": "a" * 1200})
    with use_service_factory(gh_client=gh):
        first = await get_repository_file(GetFileInput(path="docs/guía GCP.md", max_chars=500))
        rest = await get_repository_file(GetFileInput(path="docs/guía GCP.md", max_chars=1000, offset=first["data"]["next_offset"]))
    assert first["data"]["has_more"] is True and first["data"]["next_offset"] == 500
    assert len(first["data"]["content"]) == 500
    assert rest["data"]["has_more"] is False and len(rest["data"]["content"]) == 700
    assert "Accept: application/vnd.github.raw+json" in gh.calls[1]


@pytest.mark.anyio
async def test_code_search_is_pinned_to_the_repository() -> None:
    gh = FakeGH({"search/code": {"total_count": 3, "items": [{"path": "docs/a.md", "html_url": "u", "text_matches": [{"fragment": "nomenclatura"}]}]}})
    with use_service_factory(gh_client=gh):
        result = await search_repository_code(SearchCodeInput(query="nomenclatura", path_prefix="docs/devops", extension="md"))
    assert "q=nomenclatura repo:octo-org/octo-repo path:docs/devops extension:md" in gh.calls[0]
    assert result["data"]["has_more"] is True
    assert result["data"]["results"][0]["fragments"] == ["nomenclatura"]


def test_code_search_rejects_scope_qualifiers() -> None:
    with pytest.raises(ValueError):
        SearchCodeInput(query="secret repo:other/repo")


@pytest.mark.anyio
async def test_merged_pull_requests_are_ordered_by_merge_date() -> None:
    items = [
        {"number": 1, "title": "old", "state": "closed", "pull_request": {"merged_at": "2026-01-01T00:00:00Z"}},
        {"number": 2, "title": "new", "state": "closed", "pull_request": {"merged_at": "2026-03-01T00:00:00Z"}},
        {"number": 3, "title": "mid", "state": "closed", "pull_request": {"merged_at": "2026-02-01T00:00:00Z"}},
    ]
    gh = FakeGH({"search/issues": {"total_count": 3, "items": items}})
    with use_service_factory(gh_client=gh):
        result = await list_pull_requests(ListPullRequestsInput(state="merged", per_page=2))
    assert [p["number"] for p in result["data"]["pull_requests"]] == [2, 3]
    assert all(p["state"] == "merged" for p in result["data"]["pull_requests"])
    assert result["data"]["has_more"] is True
    assert "q=repo:octo-org/octo-repo is:pr is:merged" in gh.calls[0]


def test_pull_request_search_rejects_scope_qualifiers() -> None:
    with pytest.raises(ValueError):
        ListPullRequestsInput(query="org:other")


@pytest.mark.anyio
async def test_pull_request_detail_bounds_body_and_lists_files() -> None:
    gh = FakeGH({
        f"{REPO}/pulls/5": {"title": "t", "state": "closed", "merged_at": "2026-01-01", "body": "x" * 500, "changed_files": 1, "user": {"login": "ana"}},
        f"{REPO}/pulls/5/reviews": [{"user": {"login": "bob"}, "state": "APPROVED"}],
        f"{REPO}/pulls/5/files": [{"filename": "docs/a.md", "status": "added", "additions": 3, "deletions": 0}],
    })
    with use_service_factory(gh_client=gh):
        result = await get_pull_request_detail(PullRequestDetailInput(number=5, max_body_chars=200))
    data = result["data"]
    assert data["state"] == "merged" and data["author"] == "ana"
    assert data["reviews_by_user"] == {"bob": "APPROVED"}
    assert data["files"][0]["path"] == "docs/a.md" and data["files_truncated"] is False
    assert data["body_truncated"] is True


# ── Governed writes ─────────────────────────────────────────────────────────


@pytest.mark.anyio
@pytest.mark.parametrize("name", ["main", "feature/x", "docs/architecture/adr-../x", "chatbot/x.lock"])
async def test_create_branch_refuses_default_and_unlisted_names(name: str) -> None:
    gh = FakeGH(REPO_META)
    with use_service_factory(gh_client=gh):
        result = await create_branch(CreateBranchInput(name=name))
    assert result["ok"] is False
    assert not any("--method" in c for c in gh.calls)


@pytest.mark.anyio
async def test_create_branch_from_default_branch_head() -> None:
    gh = FakeGH({**REPO_META, f"{REPO}/git/ref/heads/main": {"object": {"sha": "a" * 40}}, f"{REPO}/git/refs": {"object": {"sha": "a" * 40}}})
    with use_service_factory(gh_client=gh):
        result = await create_branch(CreateBranchInput(name="docs/architecture/adr-0006-x"))
    assert result["ok"] is True
    post = gh.calls[-1]
    assert "ref=refs/heads/docs/architecture/adr-0006-x" in post and f"sha={'a' * 40}" in post


@pytest.mark.anyio
@pytest.mark.parametrize("path", [".github/workflows/ci.yml", "src/app.js", "../docs/x.md"])
async def test_commit_refuses_forbidden_paths(path: str) -> None:
    gql = FakeGraphQL()
    with use_service_factory(gh_client=FakeGH(REPO_META), graphql_client=gql):
        result = await commit_files(CommitFilesInput(branch="chatbot/x", message="docs: x", files=[FileChange(path=path, content="x")]))
    assert result["ok"] is False
    assert gql.calls == []


@pytest.mark.anyio
async def test_commit_allows_exactly_allowlisted_workflow_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GH_PROJECT_WRITE_PATH_PREFIXES", "docs/,.github/workflows/")
    monkeypatch.setenv(
        "GH_PROJECT_ALLOWED_GITHUB_WORKFLOW_PATHS",
        ".github/workflows/example-deploy.yml",
    )
    get_settings.cache_clear()
    gql = FakeGraphQL()
    gh = FakeGH({
        **REPO_META,
        f"{REPO}/git/ref/heads/chatbot/x": {"object": {"sha": "b" * 40}},
    })
    with use_service_factory(gh_client=gh, graphql_client=gql):
        result = await commit_files(CommitFilesInput(
            branch="chatbot/x",
            message="ci(workflows): update the approved example workflow",
            files=[FileChange(
                path=".github/workflows/example-deploy.yml",
                content="name: deploy\n",
            )],
        ))
    assert result["ok"] is True
    assert result["data"]["changed"] == [".github/workflows/example-deploy.yml"]
    assert gql.calls


@pytest.mark.anyio
@pytest.mark.parametrize("path", [
    ".github/workflows/unlisted.yaml",
    ".github/actions/custom/action.yaml",
])
async def test_commit_denies_unlisted_github_paths_even_with_workflow_allowlist(
    path: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GH_PROJECT_WRITE_PATH_PREFIXES", "docs/,.github/workflows/")
    monkeypatch.setenv(
        "GH_PROJECT_ALLOWED_GITHUB_WORKFLOW_PATHS",
        ".github/workflows/example-deploy.yml",
    )
    get_settings.cache_clear()
    gql = FakeGraphQL()
    with use_service_factory(gh_client=FakeGH(REPO_META), graphql_client=gql):
        result = await commit_files(CommitFilesInput(
            branch="chatbot/x",
            message="ci(workflows): test restricted path",
            files=[FileChange(path=path, content="x")],
        ))
    assert result["ok"] is False
    assert gql.calls == []


@pytest.mark.anyio
async def test_commit_refuses_the_default_branch() -> None:
    gql = FakeGraphQL()
    with use_service_factory(gh_client=FakeGH(REPO_META), graphql_client=gql):
        result = await commit_files(CommitFilesInput(branch="main", message="docs: x", files=[FileChange(path="docs/a.md", content="x")]))
    assert result["ok"] is False and gql.calls == []


@pytest.mark.anyio
async def test_commit_sends_base64_changes_on_the_branch_head() -> None:
    gql = FakeGraphQL()
    gh = FakeGH({**REPO_META, f"{REPO}/git/ref/heads/docs/architecture/adr-0006-x": {"object": {"sha": "b" * 40}}})
    with use_service_factory(gh_client=gh, graphql_client=gql):
        result = await commit_files(CommitFilesInput(
            branch="docs/architecture/adr-0006-x",
            message="feat(architecture): ADR-0006 x\n\nCloses #1",
            files=[FileChange(path="docs/architecture/adrs/ADR-0006-x.md", content="# ADR-0006 ñ")],
            deletions=["docs/old.md"],
        ))
    assert result["ok"] is True and result["data"]["commit_sha"] == "c" * 40
    payload = gql.calls[0][1]["input"]
    assert payload["expectedHeadOid"] == "b" * 40
    assert payload["branch"] == {"repositoryNameWithOwner": "octo-org/octo-repo", "branchName": "docs/architecture/adr-0006-x"}
    assert payload["message"] == {"headline": "feat(architecture): ADR-0006 x", "body": "Closes #1"}
    added = payload["fileChanges"]["additions"][0]
    assert base64.b64decode(added["contents"]).decode() == "# ADR-0006 ñ"
    assert payload["fileChanges"]["deletions"] == [{"path": "docs/old.md"}]


@pytest.mark.anyio
async def test_commit_applies_guarded_unified_patch_on_expected_head() -> None:
    branch = "chatbot/patch"
    sha = "b" * 40
    patch = (
        "diff --git a/docs/a.md b/docs/a.md\n"
        "--- a/docs/a.md\n+++ b/docs/a.md\n"
        "@@ -1,2 +1,2 @@\n line one\n-line two\n+updated\n"
        "diff --git a/docs/new.md b/docs/new.md\nnew file mode 100644\n"
        "--- /dev/null\n+++ b/docs/new.md\n@@ -0,0 +1 @@\n+new text\n"
        "diff --git a/docs/old.md b/docs/old.md\ndeleted file mode 100644\n"
        "--- a/docs/old.md\n+++ /dev/null\n@@ -1 +0,0 @@\n-removed\n"
    )
    gh = FakeGH({
        **REPO_META,
        f"{REPO}/git/ref/heads/{branch}": {"object": {"sha": sha}},
        f"{REPO}/contents/docs/a.md": "line one\nline two\n",
        f"{REPO}/contents/docs/old.md": "removed\n",
    })
    gql = FakeGraphQL()
    with use_service_factory(gh_client=gh, graphql_client=gql):
        result = await commit_files(CommitFilesInput(
            branch=branch,
            message="feat(contents): apply patch",
            patch=patch,
            expected_head_sha=sha,
        ))
    assert result["ok"] is True
    additions = gql.calls[0][1]["input"]["fileChanges"]["additions"]
    contents = {row["path"]: base64.b64decode(row["contents"]).decode() for row in additions}
    assert contents == {"docs/a.md": "line one\nupdated\n", "docs/new.md": "new text\n"}
    assert gql.calls[0][1]["input"]["fileChanges"]["deletions"] == [{"path": "docs/old.md"}]


@pytest.mark.anyio
@pytest.mark.parametrize("patch", ["GIT binary patch\n", "diff --git a/docs/a.md b/docs/a.md\nrename from docs/a.md\n"])
async def test_commit_patch_rejects_unsupported_patch_forms(patch: str) -> None:
    gql = FakeGraphQL()
    with use_service_factory(gh_client=FakeGH(REPO_META), graphql_client=gql):
        result = await commit_files(CommitFilesInput(
            branch="chatbot/patch",
            message="fix(contents): invalid patch",
            patch=patch,
            expected_head_sha="b" * 40,
        ))
    assert result["ok"] is False
    assert gql.calls == []


@pytest.mark.anyio
async def test_commit_patch_rejects_stale_head_before_fetching_files() -> None:
    branch = "chatbot/patch"
    patch = "diff --git a/docs/a.md b/docs/a.md\n--- a/docs/a.md\n+++ b/docs/a.md\n@@ -1 +1 @@\n-old\n+new\n"
    gh = FakeGH({**REPO_META, f"{REPO}/git/ref/heads/{branch}": {"object": {"sha": "c" * 40}}})
    gql = FakeGraphQL()
    with use_service_factory(gh_client=gh, graphql_client=gql):
        result = await commit_files(CommitFilesInput(
            branch=branch,
            message="fix(contents): stale patch",
            patch=patch,
            expected_head_sha="b" * 40,
        ))
    assert result["ok"] is False and "differs from expected_head_sha" in result["message"]
    assert gql.calls == []


@pytest.mark.anyio
async def test_commit_patch_refuses_forbidden_paths_before_fetching() -> None:
    branch = "chatbot/patch"
    sha = "b" * 40
    patch = (
        "diff --git a/.github/workflows/x.yml b/.github/workflows/x.yml\n"
        "--- a/.github/workflows/x.yml\n+++ b/.github/workflows/x.yml\n"
        "@@ -1 +1 @@\n-old\n+new\n"
    )
    gh = FakeGH({**REPO_META, f"{REPO}/git/ref/heads/{branch}": {"object": {"sha": sha}}})
    gql = FakeGraphQL()
    with use_service_factory(gh_client=gh, graphql_client=gql):
        result = await commit_files(CommitFilesInput(
            branch=branch,
            message="fix(workflow): refuse",
            patch=patch,
            expected_head_sha=sha,
        ))
    assert result["ok"] is False
    assert gql.calls == []
    assert not any("contents" in " ".join(call) for call in gh.calls)


def test_patch_hunk_can_change_lines_that_look_like_file_headers() -> None:
    patch = (
        "diff --git a/docs/a.md b/docs/a.md\n"
        "--- a/docs/a.md\n+++ b/docs/a.md\n"
        "@@ -1,2 +1,2 @@\n"
        "---- old marker\n-+++ old marker\n"
        "+--- new marker\n++++ new marker\n"
    )
    parsed = _parse_unified_patch(patch)
    result = _apply_hunks("--- old marker\n+++ old marker\n", parsed[0]["hunks"])
    assert result == "--- new marker\n+++ new marker\n"


# ── Registration: write allowlist and instructions ──────────────────────────


def _reload_server(monkeypatch: pytest.MonkeyPatch, **env: str):
    monkeypatch.setenv("GH_PROJECT_OWNER_TYPE", "organization")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    import server

    return importlib.reload(server)


def _names(module) -> set[str]:
    import asyncio

    return {t.name for t in asyncio.run(module.mcp.list_tools())}


def test_write_allowlist_keeps_reads_and_only_listed_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _reload_server(monkeypatch, MCP_ACCESS_LEVEL="write", MCP_WRITE_TOOL_ALLOWLIST="create_branch,commit_files,create_pull_request")
    names = _names(module)
    assert {"create_branch", "commit_files", "create_pull_request"} <= names
    assert {"get_repository_file", "list_pull_requests", "paginated_issue_page"} <= names
    assert "create_repository" not in names and "close_issue" not in names


def test_write_allowlist_does_not_bypass_read_level(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _reload_server(monkeypatch, MCP_ACCESS_LEVEL="read", MCP_WRITE_TOOL_ALLOWLIST="commit_files")
    assert "commit_files" not in _names(module)


def test_write_allowlist_rejects_unknown_names(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="commit_filez"):
        _reload_server(monkeypatch, MCP_WRITE_TOOL_ALLOWLIST="commit_filez")
    monkeypatch.delenv("MCP_WRITE_TOOL_ALLOWLIST")
    _reload_server(monkeypatch)


def test_instructions_default_and_override(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _reload_server(monkeypatch, MCP_ACCESS_LEVEL="read")
    assert "ALWAYS call tools" in module.mcp.instructions
    module = _reload_server(monkeypatch, MCP_ACCESS_LEVEL="read", MCP_SERVER_INSTRUCTIONS="Custom rules.")
    assert module.mcp.instructions == "Custom rules."
    monkeypatch.delenv("MCP_SERVER_INSTRUCTIONS")
    _reload_server(monkeypatch)


def test_instructions_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_SERVER_INSTRUCTIONS", "x" * 4_001)
    get_settings.cache_clear()
    with pytest.raises(Exception):
        get_settings()
    os.environ.pop("MCP_SERVER_INSTRUCTIONS", None)
    get_settings.cache_clear()
