"""Extended issue, Project, Markdown, comment, and strategy capabilities.

This module intentionally keeps destructive operations explicit. Reporting and
planning tools return structured Markdown/JSON plans; comment tools perform a
single visible comment mutation only when called directly.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from functools import wraps
from typing import Any

from pydantic import BaseModel, Field

from core.protocols import GHCLIRunner
from core.config import get_settings
from core.error_handling import handle_tool_error
from core.hardening import bounded_text, normalize_unique, parse_json_array, parse_json_items, parse_json_object
from models.responses import ToolSuccess
from core.factory import get_service_factory


class IssueNumberInput(BaseModel):
    issue_number: int = Field(ge=1, le=999_999, description="GitHub issue number")


class IssueTextInput(BaseModel):
    issue_number: int = Field(ge=1, le=999_999)
    text: str = Field(min_length=1, max_length=20_000)


class IssueCommentInput(BaseModel):
    issue_number: int = Field(ge=1, le=999_999)
    comment: str = Field(min_length=1, max_length=20_000)


class MarkdownInput(BaseModel):
    text: str = Field(min_length=1, max_length=65_536)


class CriteriaInput(BaseModel):
    text: str = Field(min_length=1, max_length=65_536)
    criteria: list[str] = Field(min_length=1, max_length=30)


class ValueInput(BaseModel):
    value: str = Field(min_length=1, max_length=100)


class IssueSearchInput(BaseModel):
    query: str = Field(default="", max_length=500)
    state: str = Field(default="open", pattern=r"^(open|closed|all)$")
    limit: int = Field(default=100, ge=1, le=100)


class IssueListInput(BaseModel):
    issue_numbers: list[int] = Field(min_length=1, max_length=100)


class TitleBodyInput(BaseModel):
    title: str = Field(min_length=1, max_length=256)
    body: str = Field(default="", max_length=65_536)


class CommentSearchInput(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=100, ge=1, le=100)


class ProjectReportInput(BaseModel):
    limit: int = Field(default=100, ge=1, le=100)


class ProjectFilterInput(BaseModel):
    status: str | None = Field(default=None, max_length=100)
    assignee: str | None = Field(default=None, max_length=100)
    label: str | None = Field(default=None, max_length=100)
    limit: int = Field(default=100, ge=1, le=100)


class ProjectPlanInput(BaseModel):
    milestone: str | None = Field(default=None, max_length=100)
    limit: int = Field(default=100, ge=1, le=100)


class IssueTemplateInput(BaseModel):
    title: str = Field(min_length=1, max_length=256)
    context: str = Field(min_length=1, max_length=8_000)
    problem: str = Field(min_length=1, max_length=8_000)
    solution: str = Field(min_length=1, max_length=8_000)
    acceptance_criteria: list[str] = Field(min_length=1, max_length=20)
    user_value: str = Field(min_length=1, max_length=8_000)


class CommentTemplateInput(BaseModel):
    issue_number: int = Field(ge=1, le=999_999)
    summary: str = Field(min_length=1, max_length=8_000)
    files: list[str] = Field(default_factory=list, max_length=100)
    verification: list[str] = Field(default_factory=list, max_length=30)


class IssuePairInput(BaseModel):
    source_issue: int = Field(ge=1, le=999_999)
    target_issue: int = Field(ge=1, le=999_999)


class CommentIdInput(BaseModel):
    comment_id: int = Field(ge=1)
    body: str = Field(min_length=1, max_length=20_000)


class RiskInput(BaseModel):
    risks: list[str] = Field(min_length=1, max_length=30)
    mitigations: list[str] = Field(default_factory=list, max_length=30)


async def _client() -> GHCLIRunner:
    await get_service_factory().ensure_auth()
    return get_service_factory().gh()


def _success(data: dict[str, Any]) -> dict:
    return ToolSuccess(data=data).model_dump()


def _guard(context: str) -> Callable[[Callable[..., Awaitable[dict]]], Callable[..., Awaitable[dict]]]:
    def decorator(function: Callable[..., Awaitable[dict]]) -> Callable[..., Awaitable[dict]]:
        @wraps(function)
        async def wrapped(*args: Any, **kwargs: Any) -> dict:
            try:
                return await function(*args, **kwargs)
            except Exception as exc:
                return handle_tool_error(exc, context=context)

        return wrapped

    return decorator


async def _issue_view(client: GHCLIRunner, number: int) -> dict[str, Any]:
    settings = get_settings()
    result = await client.run([
        "issue", "view", str(number), "--repo", f"{settings.org_name}/{settings.repo_name}",
        "--json", "number,title,body,state,labels,assignees,milestone,comments,createdAt,updatedAt,closedAt",
    ])
    return parse_json_object(result.stdout, context="gh issue view")


async def _issue_list(client: GHCLIRunner, params: IssueSearchInput | ProjectReportInput | ProjectPlanInput) -> list[dict[str, Any]]:
    settings = get_settings()
    args = [
        "issue", "list", "--repo", f"{settings.org_name}/{settings.repo_name}",
        "--state", getattr(params, "state", "open"), "--limit", str(params.limit),
        "--json", "number,title,body,state,labels,assignees,milestone,createdAt,updatedAt,closedAt",
    ]
    if getattr(params, "query", ""):
        args.extend(["--search", params.query])
    if getattr(params, "milestone", None):
        args.extend(["--milestone", params.milestone])
    result = await client.run(args)
    return parse_json_array(result.stdout, context="gh issue list")


def _labels(issue: dict[str, Any]) -> list[str]:
    return [str(label.get("name", "")) for label in issue.get("labels", []) if isinstance(label, dict)]


def _assignees(issue: dict[str, Any]) -> list[str]:
    return [str(user.get("login", "")) for user in issue.get("assignees", []) if isinstance(user, dict)]


def _markdown_sections(text: str) -> dict[str, str]:
    sections: dict[str, str] = {}
    current = "_preamble"
    buffer: list[str] = []
    for line in text.splitlines():
        heading = re.match(r"^#{1,6}\s+(.+?)\s*$", line)
        if heading:
            sections[current] = "\n".join(buffer).strip()
            current = heading.group(1).strip().casefold()
            buffer = []
        else:
            buffer.append(line)
    sections[current] = "\n".join(buffer).strip()
    return sections


def _issue_row(issue: dict[str, Any]) -> dict[str, Any]:
    milestone = issue.get("milestone") or {}
    return {
        "number": issue.get("number"),
        "title": issue.get("title", ""),
        "state": issue.get("state", ""),
        "labels": _labels(issue),
        "assignees": _assignees(issue),
        "milestone": milestone.get("title") if isinstance(milestone, dict) else None,
    }


# ── Issue and Markdown quality tools (1-20) ──────────────────────────────────

@_guard("Validate issue Markdown failed")
async def validate_issue_markdown(params: MarkdownInput) -> dict:
    sections = _markdown_sections(params.text)
    aliases = {
        "contexto": "context",
        "problema actual": "problem",
        "solución propuesta": "solution",
        "criterio de aceptación": "acceptance",
        "valor para el usuario": "value",
    }
    normalized = {aliases.get(key, key): value for key, value in sections.items()}
    required = ["context", "problem", "solution", "acceptance", "value"]
    missing = [name for name in required if not normalized.get(name)]
    checkboxes = len(re.findall(r"^\s*-\s*\[[ xX]\]", params.text, re.MULTILINE))
    return _success({"valid": not missing and checkboxes > 0, "missing_sections": missing, "acceptance_checkboxes": checkboxes})


@_guard("Format issue Markdown failed")
async def format_issue_markdown(params: MarkdownInput) -> dict:
    lines = [line.rstrip() for line in params.text.replace("\r\n", "\n").split("\n")]
    output: list[str] = []
    previous_blank = False
    for line in lines:
        blank = not line.strip()
        if blank and previous_blank:
            continue
        output.append(line)
        previous_blank = blank
    formatted = "\n".join(output).strip() + "\n"
    return _success({"markdown": formatted, "changed": formatted != params.text})


@_guard("Add acceptance criteria failed")
async def add_issue_acceptance_criteria(params: CriteriaInput) -> dict:
    criteria = "\n".join(f"- [ ] {item.strip()}" for item in normalize_unique(params.criteria))
    suffix = "\n\n## Criterio de aceptación\n" + criteria
    return _success({"markdown": params.text.rstrip() + suffix + "\n", "criteria_count": len(params.criteria)})


@_guard("Summarize issue failed")
async def summarize_issue(params: IssueNumberInput) -> dict:
    client = await _client()
    issue = await _issue_view(client, params.issue_number)
    body = bounded_text(issue.get("body", ""), 4_000)
    return _success({"issue": _issue_row(issue), "summary": f"#{issue.get('number')} — {issue.get('title')}\n\n{body}"})


@_guard("Suggest issue labels failed")
async def suggest_issue_labels(params: IssueTextInput) -> dict:
    text = f"{params.text}".casefold()
    mapping = {
        "security": ["Security"], "auth": ["Security", "BackEnd"], "api": ["BackEnd"],
        "docker": ["Infrastructure"], "ci": ["Infrastructure", "Testing"],
        "test": ["Testing"], "ui": ["FrontEnd", "UX/Polish"], "design": ["UX/Polish"],
        "bug": ["Fix"], "document": ["Documentation"], "performance": ["Performance"],
    }
    suggestions = sorted({label for word, labels in mapping.items() if word in text for label in labels})
    return _success({"issue_number": params.issue_number, "suggested_labels": suggestions})


@_guard("Suggest issue assignee failed")
async def suggest_issue_assignee(params: IssueTextInput) -> dict:
    text = params.text.casefold()
    backend_words = ("api", "backend", "database", "docker", "security", "migrat")
    frontend_words = ("ui", "frontend", "react", "next", "css", "naveg")
    settings = get_settings()
    # Logins come from configuration, never from code: the area heuristic is
    # generic, who owns each area is per repository (docs audit, #37).
    if any(word in text for word in backend_words):
        area, suggestion = "backend", settings.backend_assignee or None
    elif any(word in text for word in frontend_words):
        area, suggestion = "frontend", settings.frontend_assignee or None
    else:
        area, suggestion = None, None
    return _success({"issue_number": params.issue_number, "area": area, "suggested_assignee": suggestion, "confidence": "heuristic"})


@_guard("Normalize issue title failed")
async def normalize_issue_title(params: MarkdownInput) -> dict:
    title = re.sub(r"\s+", " ", params.text.strip())
    title = title[:1].upper() + title[1:] if title else title
    return _success({"title": title, "valid_length": 1 <= len(title) <= 256})


@_guard("Detect duplicate issues failed")
async def detect_issue_duplicates(params: IssueTextInput) -> dict:
    client = await _client()
    issues = await _issue_list(client, IssueSearchInput(query=params.text, state="all", limit=100))
    return _success({"issue_number": params.issue_number, "duplicates": [_issue_row(issue) for issue in issues if issue.get("number") != params.issue_number]})


@_guard("Find stale issues failed")
async def find_stale_issues(params: ProjectReportInput) -> dict:
    client = await _client()
    issues = await _issue_list(client, IssueSearchInput(state="open", limit=params.limit))
    stale = [issue for issue in issues if not issue.get("assignees") or not issue.get("updatedAt")]
    return _success({"issues": [_issue_row(issue) for issue in stale], "count": len(stale), "rule": "open without assignee or update timestamp"})


@_guard("Find unassigned issues failed")
async def find_unassigned_issues(params: ProjectReportInput) -> dict:
    client = await _client()
    issues = await _issue_list(client, IssueSearchInput(state="open", limit=params.limit))
    unassigned = [_issue_row(issue) for issue in issues if not issue.get("assignees")]
    return _success({"issues": unassigned, "count": len(unassigned)})


@_guard("Find missing issue metadata failed")
async def find_missing_issue_metadata(params: ProjectReportInput) -> dict:
    client = await _client()
    issues = await _issue_list(client, IssueSearchInput(state="open", limit=params.limit))
    result = []
    for issue in issues:
        missing = []
        if not issue.get("milestone"): missing.append("milestone")
        if not issue.get("assignees"): missing.append("assignee")
        if not issue.get("labels"): missing.append("labels")
        if missing: result.append({**_issue_row(issue), "missing": missing})
    return _success({"issues": result, "count": len(result)})


async def _comment_issue(issue_number: int, comment: str) -> dict:
    client = await _client()
    settings = get_settings()
    await client.run(["issue", "comment", str(issue_number), "--repo", f"{settings.org_name}/{settings.repo_name}", "--body", comment])
    return _success({"issue_number": issue_number, "commented": True, "comment": comment})


@_guard("Comment issue progress failed")
async def comment_issue_progress(params: IssueCommentInput) -> dict:
    return await _comment_issue(params.issue_number, f"## Avance\n\n{params.comment}")


@_guard("Comment issue plan failed")
async def comment_issue_plan(params: IssueCommentInput) -> dict:
    return await _comment_issue(params.issue_number, f"## Plan de implementación\n\n{params.comment}")


@_guard("Comment issue blocker failed")
async def comment_issue_blocker(params: IssueCommentInput) -> dict:
    return await _comment_issue(params.issue_number, f"## Bloqueo\n\n{params.comment}\n\n**Estado:** ⏸ Pending")


@_guard("Comment issue resolution failed")
async def comment_issue_resolution(params: IssueCommentInput) -> dict:
    return await _comment_issue(params.issue_number, f"## Resolución\n\n{params.comment}")


@_guard("List issue comments failed")
async def list_issue_comments(params: IssueNumberInput) -> dict:
    client = await _client()
    issue = await _issue_view(client, params.issue_number)
    comments = issue.get("comments", [])
    return _success({"issue_number": params.issue_number, "comments": comments, "count": len(comments)})


@_guard("Search issue comments failed")
async def search_issue_comments(params: CommentSearchInput) -> dict:
    client = await _client()
    issues = await _issue_list(client, IssueSearchInput(query=params.query, state="all", limit=params.limit))
    matches = []
    for issue in issues:
        for comment in issue.get("comments", []):
            body = str(comment.get("body", "")) if isinstance(comment, dict) else str(comment)
            if params.query.casefold() in body.casefold():
                matches.append({"issue_number": issue.get("number"), "comment": comment})
    return _success({"matches": matches, "count": len(matches)})


@_guard("Edit issue comment failed")
async def edit_issue_comment(params: CommentIdInput) -> dict:
    client = await _client()
    settings = get_settings()
    result = await client.run(["api", f"repos/{settings.org_name}/{settings.repo_name}/issues/comments/{params.comment_id}", "--method", "PATCH", "-f", f"body={params.body}"])
    return _success({"comment_id": params.comment_id, "updated": True, "response": bounded_text(result.stdout)})


@_guard("Build issue template failed")
async def build_issue_template(params: IssueTemplateInput) -> dict:
    criteria = "\n".join(f"- [ ] {item.strip()}" for item in normalize_unique(params.acceptance_criteria))
    markdown = f"# {params.title}\n\n## Contexto\n{params.context}\n\n## Problema actual\n{params.problem}\n\n## Solución propuesta\n{params.solution}\n\n## Criterio de aceptación\n{criteria}\n\n## Valor para el usuario\n{params.user_value}\n"
    return _success({"title": params.title, "body": markdown})


@_guard("Build closure comment failed")
async def build_closure_comment(params: CommentTemplateInput) -> dict:
    files = "\n".join(f"- `{item}`" for item in params.files) or "- No especificados"
    verification = "\n".join(f"- {item}" for item in params.verification) or "- No especificada"
    comment = f"## Implementación completada\n\n{params.summary}\n\n### Archivos modificados\n{files}\n\n### Verificación\n{verification}"
    return _success({"issue_number": params.issue_number, "comment": comment})


@_guard("Build dependency comment failed")
async def build_dependency_comment(params: IssuePairInput) -> dict:
    return _success({"comment": f"Bloqueado por #{params.target_issue}. Este issue depende de la resolución de #{params.source_issue}.", "source_issue": params.source_issue, "target_issue": params.target_issue})


@_guard("Build issue bundle failed")
async def build_issue_bundle(params: IssueListInput) -> dict:
    client = await _client()
    issues = [await _issue_view(client, number) for number in params.issue_numbers]
    markdown = "\n\n".join(f"## #{issue.get('number')} — {issue.get('title')}\n\n{issue.get('body', '')}" for issue in issues)
    return _success({"issues": [_issue_row(issue) for issue in issues], "markdown": markdown})


# ── Project reporting and planning tools (21-40) ─────────────────────────────

async def _project_items(client: GHCLIRunner, limit: int) -> list[dict[str, Any]]:
    settings = get_settings()
    result = await client.run(["project", "item-list", str(settings.project_number), "--owner", settings.org_name, "--format", "json", "--limit", str(limit)])
    return parse_json_items(result.stdout, context="gh project item-list")


@_guard("Project health report failed")
async def project_health_report(params: ProjectReportInput) -> dict:
    client = await _client()
    items = await _project_items(client, params.limit)
    missing_title = [item for item in items if not item.get("title")]
    return _success({"total": len(items), "missing_title": len(missing_title), "health_pct": round((len(items) - len(missing_title)) / max(len(items), 1) * 100), "items": items})


@_guard("Project status distribution failed")
async def project_status_distribution(params: ProjectReportInput) -> dict:
    items = await _project_items(await _client(), params.limit)
    counts = Counter(str(item.get("status", "Unknown")) for item in items)
    return _success({"distribution": dict(counts), "total": len(items)})


@_guard("Project priority distribution failed")
async def project_priority_distribution(params: ProjectReportInput) -> dict:
    items = await _project_items(await _client(), params.limit)
    counts = Counter(str(item.get("priority", "Unknown")) for item in items)
    return _success({"distribution": dict(counts), "total": len(items)})


@_guard("Project assignee load failed")
async def project_assignee_load(params: ProjectReportInput) -> dict:
    items = await _project_items(await _client(), params.limit)
    counts = Counter(str(item.get("assignees", "Unassigned")) for item in items)
    return _success({"assignee_load": dict(counts), "total": len(items)})


@_guard("Project due date risk failed")
async def project_due_date_risk(params: ProjectReportInput) -> dict:
    items = await _project_items(await _client(), params.limit)
    now = datetime.now(timezone.utc).date().isoformat()
    risky = [item for item in items if item.get("status") not in {"Done", "✅ Done"} and item.get("dueDate", "") and str(item.get("dueDate")) < now]
    return _success({"overdue": risky, "count": len(risky), "as_of": now})


@_guard("Project cycle time failed")
async def project_cycle_time(params: ProjectReportInput) -> dict:
    issues = await _issue_list(await _client(), IssueSearchInput(state="closed", limit=params.limit))
    durations = []
    for issue in issues:
        if issue.get("createdAt") and issue.get("closedAt"):
            durations.append({"number": issue.get("number"), "days": (datetime.fromisoformat(issue["closedAt"].replace("Z", "+00:00")) - datetime.fromisoformat(issue["createdAt"].replace("Z", "+00:00"))).days})
    average = round(sum(item["days"] for item in durations) / max(len(durations), 1), 2)
    return _success({"issues": durations, "average_days": average})


@_guard("Project orphan items failed")
async def project_orphan_items(params: ProjectReportInput) -> dict:
    items = await _project_items(await _client(), params.limit)
    orphaned = [item for item in items if not item.get("content") and not item.get("title")]
    return _success({"items": orphaned, "count": len(orphaned)})


@_guard("Project missing fields failed")
async def project_missing_fields(params: ProjectReportInput) -> dict:
    items = await _project_items(await _client(), params.limit)
    missing = [{"id": item.get("id"), "title": item.get("title"), "missing": [field for field in ("status", "priority") if not item.get(field)]} for item in items]
    return _success({"items": [item for item in missing if item["missing"]], "count": sum(bool(item["missing"]) for item in missing)})


@_guard("Project field options report failed")
async def project_field_options_report(params: ProjectReportInput) -> dict:
    items = await _project_items(await _client(), params.limit)
    return _success({"observed_statuses": sorted({str(item.get("status")) for item in items if item.get("status")}), "observed_priorities": sorted({str(item.get("priority")) for item in items if item.get("priority")})})


@_guard("Project board validation failed")
async def project_validate_board(params: ProjectReportInput) -> dict:
    report = await project_health_report(params)
    data = report.get("data", {})
    return _success({"valid": data.get("health_pct", 0) >= 90, "health": data})


@_guard("Project metadata sync plan failed")
async def project_sync_issue_metadata(params: ProjectFilterInput) -> dict:
    issues = await _issue_list(await _client(), IssueSearchInput(state="open", limit=params.limit))
    pending = [_issue_row(issue) for issue in issues if (params.assignee and not issue.get("assignees")) or (params.label and params.label not in _labels(issue))]
    return _success({"dry_run": True, "pending": pending, "next_action": "Review the plan before applying issue edits."})


@_guard("Project default status plan failed")
async def project_set_default_status(params: ValueInput) -> dict:
    return _success({"dry_run": True, "status": params.value, "message": "Default status plan generated; no items were mutated."})


@_guard("Project default priority plan failed")
async def project_set_default_priority(params: ValueInput) -> dict:
    return _success({"dry_run": True, "priority": params.value, "message": "Default priority plan generated; no items were mutated."})


@_guard("Project bulk status plan failed")
async def project_bulk_status_by_filter(params: ProjectFilterInput) -> dict:
    items = await _project_items(await _client(), params.limit)
    selected = [item for item in items if not params.status or item.get("status") == params.status]
    return _success({"dry_run": True, "selected": selected, "count": len(selected), "requested_status": params.status})


@_guard("Project bulk priority plan failed")
async def project_bulk_priority_by_filter(params: ProjectFilterInput) -> dict:
    items = await _project_items(await _client(), params.limit)
    selected = [item for item in items if not params.status or item.get("status") == params.status]
    return _success({"dry_run": True, "selected": selected, "count": len(selected), "requested_priority": params.label})


@_guard("Project bulk due-date plan failed")
async def project_bulk_due_date_by_filter(params: ProjectFilterInput) -> dict:
    items = await _project_items(await _client(), params.limit)
    selected = [item for item in items if not params.status or item.get("status") == params.status]
    return _success({"dry_run": True, "selected": selected, "count": len(selected), "requested_due_date": params.label})


@_guard("Project export failed")
async def project_export_markdown(params: ProjectReportInput) -> dict:
    items = await _project_items(await _client(), params.limit)
    rows = [f"| {item.get('id', '')} | {item.get('title', '')} | {item.get('status', '')} | {item.get('priority', '')} |" for item in items]
    markdown = "| ID | Título | Estado | Prioridad |\n|---|---|---|---|\n" + "\n".join(rows)
    return _success({"markdown": markdown, "count": len(items)})


@_guard("Project import validation failed")
async def project_import_markdown(params: MarkdownInput) -> dict:
    rows = [line for line in params.text.splitlines() if line.startswith("|") and "---" not in line]
    return _success({"valid": len(rows) >= 2, "row_count": max(len(rows) - 1, 0), "dry_run": True})


# ── Strategic automation and Markdown tools (41-60) ──────────────────────────

@_guard("Plan next sprint failed")
async def plan_next_sprint(params: ProjectPlanInput) -> dict:
    issues = await _issue_list(await _client(), IssueSearchInput(state="open", limit=params.limit))
    candidates = [_issue_row(issue) for issue in issues if not params.milestone or (issue.get("milestone") or {}).get("title") == params.milestone]
    markdown = "## Próximo sprint\n\n" + "\n".join(f"- [ ] #{item['number']} — {item['title']}" for item in candidates)
    return _success({"issues": candidates, "markdown": markdown, "count": len(candidates)})


@_guard("Prioritize backlog failed")
async def prioritize_backlog(params: ProjectReportInput) -> dict:
    issues = await _issue_list(await _client(), IssueSearchInput(state="open", limit=params.limit))
    ranked = sorted((_issue_row(issue) for issue in issues), key=lambda item: ("Security" not in item["labels"], "Fix" not in item["labels"], item["number"] or 0))
    return _success({"ranked_issues": ranked, "method": "security, fixes, then issue number"})


@_guard("Generate daily plan failed")
async def generate_daily_plan(params: ProjectPlanInput) -> dict:
    report = await prioritize_backlog(ProjectReportInput(limit=params.limit))
    issues = report.get("data", {}).get("ranked_issues", [])
    return _success({"markdown": "## Plan diario\n\n" + "\n".join(f"1. #{i['number']} — {i['title']}" for i in issues[:5]), "issues": issues[:5]})


@_guard("Generate weekly plan failed")
async def generate_weekly_plan(params: ProjectPlanInput) -> dict:
    report = await plan_next_sprint(params)
    return _success({"markdown": "# Plan semanal\n\n" + report.get("data", {}).get("markdown", ""), "source": report.get("data", {})})


@_guard("Generate risk register failed")
async def generate_risk_register(params: ProjectPlanInput) -> dict:
    issues = await _issue_list(await _client(), IssueSearchInput(state="open", limit=params.limit))
    risks = [_issue_row(issue) for issue in issues if any(label in {"Security", "blocked", "Fix"} for label in _labels(issue))]
    return _success({"risks": risks, "markdown": "## Registro de riesgos\n\n" + "\n".join(f"- #{i['number']} — {i['title']}" for i in risks)})


@_guard("Generate dependency report failed")
async def generate_dependency_report(params: ProjectReportInput) -> dict:
    issues = await _issue_list(await _client(), IssueSearchInput(state="open", limit=params.limit))
    dependencies = [_issue_row(issue) for issue in issues if any(word in str(issue.get("body", "")).casefold() for word in ("blocked by", "depende de", "bloqueado"))]
    return _success({"dependencies": dependencies, "count": len(dependencies)})


@_guard("Generate release checklist failed")
async def generate_release_checklist(params: ProjectPlanInput) -> dict:
    checklist = ["Tests automatizados en verde", "Criterios de aceptación verificados", "Documentación actualizada", "Issues vinculados al milestone", "Notas de release revisadas"]
    return _success({"milestone": params.milestone, "markdown": "## Checklist de release\n\n" + "\n".join(f"- [ ] {item}" for item in checklist)})


@_guard("Generate changelog failed")
async def generate_changelog_from_issues(params: ProjectPlanInput) -> dict:
    issues = await _issue_list(await _client(), IssueSearchInput(state="closed", limit=params.limit))
    groups: dict[str, list[str]] = {"Features": [], "Fixes": [], "Other": []}
    for issue in issues:
        labels = set(_labels(issue))
        group = "Features" if "Feature" in labels else "Fixes" if "Fix" in labels else "Other"
        groups[group].append(f"- #{issue.get('number')} — {issue.get('title')}")
    markdown = "\n\n".join(f"### {key}\n\n" + "\n".join(values or ["- Ninguno"]) for key, values in groups.items())
    return _success({"markdown": markdown, "groups": groups})


@_guard("Generate project brief failed")
async def generate_project_brief(params: ProjectReportInput) -> dict:
    report = await project_health_report(params)
    data = report.get("data", {})
    return _success({"markdown": f"# Project brief\n\n- Items: {data.get('total', 0)}\n- Health: {data.get('health_pct', 0)}%\n- Missing titles: {data.get('missing_title', 0)}"})


@_guard("Generate stakeholder update failed")
async def generate_stakeholder_update(params: ProjectPlanInput) -> dict:
    issues = await _issue_list(await _client(), IssueSearchInput(state="all", limit=params.limit))
    done = sum(issue.get("state") == "CLOSED" for issue in issues)
    return _success({"markdown": f"## Actualización ejecutiva\n\n- Issues revisados: {len(issues)}\n- Completados: {done}\n- Pendientes: {len(issues) - done}\n- Milestone: {params.milestone or 'Todos'}"})


@_guard("Detect scope creep failed")
async def detect_scope_creep(params: ProjectPlanInput) -> dict:
    issues = await _issue_list(await _client(), IssueSearchInput(state="open", limit=params.limit))
    scope = [issue for issue in issues if "scope" in str(issue.get("body", "")).casefold() or "fuera de alcance" in str(issue.get("body", "")).casefold()]
    return _success({"scope_creep_candidates": [_issue_row(issue) for issue in scope], "count": len(scope)})


@_guard("Detect blocked work failed")
async def detect_blocked_work(params: ProjectReportInput) -> dict:
    report = await generate_dependency_report(params)
    return _success({"blocked": report.get("data", {}).get("dependencies", []), "count": report.get("data", {}).get("count", 0)})


@_guard("Recommend WIP moves failed")
async def recommend_wip_moves(params: ProjectReportInput) -> dict:
    issues = await _issue_list(await _client(), IssueSearchInput(state="open", limit=params.limit))
    in_progress = [issue for issue in issues if "in progress" in str(issue.get("body", "")).casefold() or "in progress" in str(issue.get("title", "")).casefold()]
    return _success({"recommendations": [{"issue": _issue_row(issue), "action": "finish, block, or move back to To Do"} for issue in in_progress]})


@_guard("Recommend sprint assignment failed")
async def recommend_sprint_assignment(params: ProjectPlanInput) -> dict:
    issues = await _issue_list(await _client(), IssueSearchInput(state="open", limit=params.limit))
    assignments = [{"issue": _issue_row(issue), "recommended_action": "assign owner and milestone"} for issue in issues if not issue.get("assignees") or not issue.get("milestone")]
    return _success({"assignments": assignments, "count": len(assignments)})


@_guard("Auto-triage issue failed")
async def auto_triage_issue(params: IssueNumberInput) -> dict:
    issue = await _issue_view(await _client(), params.issue_number)
    suggestion = await suggest_issue_labels(IssueTextInput(issue_number=params.issue_number, text=f"{issue.get('title', '')}\n{issue.get('body', '')}"))
    return _success({"issue": _issue_row(issue), "suggestions": suggestion.get("data", {}), "dry_run": True})


@_guard("Build status update comment failed")
async def build_status_update_comment(params: CommentTemplateInput) -> dict:
    return await build_closure_comment(params)


@_guard("Build sprint retrospective failed")
async def build_sprint_retrospective(params: ProjectPlanInput) -> dict:
    return _success({"markdown": "## Retrospectiva\n\n### Qué salió bien\n- \\n\n### Qué mejorar\n- \\n\n### Acciones\n- [ ] Revisar métricas y compromisos", "milestone": params.milestone})


@_guard("Build roadmap Markdown failed")
async def build_roadmap_markdown(params: ProjectPlanInput) -> dict:
    issues = await _issue_list(await _client(), IssueSearchInput(state="open", limit=params.limit))
    markdown = "# Roadmap\n\n" + "\n".join(f"- **{issue.get('milestone', {}).get('title', 'Sin milestone')}** — #{issue.get('number')} {issue.get('title')}" for issue in issues)
    return _success({"markdown": markdown})


@_guard("Build issue creation bundle failed")
async def build_issue_creation_bundle(params: IssueTemplateInput) -> dict:
    body = await build_issue_template(params)
    return _success({"issue": {"title": params.title, "body": body.get("data", {}).get("body", "")}, "dry_run": True, "message": "Bundle preparado; no se creó ningún issue."})


@_guard("Build issue review checklist failed")
async def build_issue_review_checklist(params: IssueNumberInput) -> dict:
    issue = await _issue_view(await _client(), params.issue_number)
    checks = ["Descripción completa", "Criterios de aceptación presentes", "Labels configurados", "Assignee configurado", "Milestone configurado"]
    return _success({"issue": _issue_row(issue), "markdown": "## Revisión\n\n" + "\n".join(f"- [{'x' if (item == 'Labels configurados' and issue.get('labels')) or (item == 'Assignee configurado' and issue.get('assignees')) or (item == 'Milestone configurado' and issue.get('milestone')) else ' '}] {item}" for item in checks)})


@_guard("Build comment digest failed")
async def build_comment_digest(params: IssueListInput) -> dict:
    client = await _client()
    digests = []
    for number in params.issue_numbers:
        issue = await _issue_view(client, number)
        comments = issue.get("comments", [])
        digests.append({"issue_number": number, "comment_count": len(comments), "last_comment": comments[-1] if comments else None})
    return _success({"digest": digests})


@_guard("Build automation decision failed")
async def build_automation_decision(params: RiskInput) -> dict:
    decisions = [{"risk": risk, "mitigation": params.mitigations[index] if index < len(params.mitigations) else "Definir responsable y fecha"} for index, risk in enumerate(params.risks)]
    return _success({"decisions": decisions, "markdown": "## Decisiones de automatización\n\n" + "\n".join(f"- **{item['risk']}**: {item['mitigation']}" for item in decisions)})


# Exactly 60 public tools are registered by server.py.
CAPABILITY_TOOL_NAMES = [
    "validate_issue_markdown", "format_issue_markdown", "add_issue_acceptance_criteria", "summarize_issue", "suggest_issue_labels", "suggest_issue_assignee", "normalize_issue_title", "detect_issue_duplicates", "find_stale_issues", "find_unassigned_issues", "find_missing_issue_metadata", "comment_issue_progress", "comment_issue_plan", "comment_issue_blocker", "comment_issue_resolution", "list_issue_comments", "search_issue_comments", "edit_issue_comment", "build_issue_template", "build_closure_comment", "build_dependency_comment", "project_health_report", "project_status_distribution", "project_priority_distribution", "project_assignee_load", "project_due_date_risk", "project_cycle_time", "project_orphan_items", "project_missing_fields", "project_field_options_report", "project_validate_board", "project_sync_issue_metadata", "project_set_default_status", "project_set_default_priority", "project_bulk_status_by_filter", "project_bulk_priority_by_filter", "project_bulk_due_date_by_filter", "project_export_markdown", "project_import_markdown", "plan_next_sprint", "prioritize_backlog", "generate_daily_plan", "generate_weekly_plan", "generate_risk_register", "generate_dependency_report", "generate_release_checklist", "generate_changelog_from_issues", "generate_project_brief", "generate_stakeholder_update", "detect_scope_creep", "detect_blocked_work", "recommend_wip_moves", "recommend_sprint_assignment", "auto_triage_issue", "build_status_update_comment", "build_sprint_retrospective", "build_roadmap_markdown", "build_issue_creation_bundle", "build_issue_review_checklist", "build_comment_digest",
]
assert len(CAPABILITY_TOOL_NAMES) == 60

# One-line descriptions. These functions are defined compactly without
# docstrings, so without this table MCP clients received an EMPTY description
# for all 60 tools (docs audit, issue #37). tests/test_tool_docs.py asserts
# every registered tool advertises a non-empty description.
# Note: the Markdown these tools generate uses Spanish headings (e.g.
# "## Plan de implementación") — see docs/TOOLS.md.
_DESCRIPTIONS: dict[str, str] = {
    "validate_issue_markdown": "Check issue Markdown for the template sections (Contexto, Problema actual, Solución propuesta, Criterio de aceptación, Valor para el usuario) and report the missing ones (local, no API).",
    "format_issue_markdown": "Normalize issue Markdown: strip trailing spaces and collapse repeated blank lines (local, no API).",
    "add_issue_acceptance_criteria": "Append a deduplicated acceptance-criteria checklist to issue Markdown (local, no API).",
    "summarize_issue": "Return an issue's key fields and its body capped at 4,000 characters.",
    "suggest_issue_labels": "Suggest labels from keywords in the text (fixed vocabulary: Security, BackEnd, FrontEnd, Infrastructure, Testing, UX/Polish, Fix, Documentation, Performance); changes nothing.",
    "suggest_issue_assignee": "Classify the text as backend/frontend by keywords and suggest the assignee configured for that area (GH_PROJECT_BACKEND_ASSIGNEE / GH_PROJECT_FRONTEND_ASSIGNEE); changes nothing.",
    "normalize_issue_title": "Collapse whitespace, capitalize the first letter and check the 1-256 character limit (local, no API).",
    "detect_issue_duplicates": "Search issues in any state matching the text and list them as possible duplicates of the given issue.",
    "find_stale_issues": "List open issues that have no assignee or no update timestamp.",
    "find_unassigned_issues": "List open issues with no assignee.",
    "find_missing_issue_metadata": "List open issues missing a milestone, assignee or labels.",
    "comment_issue_progress": "Post a progress comment (\"## Avance\") on an issue.",
    "comment_issue_plan": "Post an implementation-plan comment (\"## Plan de implementación\") on an issue.",
    "comment_issue_blocker": "Post a blocker comment (\"## Bloqueo\") on an issue.",
    "comment_issue_resolution": "Post a resolution comment (\"## Resolución\") on an issue.",
    "list_issue_comments": "List an issue's comments.",
    "search_issue_comments": "Search issues matching a query and return their comments that contain it.",
    "edit_issue_comment": "Replace the body of an existing issue comment by comment id.",
    "build_issue_template": "Build issue Markdown (Contexto, Problema actual, Solución propuesta, Criterio de aceptación, …) from its parts (local, no API).",
    "build_closure_comment": "Build a closure comment with a summary, changed files and verification steps (local, no API).",
    "build_dependency_comment": "Build a \"Bloqueado por #N\" dependency comment (local, no API).",
    "project_health_report": "Board health: item count, items missing a title and the percentage that have one.",
    "project_status_distribution": "Count board items per Status value.",
    "project_priority_distribution": "Count board items per Priority value.",
    "project_assignee_load": "Count board items per assignee.",
    "project_due_date_risk": "List board items past their due date that are not Done.",
    "project_cycle_time": "Average days from creation to close over recently closed issues.",
    "project_orphan_items": "List board items with neither linked content nor a title.",
    "project_missing_fields": "List board items missing Status or Priority.",
    "project_field_options_report": "Report the Status and Priority values observed on the board.",
    "project_validate_board": "Validate the board: valid when at least 90% of items have a title (see project_health_report).",
    "project_sync_issue_metadata": "Plan (dry run) which open issues lack the given assignee or label; changes nothing.",
    "project_set_default_status": "Plan (dry run) a default Status; returns the plan only and changes nothing.",
    "project_set_default_priority": "Plan (dry run) a default Priority; returns the plan only and changes nothing.",
    "project_bulk_status_by_filter": "Plan (dry run) a bulk Status change for items matching a filter; changes nothing.",
    "project_bulk_priority_by_filter": "Plan (dry run) a bulk Priority change for items matching a filter; changes nothing.",
    "project_bulk_due_date_by_filter": "Plan (dry run) a bulk due-date change for items matching a filter; changes nothing.",
    "project_export_markdown": "Export board items as a Markdown table (ID, Título, Estado, Prioridad).",
    "project_import_markdown": "Validate a Markdown table for import and count its rows (dry run); creates nothing.",
    "plan_next_sprint": "List open issues, optionally only those in a milestone, as a next-sprint checklist.",
    "prioritize_backlog": "Rank open issues: Security label first, then Fix, then by issue number.",
    "generate_daily_plan": "Top five issues of the prioritized backlog as a daily plan.",
    "generate_weekly_plan": "Wrap the next-sprint checklist as a weekly plan.",
    "generate_risk_register": "Risk register of open issues labelled Security, blocked or Fix.",
    "generate_dependency_report": "List open issues whose body says \"blocked by\", \"depende de\" or \"bloqueado\".",
    "generate_release_checklist": "Build a fixed release checklist in Markdown (local, no API).",
    "generate_changelog_from_issues": "Changelog from recently closed issues, grouped by the Feature / Fix labels.",
    "generate_project_brief": "Short project brief built from the board health report.",
    "generate_stakeholder_update": "Stakeholder update with counts of reviewed, completed and pending issues.",
    "detect_scope_creep": "List open issues whose body mentions \"scope\" or \"fuera de alcance\".",
    "detect_blocked_work": "List blocked issues (same rule as generate_dependency_report).",
    "recommend_wip_moves": "For open issues that mention \"in progress\", recommend finishing, blocking or moving them back.",
    "recommend_sprint_assignment": "List open issues lacking an assignee or milestone, to assign before the sprint.",
    "auto_triage_issue": "Suggest triage labels for an issue from its title and body (dry run; nothing is applied).",
    "build_status_update_comment": "Build a status-update comment (same format as build_closure_comment; local, no API).",
    "build_sprint_retrospective": "Build a sprint retrospective Markdown template (local, no API).",
    "build_roadmap_markdown": "Roadmap in Markdown: open issues listed with their milestone.",
    "build_issue_creation_bundle": "Build a title + body bundle from the issue template (dry run; no issue is created).",
    "build_issue_review_checklist": "Review checklist for an issue: description, acceptance criteria, labels, assignee, milestone.",
    "build_comment_digest": "Comment count and latest comment for several issues.",
}
assert set(_DESCRIPTIONS) == set(CAPABILITY_TOOL_NAMES), set(_DESCRIPTIONS) ^ set(CAPABILITY_TOOL_NAMES)
for _name, _description in _DESCRIPTIONS.items():
    globals()[_name].__doc__ = _description
