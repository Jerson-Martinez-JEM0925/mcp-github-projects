"""GitHub Actions and check-run tools (issue #32).

Lets an MCP client verify a pull request's CI and recover a workflow run
without falling back to a separate API path (``gh pr checks``, the web UI):

* read  — ``list_workflows``, ``list_workflow_runs``, ``get_workflow_run``,
  ``get_pr_checks``, ``get_job_logs``
* write — ``rerun_workflow_run``, ``dispatch_workflow``, ``merge_pull_request``

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
from typing import Any, Literal, Optional
from urllib.parse import urlencode

from pydantic import BaseModel, Field, field_validator

from clients.gh_cli_client import CLIError
from clients.resource_cache import get_or_load, scoped_key
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


class CancelWorkflowRunInput(_RepoTarget):
    run_id: int = Field(..., ge=1, description="Workflow run id to cancel.")


class ListWorkflowArtifactsInput(_RepoTarget):
    run_id: int = Field(..., ge=1, description="Workflow run id whose artifacts are listed.")
    limit: int = Field(default=30, ge=1, le=MAX_LIST, description="Max artifacts returned.")


class ListEnvironmentsInput(_RepoTarget):
    limit: int = Field(default=30, ge=1, le=MAX_LIST, description="Max environments returned.")


class ListDeploymentsInput(_RepoTarget):
    environment: Optional[str] = Field(default=None, max_length=255, description="Filter by environment name.")
    ref: Optional[str] = Field(default=None, max_length=255, description="Filter by branch, tag, or SHA.")
    sha: Optional[str] = Field(default=None, min_length=7, max_length=40, description="Filter by commit SHA.")
    limit: int = Field(default=20, ge=1, le=MAX_LIST, description="Max deployments returned.")


class ListReleasesInput(_RepoTarget):
    limit: int = Field(default=20, ge=1, le=MAX_LIST, description="Max releases returned.")


class CreateReleaseInput(_RepoTarget):
    tag_name: str = Field(..., min_length=1, max_length=255, description="Git tag name for the release.")
    target_commitish: Optional[str] = Field(
        default=None, max_length=255, description="Branch or commit SHA the tag targets (default branch when omitted)."
    )
    name: Optional[str] = Field(default=None, max_length=255, description="Release title.")
    body: str = Field(default="", max_length=125_000, description="Release notes Markdown.")
    draft: bool = Field(default=False, description="Create an unpublished draft release.")
    prerelease: bool = Field(default=False, description="Mark the release as a prerelease.")
    generate_release_notes: bool = Field(
        default=False, description="Ask GitHub to generate release notes; explicit body is still returned separately."
    )

    @field_validator("tag_name")
    @classmethod
    def _safe_tag(cls, value: str) -> str:
        return DispatchWorkflowInput._safe_git_ref(value)


class CreateDeploymentInput(_RepoTarget):
    ref: str = Field(..., min_length=1, max_length=255, description="Branch, tag, or SHA to deploy.")
    environment: str = Field(..., min_length=1, max_length=255, description="Deployment environment name.")
    task: str = Field(default="deploy", min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$", description="Deployment task.")
    description: str = Field(default="", max_length=512, description="Human-readable deployment description.")
    auto_merge: bool = Field(
        default=False,
        description="Allow GitHub to merge the default branch into ref. Disabled by default to avoid hidden merges.",
    )
    transient_environment: bool = Field(default=False, description="Whether the environment is short-lived.")
    production_environment: Optional[bool] = Field(
        default=None, description="Optional explicit production marker; omitted means GitHub infers it from the environment."
    )

    @field_validator("ref")
    @classmethod
    def _safe_deployment_ref(cls, value: str) -> str:
        return DispatchWorkflowInput._safe_git_ref(value)


class DispatchWorkflowInput(_RepoTarget):
    workflow: str = Field(
        ..., min_length=1, max_length=128,
        description="Workflow file name (e.g. 'ci.yaml') or numeric id.",
    )
    ref: str = Field(..., min_length=1, max_length=255, description="Branch or tag to run the workflow on.")
    inputs: dict[str, str] = Field(
        default_factory=dict,
        description="workflow_dispatch inputs (string values, max 10 keys).",
    )

    @field_validator("workflow")
    @classmethod
    def _safe_workflow_id(cls, value: str) -> str:
        if not re.fullmatch(r"(?:[0-9]+|[A-Za-z0-9_.-]+\.ya?ml)", value):
            raise ValueError("workflow must be a numeric id or a .yml/.yaml filename")
        return value

    @field_validator("ref")
    @classmethod
    def _safe_git_ref(cls, value: str) -> str:
        forbidden = set(" ~^:?*[\\")
        if (
            value.startswith(("/", "."))
            or value.endswith(("/", ".", ".lock"))
            or "//" in value
            or ".." in value
            or "@{" in value
            or any(ord(char) < 32 or char in forbidden for char in value)
            or any(part.startswith(".") for part in value.split("/"))
        ):
            raise ValueError("ref must be a valid branch or tag name")
        return value

    @field_validator("inputs")
    @classmethod
    def _bounded_inputs(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > 10:
            raise ValueError("GitHub accepts at most 10 workflow_dispatch inputs")
        if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,99}", key) for key in value):
            raise ValueError("workflow input names must be 1-100 safe identifier characters")
        if any(len(item) > 10_000 for item in value.values()) or sum(map(len, value.values())) > 20_000:
            raise ValueError("workflow input values exceed the 20,000-character limit")
        return value


class MergePullRequestInput(_RepoTarget):
    pull_number: int = Field(..., ge=1, description="Pull request number to merge.")
    expected_head_sha: str = Field(
        ...,
        min_length=40,
        max_length=40,
        pattern=r"^[0-9a-fA-F]{40}$",
        description="Exact current head SHA; stale requests are refused.",
    )
    merge_method: Literal["merge", "squash", "rebase"] = Field(
        ..., description="GitHub merge method to use."
    )


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


async def _post_json(path: str, body: dict[str, Any]) -> Any:
    """POST a JSON body and parse the JSON response.

    Booleans/null use ``-F`` so both gh and the direct REST fast path encode
    them as typed JSON rather than strings.
    """
    args = ["api", "--method", "POST", path]
    for key, value in body.items():
        if value is None:
            args += ["-F", f"{key}=null"]
        elif isinstance(value, bool):
            args += ["-F", f"{key}={'true' if value else 'false'}"]
        else:
            args += ["-f", f"{key}={value}"]
    result = await get_service_factory().gh().run(args)
    return json.loads(result.stdout) if result.stdout.strip() else {}


async def _put(path: str, body: dict[str, Any]) -> Any:
    args = ["api", "--method", "PUT", path]
    for key, value in body.items():
        args += ["-f", f"{key}={value}"]
    result = await get_service_factory().gh().run(args)
    return json.loads(result.stdout) if result.stdout.strip() else {}


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


def _environment_summary(environment: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": environment.get("id"),
        "name": environment.get("name"),
        "url": environment.get("html_url"),
        "created_at": environment.get("created_at"),
        "updated_at": environment.get("updated_at"),
        "protection_rules": len(environment.get("protection_rules") or []),
        "deployment_branch_policy": environment.get("deployment_branch_policy"),
    }


def _deployment_summary(deployment: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": deployment.get("id"),
        "sha": deployment.get("sha"),
        "ref": deployment.get("ref"),
        "task": deployment.get("task"),
        "environment": deployment.get("environment"),
        "description": deployment.get("description"),
        "state": deployment.get("state"),
        "transient_environment": deployment.get("transient_environment"),
        "production_environment": deployment.get("production_environment"),
        "created_at": deployment.get("created_at"),
        "updated_at": deployment.get("updated_at"),
        "url": deployment.get("url"),
    }


def _release_summary(release: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": release.get("id"),
        "tag_name": release.get("tag_name"),
        "target_commitish": release.get("target_commitish"),
        "name": release.get("name"),
        "draft": release.get("draft"),
        "prerelease": release.get("prerelease"),
        "created_at": release.get("created_at"),
        "published_at": release.get("published_at"),
        "url": release.get("html_url"),
        "assets": len(release.get("assets") or []),
    }


def _artifact_summary(artifact: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": artifact.get("id"),
        "name": artifact.get("name"),
        "size_in_bytes": artifact.get("size_in_bytes"),
        "expired": artifact.get("expired"),
        "created_at": artifact.get("created_at"),
        "expires_at": artifact.get("expires_at"),
        "archive_download_url": artifact.get("archive_download_url"),
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
_MERGE_HINT = (
    "Merging needs Pull requests write plus Actions/checks read. GitHub "
    "repository rules and required reviews still apply; the MCP never bypasses them."
)


# ── Read tools ───────────────────────────────────────────────────────────────


async def list_workflows(params: ListWorkflowsInput) -> dict:
    """List the repository's GitHub Actions workflows (id, name, file, state)."""
    try:
        await get_service_factory().ensure_auth()
        repo_path = _repo_path(params)

        async def load_workflows() -> dict:
            return await _get(f"{repo_path}/actions/workflows", {"per_page": params.limit})

        ttl = get_settings().resource_cache_ttl_seconds
        key = await scoped_key(get_service_factory(), "workflows", repo_path, str(params.limit))
        if ttl and key is not None:
            data, _cache_hit = await get_or_load(key, ttl_seconds=ttl, loader=load_workflows)
        else:
            data = await load_workflows()
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


