"""GitHub Actions and check-run tools (issue #32).

Lets an MCP client verify a pull request's CI and recover a workflow run
without falling back to a separate API path (``gh pr checks``, the web UI):

* read  — ``list_workflows``, ``list_workflow_runs``, ``get_workflow_run``,
  ``get_pr_checks``, ``get_job_logs``
* write — ``rerun_workflow_run``, ``dispatch_workflow``

All calls go through the REST API via the factory's ``gh`` runner. Every tool
targets the configured ``GH_PROJECT_ORG_NAME/GH_PROJECT_REPO_NAME`` by default;
an ``owner``/``repo`` override is checked by ``enforce_scope`` first, so with
``GH_PROJECT_SCOPE_LOCK=true`` another repository is refused before any call.

Output is bounded (``limit`` caps list sizes, ``tail_lines`` caps logs) and
job logs are redacted with the shared ``redact_sensitive`` helper.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional
from urllib.parse import urlencode

from pydantic import BaseModel, Field, field_validator

from clients.gh_cli_client import CLIError
from core.access import enforce_scope
from core.config import get_settings
from core.error_handling import build_error_response, handle_tool_error
from core.factory import get_service_factory
from core.hardening import redact_sensitive
from models.responses import ToolSuccess

logger = logging.getLogger(__name__)

MAX_LIST = 100
MAX_LOG_LINES = 2_000

# Check-run conclusions GitHub counts as passing / failing (REST docs).
_PASSING = {"success", "neutral", "skipped"}
_FAILING = {"failure", "cancelled", "timed_out", "action_required", "startup_failure", "stale"}

# CSI / OSC terminal escape sequences (colour codes, hyperlinks) in job logs.
_ANSI_ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))")


# ── Inputs ───────────────────────────────────────────────────────────────────


class _RepoTarget(BaseModel):
    owner: Optional[str] = Field(
        default=None,
        description="Repository owner. Defaults to GH_PROJECT_ORG_NAME (scope-lock checked).",
    )
    repo: Optional[str] = Field(
        default=None,
        description="Repository name. Defaults to GH_PROJECT_REPO_NAME (scope-lock checked).",
    )


class ListWorkflowsInput(_RepoTarget):
    limit: int = Field(default=30, ge=1, le=MAX_LIST, description="Max workflows returned.")


class ListWorkflowRunsInput(_RepoTarget):
    workflow: Optional[str] = Field(
        default=None,
        description="Workflow file name (e.g. 'ci.yaml') or numeric id. Omit for all workflows.",
    )
    branch: Optional[str] = Field(default=None, description="Filter by head branch.")
    event: Optional[str] = Field(default=None, description="Filter by event, e.g. 'pull_request'.")
    status: Optional[str] = Field(
        default=None,
        description="Filter by status or conclusion, e.g. 'in_progress', 'failure', 'success'.",
    )
    head_sha: Optional[str] = Field(default=None, description="Filter by head commit SHA.")
    limit: int = Field(default=20, ge=1, le=MAX_LIST, description="Max runs returned.")


class GetWorkflowRunInput(_RepoTarget):
    run_id: int = Field(..., ge=1, description="Workflow run id.")
    include_jobs: bool = Field(default=True, description="Include jobs and their failed steps.")


class GetPrChecksInput(_RepoTarget):
    pr_number: int = Field(..., ge=1, description="Pull request number.")


class GetJobLogsInput(_RepoTarget):
    job_id: int = Field(..., ge=1, description="Workflow job id (from get_workflow_run).")
    tail_lines: int = Field(
        default=200, ge=1, le=MAX_LOG_LINES, description="Return only the last N log lines."
    )


class RerunWorkflowRunInput(_RepoTarget):
    run_id: int = Field(..., ge=1, description="Workflow run id to re-run.")
    failed_only: bool = Field(
        default=True, description="Re-run only failed jobs (default) instead of the whole run."
    )


class DispatchWorkflowInput(_RepoTarget):
    workflow: str = Field(
        ..., min_length=1, description="Workflow file name (e.g. 'ci.yaml') or numeric id."
    )
    ref: str = Field(..., min_length=1, description="Branch or tag to run the workflow on.")
    inputs: dict[str, str] = Field(
        default_factory=dict,
        description="workflow_dispatch inputs (string values, max 10 keys).",
    )

    @field_validator("inputs")
    @classmethod
    def _bounded_inputs(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > 10:
            raise ValueError("GitHub accepts at most 10 workflow_dispatch inputs")
        return value


# ── Helpers ──────────────────────────────────────────────────────────────────


def _repo_path(target: _RepoTarget) -> str:
    enforce_scope(owner=target.owner, repo=target.repo)
    settings = get_settings()
    return f"repos/{target.owner or settings.org_name}/{target.repo or settings.repo_name}"


async def _get(path: str, query: dict[str, Any] | None = None) -> Any:
    params = {k: v for k, v in (query or {}).items() if v is not None}
    url = f"{path}?{urlencode(params)}" if params else path
    result = await get_service_factory().gh().run(["api", url])
    return json.loads(result.stdout) if result.stdout.strip() else {}


async def _post(path: str, body: dict[str, Any] | None = None) -> None:
    # Fields are sent with -f (always strings); a nested dict such as
    # workflow_dispatch `inputs` is encoded as -f inputs[key]=value.
    args = ["api", "--method", "POST", path]
    for key, value in (body or {}).items():
        if isinstance(value, dict):
            for inner_key, inner_value in value.items():
                args += ["-f", f"{key}[{inner_key}]={inner_value}"]
        else:
            args += ["-f", f"{key}={value}"]
    await get_service_factory().gh().run(args)


def _run_summary(run: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": run.get("id"),
        "name": run.get("name"),
        "workflow_id": run.get("workflow_id"),
        "event": run.get("event"),
        "branch": run.get("head_branch"),
        "head_sha": run.get("head_sha"),
        "status": run.get("status"),
        "conclusion": run.get("conclusion"),
        "attempt": run.get("run_attempt"),
        "created_at": run.get("created_at"),
        "url": run.get("html_url"),
    }


def _check_bucket(status: str | None, conclusion: str | None) -> str:
    if status != "completed":
        return "pending"
    if conclusion in _PASSING:
        return "passed"
    if conclusion in _FAILING:
        return "failed"
    return "pending"


def _status_bucket(state: str | None) -> str:
    return {"success": "passed", "failure": "failed", "error": "failed"}.get(state or "", "pending")


def _cli_error(tool: str, exc: CLIError, suggestion: str) -> dict:
    logger.error("CLI error in %s: %s", tool, redact_sensitive(exc))
    return build_error_response(
        error_type="not_found" if "404" in exc.stderr or "Not Found" in exc.stderr else "internal",
        message=f"{tool} failed: {redact_sensitive(exc.stderr.strip())[:500]}",
        suggestion=suggestion,
    )


_PERMISSION_HINT = (
    "Check the id/name and that the token can read Actions (classic: 'repo'; "
    "fine-grained: Actions read, Checks read, Pull requests read)."
)
_WRITE_HINT = (
    "Re-running or dispatching needs Actions write (classic 'repo' + 'workflow' "
    "scopes; fine-grained: Actions read and write). dispatch_workflow also "
    "requires the workflow to declare a workflow_dispatch trigger on that ref."
)


# ── Read tools ───────────────────────────────────────────────────────────────


async def list_workflows(params: ListWorkflowsInput) -> dict:
    """List the repository's GitHub Actions workflows (id, name, file, state)."""
    try:
        await get_service_factory().ensure_auth()
        data = await _get(f"{_repo_path(params)}/actions/workflows", {"per_page": params.limit})
        workflows = [
            {
                "id": wf.get("id"),
                "name": wf.get("name"),
                "path": wf.get("path"),
                "file": (wf.get("path") or "").rsplit("/", 1)[-1],
                "state": wf.get("state"),
                "url": wf.get("html_url"),
            }
            for wf in data.get("workflows", [])[: params.limit]
        ]
        return ToolSuccess(
            data={"total": data.get("total_count", len(workflows)), "workflows": workflows}
        ).model_dump()
    except CLIError as exc:
        return _cli_error("list_workflows", exc, _PERMISSION_HINT)
    except Exception as exc:
        return handle_tool_error(exc, context="List workflows failed")


