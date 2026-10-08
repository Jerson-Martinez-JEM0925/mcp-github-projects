"""Pull request listing and detail for the configured repository.

``search_issues`` drives ``gh issue list``, which rejects pull-request
qualifiers, so there was no way to answer "which PRs were merged last?".
These tools use the REST search and pulls endpoints, are confined to the
configured repository, and report explicit pagination metadata.
"""
from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, Field, field_validator

from core.config import get_settings
from core.error_handling import build_error_response, handle_tool_error
from core.factory import get_service_factory
from core.hardening import bounded_text
from models.responses import ToolSuccess

_MERGED_WINDOW = 100


def _repo() -> str:
    settings = get_settings()
    return f"{settings.org_name}/{settings.repo_name}"


def _logins(values: Any) -> list[str]:
    return [v.get("login") for v in values or [] if isinstance(v, dict) and v.get("login")]


class ListPullRequestsInput(BaseModel):
    state: str = Field(default="open", pattern=r"^(open|closed|merged|all)$", description="open, closed (closed without merge included), merged, or all.")
    query: str = Field(default="", max_length=256, description="Optional extra GitHub search terms, e.g. 'ADR in:title' or 'label:documentation'.")
    author: str = Field(default="", max_length=100, description="Optional author login.")
    base: str = Field(default="", max_length=200, description="Optional base branch, e.g. 'main'.")
    sort: str = Field(default="updated", pattern=r"^(updated|created)$", description="Sort key (descending). For state=merged results are ordered by merge date.")
    page: int = Field(default=1, ge=1, le=10, description="1-based page.")
    per_page: int = Field(default=10, ge=1, le=50, description="Results per page.")

    @field_validator("query", "author", "base")
    @classmethod
    def _no_scope_qualifiers(cls, value: str) -> str:
        if re.search(r"(^|\s)-?(repo|org|user):", value, flags=re.IGNORECASE):
            raise ValueError("search terms must not contain repo:/org:/user: qualifiers; the repository is fixed")
        return value.strip()


def _row(item: dict[str, Any]) -> dict[str, Any]:
    pull = item.get("pull_request") or {}
    merged_at = pull.get("merged_at")
    return {
        "number": item.get("number"),
        "title": bounded_text(item.get("title", ""), 256),
        "state": "merged" if merged_at else item.get("state"),
        "draft": bool(item.get("draft", False)),
        "author": (item.get("user") or {}).get("login"),
        "labels": [x.get("name") for x in item.get("labels", []) if isinstance(x, dict)],
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
        "merged_at": merged_at,
        "closed_at": item.get("closed_at"),
        "url": item.get("html_url"),
    }


async def list_pull_requests(params: ListPullRequestsInput) -> dict:
    """List pull requests of the configured repository: open, closed, merged (latest first) or all, with filters.

    Example: the last five merged PRs -> state='merged', per_page=5. Returns
    number, title, state, author, labels, dates and URL plus `total_count`
    and `has_more`.
    """
    try:
        await get_service_factory().ensure_auth()
        repo = _repo()
        terms = [f"repo:{repo}", "is:pr"]
        if params.state == "merged":
            terms.append("is:merged")
        elif params.state == "closed":
            terms.append("is:closed")
        elif params.state == "open":
            terms.append("is:open")
        if params.author:
            terms.append(f"author:{params.author}")
        if params.base:
            terms.append(f"base:{params.base}")
        if params.query:
            terms.append(params.query)
        merged = params.state == "merged"
        # Search cannot sort by merge date: fetch the most recently updated
        # merged PRs and order them by merged_at locally.
        fetch = _MERGED_WINDOW if merged else params.per_page
        page = 1 if merged else params.page
        result = await get_service_factory().gh().run(
            [
                "api", "-X", "GET", "search/issues",
                "-f", f"q={' '.join(terms)}",
                "-f", f"sort={params.sort}",
                "-f", "order=desc",
                "-f", f"per_page={fetch}",
                "-f", f"page={page}",
            ]
        )
        data = json.loads(result.stdout) if result.stdout.strip() else {}
        items = [x for x in (data.get("items", []) if isinstance(data, dict) else []) if isinstance(x, dict)]
        total = int(data.get("total_count", len(items))) if isinstance(data, dict) else len(items)
        rows = [_row(x) for x in items]
        if merged:
            rows.sort(key=lambda r: r.get("merged_at") or "", reverse=True)
            start = (params.page - 1) * params.per_page
            rows = rows[start : start + params.per_page]
            seen = start + len(rows)
        else:
            seen = (params.page - 1) * params.per_page + len(rows)
        return ToolSuccess(
            data={
                "repository": repo,
                "query": " ".join(terms),
                "state": params.state,
                "page": params.page,
                "per_page": params.per_page,
                "total_count": total,
                "count": len(rows),
                "has_more": total > seen,
                "ordering": "merged_at desc (within the 100 most recently updated merged PRs)" if merged else f"{params.sort} desc",
                "pull_requests": rows,
            }
        ).model_dump()
    except ValueError as exc:
        return build_error_response(error_type="validation", message=str(exc), suggestion="Remove repo:/org:/user: from the search terms.")
    except Exception as exc:
        return handle_tool_error(exc, context="List pull requests failed")