async def cancel_workflow_run(params: CancelWorkflowRunInput) -> dict:
    """Cancel a workflow run that is queued or in progress."""
    try:
        await _post(f"{_repo_path(params)}/actions/runs/{params.run_id}/cancel")
        return ToolSuccess(
            data={
                "run_id": params.run_id,
                "cancellation_requested": True,
                "message": "Cancellation accepted by GitHub; poll get_workflow_run for the final state.",
            }
        ).model_dump()
    except CLIError as exc:
        return _cli_error("cancel_workflow_run", exc, _WRITE_HINT)
    except Exception as exc:
        return handle_tool_error(exc, context="Cancel workflow run failed")


async def list_workflow_artifacts(params: ListWorkflowArtifactsInput) -> dict:
    """List artifacts produced by one workflow run (metadata and download URLs)."""
    try:
        data = await _get(
            f"{_repo_path(params)}/actions/runs/{params.run_id}/artifacts",
            {"per_page": params.limit},
        )
        artifacts = [_artifact_summary(a) for a in data.get("artifacts", [])[: params.limit]]
        return ToolSuccess(
            data={"run_id": params.run_id, "total": data.get("total_count", len(artifacts)), "artifacts": artifacts}
        ).model_dump()
    except CLIError as exc:
        return _cli_error("list_workflow_artifacts", exc, _PERMISSION_HINT)
    except Exception as exc:
        return handle_tool_error(exc, context="List workflow artifacts failed")