async def list_workflow_runs(params: ListWorkflowRunsInput) -> dict:
    """List recent workflow runs, optionally filtered by workflow/branch/event/status/SHA."""
    try:
        await get_service_factory().ensure_auth()
        base = _repo_path(params)
        path = (
            f"{base}/actions/workflows/{params.workflow}/runs"
            if params.workflow
            else f"{base}/actions/runs"
        )
        data = await _get(
            path,
            {
                "branch": params.branch,
                "event": params.event,
                "status": params.status,
                "head_sha": params.head_sha,
                "per_page": params.limit,
            },
        )
        runs = [_run_summary(r) for r in data.get("workflow_runs", [])[: params.limit]]
        return ToolSuccess(
            data={"total": data.get("total_count", len(runs)), "runs": runs}
        ).model_dump()
    except CLIError as exc:
        return _cli_error("list_workflow_runs", exc, _PERMISSION_HINT)
    except Exception as exc:
        return handle_tool_error(exc, context="List workflow runs failed")


async def get_workflow_run(params: GetWorkflowRunInput) -> dict:
    """Get one workflow run and (by default) its jobs with the steps that failed."""
    try:
        await get_service_factory().ensure_auth()
        base = _repo_path(params)
        run = await _get(f"{base}/actions/runs/{params.run_id}")
        data: dict[str, Any] = {"run": _run_summary(run)}
        if params.include_jobs:
            jobs = await _get(f"{base}/actions/runs/{params.run_id}/jobs", {"per_page": MAX_LIST})
            data["jobs"] = [
                {
                    "id": job.get("id"),
                    "name": job.get("name"),
                    "status": job.get("status"),
                    "conclusion": job.get("conclusion"),
                    "url": job.get("html_url"),
                    "failed_steps": [
                        step.get("name")
                        for step in job.get("steps") or []
                        if step.get("conclusion") in _FAILING
                    ],
                }
                for job in jobs.get("jobs", [])
            ]
        return ToolSuccess(data=data).model_dump()
    except CLIError as exc:
        return _cli_error("get_workflow_run", exc, _PERMISSION_HINT)
    except Exception as exc:
        return handle_tool_error(exc, context="Get workflow run failed")