class PullRequestDetailInput(BaseModel):
    number: int = Field(ge=1, description="Pull request number.")
    include_files: bool = Field(default=True, description="Include the changed files (up to 100).")
    max_body_chars: int = Field(default=8_000, ge=200, le=40_000, description="Maximum characters of the PR description returned.")


async def get_pull_request_detail(params: PullRequestDetailInput) -> dict:
    """Return one pull request of the configured repository: description, author, branches, state, reviews and changed files."""
    try:
        await get_service_factory().ensure_auth()
        repo = _repo()
        gh = get_service_factory().gh()

        async def api(path: str) -> Any:
            out = await gh.run(["api", path])
            return json.loads(out.stdout) if out.stdout.strip() else {}

        pr = await api(f"repos/{repo}/pulls/{params.number}")
        if not isinstance(pr, dict):
            pr = {}
        reviews = await api(f"repos/{repo}/pulls/{params.number}/reviews?per_page=100")
        reviews = reviews if isinstance(reviews, list) else []
        files: list[dict[str, Any]] = []
        if params.include_files:
            raw = await api(f"repos/{repo}/pulls/{params.number}/files?per_page=100")
            files = [
                {"path": f.get("filename"), "status": f.get("status"), "additions": f.get("additions"), "deletions": f.get("deletions")}
                for f in (raw if isinstance(raw, list) else [])
                if isinstance(f, dict)
            ]
        body = pr.get("body") or ""
        latest: dict[str, str] = {}
        for review in reviews:
            if isinstance(review, dict) and (review.get("user") or {}).get("login"):
                latest[review["user"]["login"]] = review.get("state", "")
        return ToolSuccess(
            data={
                "repository": repo,
                "number": params.number,
                "title": bounded_text(pr.get("title", ""), 256),
                "url": pr.get("html_url"),
                "state": "merged" if pr.get("merged_at") else pr.get("state"),
                "draft": bool(pr.get("draft", False)),
                "author": (pr.get("user") or {}).get("login"),
                "head": (pr.get("head") or {}).get("ref"),
                "base": (pr.get("base") or {}).get("ref"),
                "created_at": pr.get("created_at"),
                "merged_at": pr.get("merged_at"),
                "merged_by": (pr.get("merged_by") or {}).get("login"),
                "labels": [x.get("name") for x in pr.get("labels", []) if isinstance(x, dict)],
                "requested_reviewers": _logins(pr.get("requested_reviewers")),
                "reviews_by_user": latest,
                "changed_files": pr.get("changed_files"),
                "additions": pr.get("additions"),
                "deletions": pr.get("deletions"),
                "files": files,
                "files_truncated": bool(params.include_files and (pr.get("changed_files") or 0) > len(files)),
                "body": bounded_text(body, params.max_body_chars),
                "body_truncated": len(body) > params.max_body_chars,
            }
        ).model_dump()
    except Exception as exc:
        return handle_tool_error(exc, context="Get pull request detail failed")


PULL_REQUEST_LISTING_TOOLS = [list_pull_requests, get_pull_request_detail]

__all__ = ["PULL_REQUEST_LISTING_TOOLS", *(fn.__name__ for fn in PULL_REQUEST_LISTING_TOOLS)]