async def list_environments(params: ListEnvironmentsInput) -> dict:
    """List deployment environments and summarized protection-rule counts."""
    try:
        data = await _get(f"{_repo_path(params)}/environments", {"per_page": params.limit})
        environments = [_environment_summary(e) for e in data.get("environments", [])[: params.limit]]
        return ToolSuccess(
            data={"total": data.get("total_count", len(environments)), "environments": environments}
        ).model_dump()
    except CLIError as exc:
        return _cli_error("list_environments", exc, _PERMISSION_HINT)
    except Exception as exc:
        return handle_tool_error(exc, context="List environments failed")


async def list_deployments(params: ListDeploymentsInput) -> dict:
    """List deployments, optionally filtered by environment, ref, or SHA."""
    try:
        data = await _get(
            f"{_repo_path(params)}/deployments",
            {
                "environment": params.environment,
                "ref": params.ref,
                "sha": params.sha,
                "per_page": params.limit,
            },
        )
        deployments = [_deployment_summary(d) for d in (data if isinstance(data, list) else [])[: params.limit]]
        return ToolSuccess(data={"total": len(deployments), "deployments": deployments}).model_dump()
    except CLIError as exc:
        return _cli_error("list_deployments", exc, _PERMISSION_HINT)
    except Exception as exc:
        return handle_tool_error(exc, context="List deployments failed")


