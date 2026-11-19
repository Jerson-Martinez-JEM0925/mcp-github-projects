"""Pull request review tools.

``list_pull_request_reviews`` inspects the reviews already submitted on a PR
(approvals, change requests, comments) plus the inline review comments.
``review_pull_request`` submits a new review — approve, request changes, or
comment-only — authored by the authenticated user (the request-scoped token
in request-auth mode), so reviews appear under the caller's GitHub identity
and are bounded by that user's permissions and the repository's rules.

Both tools accept the ``owner``/``repo`` override pair, fenced by
``enforce_scope`` so ``GH_PROJECT_SCOPE_LOCK=true`` still confines them to
the configured target.
"""
from __future__ import annotations

import json
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator

from core.access import resolve_repository
from core.error_handling import build_error_response, handle_tool_error
from core.factory import get_service_factory
from core.hardening import bounded_text
from models.responses import ToolSuccess

_MAX_BODY_CHARS = 20_000


class _RepoTarget(BaseModel):
    owner: Optional[str] = Field(
        default=None,
        description="Repository owner. Defaults to GH_PROJECT_ORG_NAME (scope-lock checked).",
    )
    repo: Optional[str] = Field(
        default=None,
        description="Repository name. Defaults to GH_PROJECT_REPO_NAME (scope-lock checked).",
    )


class ListPullRequestReviewsInput(_RepoTarget):
    """Input schema for the list_pull_request_reviews tool."""

    number: int = Field(ge=1, description="Pull request number.")
    include_comments: bool = Field(
        default=True,
        description="Include inline review comments (file/line annotations) grouped by review.",
    )
    per_page: int = Field(default=50, ge=1, le=100, description="Reviews per page (max 100).")


async def list_pull_request_reviews(params: ListPullRequestReviewsInput) -> dict:
    """List the reviews submitted on a pull request: reviewer, verdict and body.

    Returns each review's state (APPROVED, CHANGES_REQUESTED, COMMENTED,
    DISMISSED, PENDING), its summary body and, when ``include_comments`` is
    set, the inline file/line comments attached to each review. Useful
    before deciding whether a PR still needs changes or is ready to merge.
    """
    try:
        await get_service_factory().ensure_auth()
        repo = resolve_repository(params.owner, params.repo)
        gh = get_service_factory().gh()

        async def api(path: str) -> Any:
            out = await gh.run(["api", path])
            return json.loads(out.stdout) if out.stdout.strip() else {}

        reviews = await api(
            f"repos/{repo}/pulls/{params.number}/reviews?per_page={params.per_page}"
        )
        reviews = reviews if isinstance(reviews, list) else []

        comments_by_review: dict[int, list[dict[str, Any]]] = {}
        if params.include_comments:
            raw = await api(
                f"repos/{repo}/pulls/{params.number}/comments?per_page=100"
            )
            for c in (raw if isinstance(raw, list) else []):
                if not isinstance(c, dict):
                    continue
                rid = c.get("pull_request_review_id")
                if rid is None:
                    continue
                comments_by_review.setdefault(int(rid), []).append(
                    {
                        "path": c.get("path"),
                        "line": c.get("line") or c.get("original_line"),
                        "side": c.get("side"),
                        "author": (c.get("user") or {}).get("login"),
                        "body": bounded_text(c.get("body", ""), 2_000),
                        "url": c.get("html_url"),
                    }
                )

        rows = [
            {
                "id": r.get("id"),
                "author": (r.get("user") or {}).get("login"),
                "state": r.get("state"),
                "body": bounded_text(r.get("body", ""), _MAX_BODY_CHARS),
                "commit_id": r.get("commit_id"),
                "submitted_at": r.get("submitted_at"),
                "url": r.get("html_url"),
                **(
                    {"comments": comments_by_review.get(int(r["id"]), [])}
                    if params.include_comments and isinstance(r.get("id"), int)
                    else {}
                ),
            }
            for r in reviews
            if isinstance(r, dict)
        ]

        latest: dict[str, str] = {}
        for row in rows:
            if row["author"]:
                latest[row["author"]] = row.get("state") or ""

        return ToolSuccess(
            data={
                "repository": repo,
                "pull_request": params.number,
                "count": len(rows),
                "has_more": len(rows) >= params.per_page,
                "latest_state_by_reviewer": latest,
                "reviews": rows,
            }
        ).model_dump()
    except ValueError as exc:
        return build_error_response(
            error_type="validation",
            message=str(exc),
            suggestion="Provide a valid pull request number.",
        )
    except Exception as exc:
        return handle_tool_error(exc, context="List pull request reviews failed")


class ReviewPullRequestInput(_RepoTarget):
    """Input schema for the review_pull_request tool."""

    number: int = Field(ge=1, description="Pull request number.")
    event: Literal["approve", "request_changes", "comment"] = Field(
        description="Review verdict: 'approve', 'request_changes', or 'comment'.",
    )
    body: str = Field(
        default="",
        max_length=_MAX_BODY_CHARS,
        validate_default=True,
        description="Review summary body. Required for 'comment' and 'request_changes'; optional but recommended for 'approve'.",
    )

    @field_validator("body")
    @classmethod
    def _body_required_for_non_approvals(cls, value: str, info: Any) -> str:
        event = (info.data or {}).get("event")
        if event in {"comment", "request_changes"} and not value.strip():
            raise ValueError(f"body is required when event='{event}'.")
        return value


async def review_pull_request(params: ReviewPullRequestInput) -> dict:
    """Submit a pull request review as the authenticated user.

    ``event`` selects the verdict: 'approve', 'request_changes', or
    'comment' (a review with feedback but no approval verdict). The review
    is authored by whoever owns the token — in request-auth mode that is
    the calling user — and is subject to that user's permissions and the
    repository's review rules (e.g. you cannot approve your own PR).
    """
    try:
        await get_service_factory().ensure_auth()
        repo = resolve_repository(params.owner, params.repo)
        gh = get_service_factory().gh()

        flag = {
            "approve": "--approve",
            "request_changes": "--request-changes",
            "comment": "--comment",
        }[params.event]
        argv = ["pr", "review", str(params.number), "--repo", repo, flag]
        if params.body.strip():
            argv += ["--body", params.body]

        result = await gh.run(argv)
        detail = (result.stdout or result.stderr or "").strip()

        return ToolSuccess(
            data={
                "repository": repo,
                "pull_request": params.number,
                "event": params.event,
                "message": detail or f"Review submitted: {params.event}.",
            }
        ).model_dump()
    except Exception as exc:
        return handle_tool_error(exc, context="Review pull request failed")


PULL_REQUEST_REVIEW_TOOLS = [list_pull_request_reviews, review_pull_request]

__all__ = [
    "PULL_REQUEST_REVIEW_TOOLS",
    *(fn.__name__ for fn in PULL_REQUEST_REVIEW_TOOLS),
]