async def get_pr_checks(params: GetPrChecksInput) -> dict:
    """Summarize every check on a PR's head commit: check runs plus commit statuses.

    ``overall`` is ``passed`` only when at least one check exists and none is
    failed or pending; ``none`` means the head commit has no checks at all,
    which should be treated as a blocker, not as green.
    """
    try:
        await get_service_factory().ensure_auth()
        base = _repo_path(params)
        pr = await _get(f"{base}/pulls/{params.pr_number}")
        sha = (pr.get("head") or {}).get("sha")
        if not sha:
            raise ValueError(f"PR #{params.pr_number} has no head commit")
        check_runs = await _get(f"{base}/commits/{sha}/check-runs", {"per_page": MAX_LIST})
        statuses = await _get(f"{base}/commits/{sha}/status", {"per_page": MAX_LIST})

        checks = [
            {
                "name": cr.get("name"),
                "kind": "check_run",
                "status": cr.get("status"),
                "conclusion": cr.get("conclusion"),
                "result": _check_bucket(cr.get("status"), cr.get("conclusion")),
                "url": cr.get("details_url") or cr.get("html_url"),
                "app": (cr.get("app") or {}).get("slug"),
            }
            for cr in check_runs.get("check_runs", [])
        ]
        checks += [
            {
                "name": st.get("context"),
                "kind": "status",
                "status": st.get("state"),
                "conclusion": st.get("state"),
                "result": _status_bucket(st.get("state")),
                "url": st.get("target_url"),
                "app": None,
            }
            for st in statuses.get("statuses", [])
        ]
        counts = {k: sum(1 for c in checks if c["result"] == k) for k in ("passed", "failed", "pending")}
        if not checks:
            overall = "none"
        elif counts["failed"]:
            overall = "failed"
        elif counts["pending"]:
            overall = "pending"
        else:
            overall = "passed"
        return ToolSuccess(
            data={
                "pr_number": params.pr_number,
                "head_sha": sha,
                "state": pr.get("state"),
                "merged": pr.get("merged", False),
                "mergeable_state": pr.get("mergeable_state"),
                "overall": overall,
                "total": len(checks),
                **counts,
                "checks": checks,
            }
        ).model_dump()
    except CLIError as exc:
        return _cli_error("get_pr_checks", exc, _PERMISSION_HINT)
    except Exception as exc:
        return handle_tool_error(exc, context="Get PR checks failed")