async def list_releases(params: ListReleasesInput) -> dict:
    """List repository releases with tag, publication state, and asset counts."""
    try:
        data = await _get(f"{_repo_path(params)}/releases", {"per_page": params.limit})
        releases = [_release_summary(r) for r in (data if isinstance(data, list) else [])[: params.limit]]
        return ToolSuccess(data={"total": len(releases), "releases": releases}).model_dump()
    except CLIError as exc:
        return _cli_error("list_releases", exc, _PERMISSION_HINT)
    except Exception as exc:
        return handle_tool_error(exc, context="List releases failed")


async def create_release(params: CreateReleaseInput) -> dict:
    """Create a GitHub release for a tag, optionally as draft/prerelease."""
    try:
        body: dict[str, Any] = {
            "tag_name": params.tag_name,
            "body": params.body,
            "draft": params.draft,
            "prerelease": params.prerelease,
            "generate_release_notes": params.generate_release_notes,
        }
        if params.target_commitish:
            body["target_commitish"] = params.target_commitish
        if params.name:
            body["name"] = params.name
        release = await _post_json(f"{_repo_path(params)}/releases", body)
        if not release.get("id") or not release.get("html_url"):
            return build_error_response(
                error_type="internal",
                message="GitHub returned an incomplete release response; reconcile with list_releases before retrying.",
                suggestion="Release creation is not safely replayable after an ambiguous transport response.",
            )
        return ToolSuccess(data={"release": _release_summary(release)}).model_dump()
    except CLIError as exc:
        return _cli_error("create_release", exc, _WRITE_HINT)
    except Exception as exc:
        return handle_tool_error(exc, context="Create release failed")


async def create_deployment(params: CreateDeploymentInput) -> dict:
    """Create a deployment for a validated ref without bypassing required contexts."""
    try:
        body: dict[str, Any] = {
            "ref": params.ref,
            "environment": params.environment,
            "task": params.task,
            "description": params.description,
            "auto_merge": params.auto_merge,
            "transient_environment": params.transient_environment,
        }
        if params.production_environment is not None:
            body["production_environment"] = params.production_environment
        deployment = await _post_json(f"{_repo_path(params)}/deployments", body)
        if not deployment.get("id"):
            return build_error_response(
                error_type="internal",
                message="GitHub returned an incomplete deployment response; reconcile with list_deployments before retrying.",
                suggestion="Deployment creation is not safely replayable after an ambiguous transport response.",
            )
        return ToolSuccess(data={"deployment": _deployment_summary(deployment)}).model_dump()
    except CLIError as exc:
        return _cli_error("create_deployment", exc, _WRITE_HINT)
    except Exception as exc:
        return handle_tool_error(exc, context="Create deployment failed")


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
                "input_names": sorted(params.inputs),
                "message": "Dispatch accepted. GitHub creates the run asynchronously; "
                "find it with list_workflow_runs(workflow, branch=ref, event='workflow_dispatch').",
            }
        ).model_dump()
    except CLIError as exc:
        return _cli_error("dispatch_workflow", exc, _WRITE_HINT)
    except Exception as exc:
        return handle_tool_error(exc, context="Dispatch workflow failed")


