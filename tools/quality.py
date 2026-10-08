"""Read-only GitHub response-quality and automation diagnostics.

These tools intentionally return bounded, derived views instead of changing
existing tool envelopes. They make repository synchronization workers safer by
making completeness, risk, and lifecycle state explicit.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from core.config import get_settings
from core.error_handling import build_error_response, handle_tool_error
from core.factory import get_service_factory
from core.hardening import bounded_text, parse_json_array, parse_json_object
from models.responses import ToolSuccess

MAX_ITEMS = 100


class RepoHealthInput(BaseModel):
    include_items: bool = Field(default=True, description="Include a bounded sample of open issues.")
    limit: int = Field(default=30, ge=1, le=MAX_ITEMS, description="Maximum issues sampled.")


class NumberInput(BaseModel):
    number: int = Field(ge=1, description="GitHub issue or pull request number.")


class RunInput(BaseModel):
    run_id: int = Field(ge=1, description="GitHub Actions workflow run id.")


class PageInput(BaseModel):
    query: str = Field(default="", max_length=500, description="Optional GitHub issue search query.")
    state: str = Field(default="open", pattern=r"^(open|closed|all)$")
    page: int = Field(default=1, ge=1, le=100, description="1-based page number.")
    per_page: int = Field(default=30, ge=1, le=MAX_ITEMS, description="Items per page.")


class ConsistencyInput(BaseModel):
    limit: int = Field(default=100, ge=1, le=MAX_ITEMS, description="Maximum issues inspected.")


async def _api(path: str, *extra: str) -> Any:
    result = await get_service_factory().gh().run(["api", path, *extra])
    return json.loads(result.stdout) if result.stdout.strip() else {}


def _repo() -> str:
    settings = get_settings()
    return f"{settings.org_name}/{settings.repo_name}"


def _row(issue: dict[str, Any]) -> dict[str, Any]:
    return {
        "number": issue.get("number"),
        "title": bounded_text(issue.get("title", ""), 256),
        "state": issue.get("state"),
        "labels": [x.get("name") for x in issue.get("labels", []) if isinstance(x, dict)],
        "assignees": [x.get("login") for x in issue.get("assignees", []) if isinstance(x, dict)],
        "milestone": (issue.get("milestone") or {}).get("title"),
        "updated_at": issue.get("updated_at"),
        "url": issue.get("html_url"),
    }


def _age_days(timestamp: str | None) -> int | None:
    if not timestamp:
        return None
    try:
        value = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        return max(0, (datetime.now(timezone.utc) - value).days)
    except ValueError:
        return None


async def repository_health_summary(params: RepoHealthInput) -> dict:
    """Summarize repository visibility, defaults, open work, and stale work."""
    try:
        await get_service_factory().ensure_auth()
        repo = await _api(f"repos/{_repo()}")
        open_issues = await _api(f"repos/{_repo()}/issues?state=open&per_page={params.limit}")
        issues = [x for x in open_issues if "pull_request" not in x]
        missing = {"assignee": 0, "labels": 0, "milestone": 0}
        for issue in issues:
            if not issue.get("assignees"): missing["assignee"] += 1
            if not issue.get("labels"): missing["labels"] += 1
            if not issue.get("milestone"): missing["milestone"] += 1
        data = {"repository": _repo(), "visibility": repo.get("visibility"), "default_branch": repo.get("default_branch"), "archived": repo.get("archived"), "open_issue_count": len(issues), "metadata_gaps": missing, "sample": [_row(x) for x in issues] if params.include_items else []}
        return ToolSuccess(data=data).model_dump()
    except Exception as exc:
        return handle_tool_error(exc, context="Repository health summary failed")


async def pull_request_lifecycle_summary(params: NumberInput) -> dict:
    """Return mergeability, review, check, and age metadata for one pull request."""
    try:
        await get_service_factory().ensure_auth()
        pr = await _api(f"repos/{_repo()}/pulls/{params.number}")
        reviews_raw = await _api(f"repos/{_repo()}/pulls/{params.number}/reviews?per_page=100")
        if isinstance(reviews_raw, list):
            reviews = reviews_raw
        elif isinstance(reviews_raw, dict) and isinstance(reviews_raw.get("reviews"), list):
            reviews = reviews_raw["reviews"]
        else:
            reviews = []
        return ToolSuccess(data={"number": params.number, "url": pr.get("html_url"), "state": pr.get("state"), "draft": pr.get("draft", False), "merged": pr.get("merged", False), "mergeable": pr.get("mergeable"), "mergeable_state": pr.get("mergeable_state"), "head_sha": (pr.get("head") or {}).get("sha"), "head_ref": (pr.get("head") or {}).get("ref"), "base": (pr.get("base") or {}).get("ref"), "review_states": {state: sum(1 for review in reviews if review.get("state") == state) for state in ("APPROVED", "CHANGES_REQUESTED", "COMMENTED")}, "age_days": _age_days(pr.get("created_at")), "updated_age_days": _age_days(pr.get("updated_at"))}).model_dump()
    except Exception as exc:
        return handle_tool_error(exc, context="Pull request lifecycle summary failed")


async def issue_activity_digest(params: NumberInput) -> dict:
    """Summarize issue comments and timeline activity without returning unbounded text."""
    try:
        await get_service_factory().ensure_auth()
        issue = await _api(f"repos/{_repo()}/issues/{params.number}")
        comments = await _api(f"repos/{_repo()}/issues/{params.number}/comments?per_page=100")
        authors: dict[str, int] = {}
        for comment in comments:
            login = (comment.get("user") or {}).get("login", "unknown")
            authors[login] = authors.get(login, 0) + 1
        latest = max((x.get("updated_at", "") for x in comments), default="")
        return ToolSuccess(data={"issue": _row(issue), "comment_count": len(comments), "comment_authors": authors, "latest_comment_at": latest, "has_activity": bool(comments)}).model_dump()
    except Exception as exc:
        return handle_tool_error(exc, context="Issue activity digest failed")


async def workflow_run_diagnostic_summary(params: RunInput) -> dict:
    """Summarize a workflow run with failed jobs and failed step names."""
    try:
        await get_service_factory().ensure_auth()
        run = await _api(f"repos/{_repo()}/actions/runs/{params.run_id}")
        jobs = await _api(f"repos/{_repo()}/actions/runs/{params.run_id}/jobs?per_page=100")
        rows = []
        for job in jobs.get("jobs", []):
            failed = [step.get("name") for step in job.get("steps") or [] if step.get("conclusion") not in (None, "success", "skipped", "neutral")]
            rows.append({"id": job.get("id"), "name": job.get("name"), "status": job.get("status"), "conclusion": job.get("conclusion"), "failed_steps": failed})
        return ToolSuccess(data={"run_id": params.run_id, "status": run.get("status"), "conclusion": run.get("conclusion"), "head_sha": run.get("head_sha"), "attempt": run.get("run_attempt"), "failed_jobs": sum(1 for row in rows if row["conclusion"] not in (None, "success", "skipped", "neutral")), "jobs": rows}).model_dump()
    except Exception as exc:
        return handle_tool_error(exc, context="Workflow run diagnostic summary failed")


async def check_conclusion_summary(params: NumberInput) -> dict:
    """Aggregate check-run conclusions for a pull request head and never treat no checks as green."""
    try:
        await get_service_factory().ensure_auth()
        pr = await _api(f"repos/{_repo()}/pulls/{params.number}")
        sha = (pr.get("head") or {}).get("sha")
        checks = await _api(f"repos/{_repo()}/commits/{sha}/check-runs?per_page=100")
        counts: dict[str, int] = {}
        for check in checks.get("check_runs", []):
            key = check.get("conclusion") or check.get("status") or "unknown"
            counts[key] = counts.get(key, 0) + 1
        blocking = [name for name in ("failure", "cancelled", "timed_out", "action_required") if counts.get(name)]
        overall = "none" if not counts else ("failed" if blocking else ("pending" if counts.get("queued", 0) + counts.get("in_progress", 0) else "passed"))
        return ToolSuccess(data={"pr_number": params.number, "head_sha": sha, "overall": overall, "counts": counts, "blocking_conclusions": blocking, "checked": bool(counts)}).model_dump()
    except Exception as exc:
        return handle_tool_error(exc, context="Check conclusion summary failed")


async def issue_metadata_consistency_report(params: ConsistencyInput) -> dict:
    """Find open issues missing one or more fields needed for reliable automation."""
    try:
        await get_service_factory().ensure_auth()
        raw = await _api(f"repos/{_repo()}/issues?state=open&per_page={params.limit}")
        issues = [x for x in raw if "pull_request" not in x]
        gaps = []
        for issue in issues:
            missing = []
            if not issue.get("title", "").strip(): missing.append("title")
            if not issue.get("body", "").strip(): missing.append("body")
            if not issue.get("labels"): missing.append("labels")
            if not issue.get("assignees"): missing.append("assignee")
            if missing: gaps.append({"issue": _row(issue), "missing": missing})
        return ToolSuccess(data={"inspected": len(issues), "incomplete": len(gaps), "issues": gaps, "truncated": len(issues) >= params.limit}).model_dump()
    except Exception as exc:
        return handle_tool_error(exc, context="Issue metadata consistency report failed")


async def issue_closure_readiness_report(params: NumberInput) -> dict:
    """Explain whether an issue has the metadata and linked PR evidence needed to close it."""
    try:
        await get_service_factory().ensure_auth()
        issue = await _api(f"repos/{_repo()}/issues/{params.number}")
        timeline = await _api(f"repos/{_repo()}/issues/{params.number}/timeline?per_page=100")
        linked = [{"number": event.get("source", {}).get("issue", {}).get("number"), "merged": event.get("source", {}).get("issue", {}).get("pull_request", {}).get("merged_at") is not None} for event in timeline if event.get("event") in ("cross-referenced", "connected") and event.get("source")]
        reasons = []
        if issue.get("state") != "closed": reasons.append("issue is open")
        if not linked: reasons.append("no linked pull request evidence")
        if linked and not any(item["merged"] for item in linked): reasons.append("linked pull requests are not confirmed merged")
        return ToolSuccess(data={"issue": _row(issue), "linked_pull_requests": linked, "ready": not reasons, "reasons": reasons}).model_dump()
    except Exception as exc:
        return handle_tool_error(exc, context="Issue closure readiness report failed")


async def label_milestone_consistency_report(params: ConsistencyInput) -> dict:
    """Report missing planning metadata and inconsistent priority labels on open issues."""
    try:
        await get_service_factory().ensure_auth()
        raw = await _api(f"repos/{_repo()}/issues?state=open&per_page={params.limit}")
        issues = [x for x in raw if "pull_request" not in x]
        rows = []
        for issue in issues:
            labels = [x.get("name", "") for x in issue.get("labels", []) if isinstance(x, dict)]
            priority_labels = [name for name in labels if name.casefold().startswith("priority:")]
            problems = []
            if not issue.get("milestone"): problems.append("missing milestone")
            if len(priority_labels) > 1: problems.append("multiple priority labels")
            if problems: rows.append({"issue": _row(issue), "priority_labels": priority_labels, "problems": problems})
        return ToolSuccess(data={"inspected": len(issues), "inconsistent": len(rows), "issues": rows, "truncated": len(issues) >= params.limit}).model_dump()
    except Exception as exc:
        return handle_tool_error(exc, context="Label and milestone consistency report failed")


async def paginated_issue_page(params: PageInput) -> dict:
    """Return one explicit page of issues (pull requests excluded), newest first, with total and has_more.

    Uses the search API with `is:issue`, so a page is never shortened by
    pull requests and `query` (plain GitHub search terms, e.g. 'label:adr'
    or 'ADR in:title') is honoured.
    """
    try:
        if re.search(r"(^|\s)-?(repo|org|user):", params.query, flags=re.IGNORECASE):
            return build_error_response(error_type="validation", message="query must not contain repo:/org:/user: qualifiers; the repository is fixed", suggestion="Remove the qualifier and retry.")
        await get_service_factory().ensure_auth()
        terms = [f"repo:{_repo()}", "is:issue"]
        if params.state != "all":
            terms.append(f"is:{params.state}")
        if params.query.strip():
            terms.append(params.query.strip())
        result = await get_service_factory().gh().run(["api", "-X", "GET", "search/issues", "-f", f"q={' '.join(terms)}", "-f", "sort=created", "-f", "order=desc", "-f", f"per_page={params.per_page}", "-f", f"page={params.page}"])
        raw = json.loads(result.stdout) if result.stdout.strip() else {}
        issues = [x for x in (raw.get("items", []) if isinstance(raw, dict) else []) if isinstance(x, dict) and "pull_request" not in x]
        total = int(raw.get("total_count", len(issues))) if isinstance(raw, dict) else len(issues)
        has_more = total > (params.page - 1) * params.per_page + len(issues)
        return ToolSuccess(data={"query": " ".join(terms), "state": params.state, "page": params.page, "per_page": params.per_page, "total_count": total, "count": len(issues), "has_more": has_more, "truncated": has_more, "issues": [_row(x) for x in issues]}).model_dump()
    except Exception as exc:
        return handle_tool_error(exc, context="Paginated issue page failed")


async def response_diagnostics() -> dict:
    """Return safe target, access, version, and correlation diagnostics for automation logs."""
    try:
        settings = get_settings()
        from core.access import level_counts, AccessLevel
        counts = level_counts(AccessLevel.parse(settings.access_level))
        return ToolSuccess(data={"repository": _repo(), "project_number": settings.project_number, "owner_type": settings.owner_type, "access_level": settings.access_level, "scope_lock": settings.scope_lock, "tool_counts": counts, "version": __import__("core.version", fromlist=["VERSION"]).VERSION}).model_dump()
    except Exception as exc:
        return handle_tool_error(exc, context="Response diagnostics failed")


QUALITY_TOOLS = [repository_health_summary, pull_request_lifecycle_summary, issue_activity_digest, workflow_run_diagnostic_summary, check_conclusion_summary, label_milestone_consistency_report, issue_metadata_consistency_report, issue_closure_readiness_report, paginated_issue_page, response_diagnostics]

__all__ = ["QUALITY_TOOLS", *(fn.__name__ for fn in QUALITY_TOOLS)]