async def get_job_logs(params: GetJobLogsInput) -> dict:
    """Return the redacted tail of one workflow job's log (bounded by tail_lines)."""
    try:
        await get_service_factory().ensure_auth()
        # Job logs carry ANSI colour codes; gh refuses to print them unless
        # allowed, so allow them and strip every escape sequence ourselves.
        result = await get_service_factory().gh().run(
            [
                "api",
                "--allow-escape-sequences",
                f"{_repo_path(params)}/actions/jobs/{params.job_id}/logs",
            ]
        )
        lines = _ANSI_ESCAPE.sub("", result.stdout).splitlines()
        tail = lines[-params.tail_lines :]
        return ToolSuccess(
            data={
                "job_id": params.job_id,
                "total_lines": len(lines),
                "returned_lines": len(tail),
                "truncated": len(lines) > len(tail),
                "log": redact_sensitive("\n".join(tail)),
            }
        ).model_dump()
    except CLIError as exc:
        return _cli_error(
            "get_job_logs",
            exc,
            _PERMISSION_HINT
            + " Logs expire after the repository's retention period; very large "
            "logs can exceed GH_PROJECT_MAX_CLI_OUTPUT_CHARS.",
        )
    except Exception as exc:
        return handle_tool_error(exc, context="Get job logs failed")


# ── Write tools ──────────────────────────────────────────────────────────────


async def rerun_workflow_run(params: RerunWorkflowRunInput) -> dict:
    """Re-run a workflow run: only its failed jobs (default) or the whole run."""
    try:
        await get_service_factory().ensure_auth()
        suffix = "rerun-failed-jobs" if params.failed_only else "rerun"
        await _post(f"{_repo_path(params)}/actions/runs/{params.run_id}/{suffix}")
        return ToolSuccess(
            data={
                "run_id": params.run_id,
                "failed_only": params.failed_only,
                "message": f"Re-run requested for run {params.run_id}. "
                "Poll get_workflow_run or get_pr_checks for the new attempt.",
            }
        ).model_dump()
    except CLIError as exc:
        return _cli_error("rerun_workflow_run", exc, _WRITE_HINT)
    except Exception as exc:
        return handle_tool_error(exc, context="Re-run workflow failed")


async def dispatch_workflow(params: DispatchWorkflowInput) -> dict:
    """Trigger a workflow_dispatch run of a workflow on a branch or tag."""
    try:
        await get_service_factory().ensure_auth()
        body: dict[str, Any] = {"ref": params.ref}
        if params.inputs:
            body["inputs"] = params.inputs
        await _post(f"{_repo_path(params)}/actions/workflows/{params.workflow}/dispatches", body)
        return ToolSuccess(
            data={
                "workflow": params.workflow,
                "ref": params.ref,
                "inputs": params.inputs,
                "message": "Dispatch accepted. GitHub creates the run asynchronously; "
                "find it with list_workflow_runs(workflow, branch=ref, event='workflow_dispatch').",
            }
        ).model_dump()
    except CLIError as exc:
        return _cli_error("dispatch_workflow", exc, _WRITE_HINT)
    except Exception as exc:
        return handle_tool_error(exc, context="Dispatch workflow failed")


CI_TOOLS = [
    list_workflows,
    list_workflow_runs,
    get_workflow_run,
    get_pr_checks,
    get_job_logs,
    rerun_workflow_run,
    dispatch_workflow,
]

__all__ = ["CI_TOOLS", *(fn.__name__ for fn in CI_TOOLS)]
