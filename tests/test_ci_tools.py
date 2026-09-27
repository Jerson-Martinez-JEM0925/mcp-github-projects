"""Mocked contract tests for the GitHub Actions / checks tools (issue #32).

Each tool runs against a routing fake ``gh`` runner injected through the
ServiceFactory (no patching, no network). The tests pin the exact REST paths
and query strings sent, the response shaping, the bounds (limit, log tail),
redaction, scope-lock refusal, access classification and error envelopes.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from clients.gh_cli_client import CLIError, CommandResult  # noqa: E402
from core.access import AccessLevel, ToolAccess, classify, is_exposed  # noqa: E402
from core.config import get_settings  # noqa: E402
from core.factory import use_service_factory  # noqa: E402
from tools.ci.actions import (  # noqa: E402
    CI_TOOLS,
    DispatchWorkflowInput,
    GetJobLogsInput,
    GetPrChecksInput,
    GetWorkflowRunInput,
    ListWorkflowRunsInput,
    ListWorkflowsInput,
    RerunWorkflowRunInput,
    dispatch_workflow,
    get_job_logs,
    get_pr_checks,
    get_workflow_run,
    list_workflow_runs,
    list_workflows,
    rerun_workflow_run,
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
    monkeypatch.delenv("GH_PROJECT_SCOPE_LOCK", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class RoutingGH:
    """Fake GHCLIRunner: answers by the REST path (last api arg before flags)."""

    def __init__(self, routes: dict[str, object] | None = None, error: CLIError | None = None):
        self.routes = routes or {}
        self.error = error
        self.calls: list[list[str]] = []

    async def run(self, args: list[str]) -> CommandResult:
        self.calls.append(args)
        if self.error is not None:
            raise self.error
        path = next(a for a in args[1:] if a.startswith("repos/"))
        body = self.routes.get(path.split("?")[0], {})
        text = body if isinstance(body, str) else json.dumps(body)
        return CommandResult(stdout=text, stderr="", return_code=0)

    async def api_graphql(self, query: str, variables: dict) -> dict:
        return {}


async def _with(gh: RoutingGH, coro_fn, params):
    with use_service_factory(gh_client=gh):
        return await coro_fn(params)


# ── Access classification ───────────────────────────────────────────────────


def test_read_tools_are_read_and_write_tools_are_write() -> None:
    reads = {"list_workflows", "list_workflow_runs", "get_workflow_run", "get_pr_checks", "get_job_logs"}
    for fn in CI_TOOLS:
        expected = ToolAccess.READ if fn.__name__ in reads else ToolAccess.WRITE
        assert classify(fn.__name__) is expected, fn.__name__
    assert not is_exposed("dispatch_workflow", AccessLevel.READ)
    assert is_exposed("get_pr_checks", AccessLevel.READ)


# ── Read tools ──────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_list_workflows_shapes_and_bounds() -> None:
    gh = RoutingGH({
        f"{REPO}/actions/workflows": {
            "total_count": 3,
            "workflows": [
                {"id": i, "name": f"W{i}", "path": f".github/workflows/w{i}.yaml", "state": "active"}
                for i in range(3)
            ],
        }
    })
    result = await _with(gh, list_workflows, ListWorkflowsInput(limit=2))
    assert gh.calls == [["api", f"{REPO}/actions/workflows?per_page=2"]]
    assert result["ok"] is True
    assert result["data"]["total"] == 3
    assert [w["file"] for w in result["data"]["workflows"]] == ["w0.yaml", "w1.yaml"]


@pytest.mark.anyio
async def test_list_workflow_runs_filters_and_workflow_path() -> None:
    gh = RoutingGH({f"{REPO}/actions/workflows/ci.yaml/runs": {"total_count": 1, "workflow_runs": [
        {"id": 9, "name": "CI", "head_branch": "feat/x", "head_sha": "abc", "status": "completed",
         "conclusion": "failure", "run_attempt": 2, "event": "pull_request", "html_url": "u"}
    ]}})
    result = await _with(
        gh,
        list_workflow_runs,
        ListWorkflowRunsInput(workflow="ci.yaml", branch="feat/x", status="failure", limit=5),
    )
    assert gh.calls == [[
        "api", f"{REPO}/actions/workflows/ci.yaml/runs?branch=feat%2Fx&status=failure&per_page=5"
    ]]
    run = result["data"]["runs"][0]
    assert (run["id"], run["branch"], run["conclusion"], run["attempt"]) == (9, "feat/x", "failure", 2)


@pytest.mark.anyio
async def test_list_workflow_runs_without_workflow_uses_repo_runs() -> None:
    gh = RoutingGH({f"{REPO}/actions/runs": {"workflow_runs": []}})
    await _with(gh, list_workflow_runs, ListWorkflowRunsInput())
    assert gh.calls == [["api", f"{REPO}/actions/runs?per_page=20"]]


@pytest.mark.anyio
async def test_get_workflow_run_lists_failed_steps() -> None:
    gh = RoutingGH({
        f"{REPO}/actions/runs/5": {"id": 5, "status": "completed", "conclusion": "failure"},
        f"{REPO}/actions/runs/5/jobs": {"jobs": [{
            "id": 77, "name": "Tests", "status": "completed", "conclusion": "failure",
            "steps": [{"name": "setup", "conclusion": "success"}, {"name": "pytest", "conclusion": "failure"}],
        }]},
    })
    result = await _with(gh, get_workflow_run, GetWorkflowRunInput(run_id=5))
    assert result["data"]["run"]["conclusion"] == "failure"
    assert result["data"]["jobs"] == [{
        "id": 77, "name": "Tests", "status": "completed", "conclusion": "failure",
        "url": None, "failed_steps": ["pytest"],
    }]


def _pr_routes(check_runs: list[dict], statuses: list[dict]) -> dict:
    return {
        f"{REPO}/pulls/40": {"state": "open", "head": {"sha": "deadbeef"}, "mergeable_state": "clean"},
        f"{REPO}/commits/deadbeef/check-runs": {"check_runs": check_runs},
        f"{REPO}/commits/deadbeef/status": {"statuses": statuses},
    }


@pytest.mark.anyio
async def test_pr_checks_all_green() -> None:
    gh = RoutingGH(_pr_routes(
        [{"name": "Tests", "status": "completed", "conclusion": "success"},
         {"name": "Lint", "status": "completed", "conclusion": "skipped"}],
        [{"context": "ext/ci", "state": "success"}],
    ))
    data = (await _with(gh, get_pr_checks, GetPrChecksInput(pr_number=40)))["data"]
    assert (data["overall"], data["total"], data["passed"], data["failed"], data["pending"]) == (
        "passed", 3, 3, 0, 0,
    )
    assert data["head_sha"] == "deadbeef"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("runs", "statuses", "overall"),
    [
        ([{"name": "T", "status": "in_progress", "conclusion": None}], [], "pending"),
        ([{"name": "T", "status": "completed", "conclusion": "success"}],
         [{"context": "x", "state": "failure"}], "failed"),
        ([{"name": "T", "status": "completed", "conclusion": "timed_out"}], [], "failed"),
        ([], [], "none"),
    ],
    ids=["pending", "status-failed", "timed-out", "no-checks"],
)
async def test_pr_checks_overall(runs, statuses, overall) -> None:
    gh = RoutingGH(_pr_routes(runs, statuses))
    data = (await _with(gh, get_pr_checks, GetPrChecksInput(pr_number=40)))["data"]
    assert data["overall"] == overall


@pytest.mark.anyio
async def test_job_logs_are_tailed_and_redacted() -> None:
    log = "\n".join([f"line {i}" for i in range(10)] + ["Authorization: Bearer s3cr3t-value"])
    gh = RoutingGH({f"{REPO}/actions/jobs/77/logs": log})
    data = (await _with(gh, get_job_logs, GetJobLogsInput(job_id=77, tail_lines=3)))["data"]
    assert gh.calls == [["api", "--allow-escape-sequences", f"{REPO}/actions/jobs/77/logs"]]
    assert (data["total_lines"], data["returned_lines"], data["truncated"]) == (11, 3, True)
    assert "s3cr3t-value" not in data["log"]
    assert data["log"].startswith("line 8")


@pytest.mark.anyio
async def test_job_logs_strip_terminal_escapes() -> None:
    log = "\x1b[36;1mrun tests\x1b[0m\n\x1b]8;;https://x\x07link\x1b]8;;\x07 done"
    gh = RoutingGH({f"{REPO}/actions/jobs/77/logs": log})
    data = (await _with(gh, get_job_logs, GetJobLogsInput(job_id=77)))["data"]
    assert "\x1b" not in data["log"]
    assert data["log"] == "run tests\nlink done"


def test_bounds_are_enforced() -> None:
    with pytest.raises(ValueError):
        ListWorkflowsInput(limit=101)
    with pytest.raises(ValueError):
        GetJobLogsInput(job_id=1, tail_lines=5000)
    with pytest.raises(ValueError):
        DispatchWorkflowInput(workflow="ci.yaml", ref="main", inputs={str(i): "v" for i in range(11)})


# ── Write tools ─────────────────────────────────────────────────────────────


@pytest.mark.anyio
@pytest.mark.parametrize(("failed_only", "suffix"), [(True, "rerun-failed-jobs"), (False, "rerun")])
async def test_rerun_paths(failed_only: bool, suffix: str) -> None:
    gh = RoutingGH()
    result = await _with(gh, rerun_workflow_run, RerunWorkflowRunInput(run_id=5, failed_only=failed_only))
    assert result["ok"] is True
    assert gh.calls == [["api", "--method", "POST", f"{REPO}/actions/runs/5/{suffix}"]]


@pytest.mark.anyio
async def test_dispatch_encodes_ref_and_inputs() -> None:
    gh = RoutingGH()
    await _with(
        gh,
        dispatch_workflow,
        DispatchWorkflowInput(workflow="release.yaml", ref="main", inputs={"version": "1.1.0"}),
    )
    assert gh.calls == [[
        "api", "--method", "POST", f"{REPO}/actions/workflows/release.yaml/dispatches",
        "-f", "ref=main", "-f", "inputs[version]=1.1.0",
    ]]


# ── Scope lock, overrides and errors ────────────────────────────────────────


@pytest.mark.anyio
async def test_override_repo_is_used_when_unlocked() -> None:
    gh = RoutingGH({"repos/other/thing/actions/workflows": {"workflows": []}})
    await _with(gh, list_workflows, ListWorkflowsInput(owner="other", repo="thing"))
    assert gh.calls[0][1].startswith("repos/other/thing/")


@pytest.mark.anyio
async def test_scope_lock_refuses_foreign_repo_before_any_call(monkeypatch) -> None:
    monkeypatch.setenv("GH_PROJECT_SCOPE_LOCK", "true")
    get_settings.cache_clear()
    gh = RoutingGH()
    result = await _with(gh, dispatch_workflow, DispatchWorkflowInput(
        workflow="ci.yaml", ref="main", repo="elsewhere",
    ))
    assert result["ok"] is False
    assert "GH_PROJECT_SCOPE_LOCK" in result["message"]
    assert gh.calls == []


@pytest.mark.anyio
async def test_cli_error_is_sanitized_and_typed() -> None:
    gh = RoutingGH(error=CLIError("boom", 1, "HTTP 404: Not Found (token=ghp_abcdefghij)"))
    result = await _with(gh, get_workflow_run, GetWorkflowRunInput(run_id=1))
    assert result["ok"] is False
    assert result["error_type"] == "not_found"
    assert "ghp_abcdefghij" not in result["message"]
    assert "Actions read" in result["suggestion"]