async def merge_pull_request(params: MergePullRequestInput) -> dict:
    """Merge an open PR only when its exact head and all observed checks are ready.

    GitHub remains the final authority for required reviews and branch
    protection; this tool never requests an admin bypass.
    """
    try:
        await get_service_factory().ensure_auth()
        base = _repo_path(params)
        pr = await _get(f"{base}/pulls/{params.pull_number}")
        if pr.get("state") != "open" or pr.get("merged"):
            return build_error_response(
                error_type="validation",
                message=f"PR #{params.pull_number} is not open.",
                suggestion="Only open, unmerged pull requests can be merged.",
            )
        if pr.get("draft"):
            return build_error_response(
                error_type="validation",
                message=f"PR #{params.pull_number} is a draft.",
                suggestion="Mark the pull request ready for review before merging.",
            )

        head_sha = (pr.get("head") or {}).get("sha")
        if head_sha != params.expected_head_sha:
            return build_error_response(
                error_type="validation",
                message="The pull request head changed since it was reviewed.",
                suggestion=f"Read the latest PR details and retry with its current head SHA ({head_sha or 'unavailable'}).",
            )
        if pr.get("mergeable") is not True or pr.get("mergeable_state") != "clean":
            return build_error_response(
                error_type="validation",
                message=f"PR #{params.pull_number} is not confirmed mergeable.",
                suggestion="Resolve conflicts and wait for GitHub to calculate a clean mergeable state.",
            )

        checks_result = await get_pr_checks(
            GetPrChecksInput(owner=params.owner, repo=params.repo, pr_number=params.pull_number)
        )
        if not checks_result.get("ok"):
            return checks_result
        checks = checks_result["data"]
        if checks.get("head_sha") != params.expected_head_sha:
            return build_error_response(
                error_type="validation",
                message="The PR head changed while merge preconditions were checked.",
                suggestion="Read the latest PR details and retry with its current head SHA.",
            )
        if checks.get("overall") != "passed":
            return build_error_response(
                error_type="validation",
                message=f"PR checks are {checks.get('overall', 'unknown')}; merge was not attempted.",
                suggestion="Wait for every check to pass, then retry. Missing checks are not treated as success.",
            )

        result = await _put(
            f"{base}/pulls/{params.pull_number}/merge",
            {"sha": params.expected_head_sha, "merge_method": params.merge_method},
        )
        if result.get("merged") is not True:
            return build_error_response(
                error_type="validation",
                message="GitHub did not confirm that the pull request was merged.",
                suggestion="Check the PR state and repository branch protections before retrying.",
            )
        return ToolSuccess(
            data={
                "pull_number": params.pull_number,
                "merged": True,
                "merge_method": params.merge_method,
                "head_sha": params.expected_head_sha,
                "merge_commit_sha": result.get("sha"),
                "url": pr.get("html_url"),
                "message": result.get("message", "Pull request merged."),
            }
        ).model_dump()
    except CLIError as exc:
        if any(code in exc.stderr for code in ("HTTP 405", "HTTP 409", "HTTP 422")):
            return build_error_response(
                error_type="validation",
                message="GitHub refused the merge; repository protections or current PR state may have changed.",
                suggestion="Re-read the PR and checks. This tool never bypasses repository protections.",
            )
        return _cli_error("merge_pull_request", exc, _MERGE_HINT)
    except Exception as exc:
        return handle_tool_error(exc, context="Merge pull request failed")


CI_TOOLS = [
    list_workflows,
    list_workflow_runs,
    get_workflow_run,
    get_pr_checks,
    get_job_logs,
    rerun_workflow_run,
    cancel_workflow_run,
    list_workflow_artifacts,
    list_environments,
    list_deployments,
    create_deployment,
    list_releases,
    create_release,
    dispatch_workflow,
    merge_pull_request,
]

__all__ = ["CI_TOOLS", *(fn.__name__ for fn in CI_TOOLS)]
