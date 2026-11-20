"""Program-scale read and traceability tools for GitHub Projects.

The tools in this module compose GitHub's REST and Projects V2 APIs without
embedding repository-specific policy.  They are deliberately read-heavy;
mutations return structured partial results and are safe to retry only after
the caller reconciles the target resource.
"""
from __future__ import annotations

import json
import logging
import re
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from core.access import resolve_repository
from core.error_handling import build_error_response, handle_tool_error
from core.factory import get_service_factory
from models.responses import ToolSuccess

logger = logging.getLogger(__name__)


class RepositoryTarget(BaseModel):
    """Optional repository target accepted by program-scale tools."""

    repository: str | None = Field(default=None, description="Repository as owner/name")
    owner: str | None = Field(default=None, description="Repository owner override")
    repo: str | None = Field(default=None, description="Repository name override")

    @model_validator(mode="after")
    def validate_repository(self) -> "RepositoryTarget":
        if self.repository is not None:
            parts = self.repository.split("/")
            if len(parts) != 2 or not all(parts):
                raise ValueError("repository must use the owner/name format")
            if self.owner is not None or self.repo is not None:
                raise ValueError("repository cannot be combined with owner or repo")
        return self


def _repository(params: RepositoryTarget) -> str:
    """Resolve and scope-check a repository target."""
    if params.repository:
        owner, repo = params.repository.split("/", 1)
        return resolve_repository(owner, repo)
    return resolve_repository(params.owner, params.repo)


async def _json(gh: Any, args: list[str], default: Any = None) -> Any:
    """Run a read-only gh command and decode its JSON response."""
    result = await gh.run(args)
    if not result.stdout.strip():
        return default
    return json.loads(result.stdout)


def _success(data: dict[str, Any]) -> dict:
    return ToolSuccess(data=data).model_dump()


# GitHub's compare API returns at most 250 commits; associated-PR lookups are one call per commit, so they are capped.
_MAX_COMMITS_FOR_PR_LOOKUP = 250


async def _pull_requests_in_comparison(gh: Any, repo: str, comparison: dict, base: str, head: str) -> dict[str, Any]:
    """Return the merged pull requests whose commits are in ``base...head``.

    The included PRs of a promotion are the ones merged into ``head`` that ``base`` does not contain yet, so they are
    resolved from the compared commits (``GET /repos/{repo}/commits/{sha}/pulls``), not from PRs whose head branch is
    ``head`` (those are earlier promotions). Earlier promotion PRs ``head -> base`` are excluded.
    """
    commits = comparison.get("commits", []) if isinstance(comparison, dict) else []
    shas = [c.get("sha") for c in commits if isinstance(c, dict) and c.get("sha")]
    seen: dict[int, dict[str, Any]] = {}
    for sha in shas[:_MAX_COMMITS_FOR_PR_LOOKUP]:
        prs = await _json(gh, ["api", f"repos/{repo}/commits/{sha}/pulls?per_page=100"], [])
        for p in prs if isinstance(prs, list) else []:
            if not isinstance(p, dict) or p.get("number") is None or not p.get("merged_at"):
                continue
            if (p.get("head") or {}).get("ref") == head and (p.get("base") or {}).get("ref") == base:
                continue
            seen.setdefault(int(p["number"]), {
                "number": p.get("number"),
                "title": p.get("title"),
                "state": p.get("state"),
                "url": p.get("html_url"),
                "base": (p.get("base") or {}).get("ref"),
                "merged_at": p.get("merged_at"),
            })
    total = comparison.get("total_commits", len(shas)) if isinstance(comparison, dict) else len(shas)
    return {
        "pull_requests": sorted(seen.values(), key=lambda p: p["number"]),
        "commits_scanned": min(len(shas), _MAX_COMMITS_FOR_PR_LOOKUP),
        "truncated": bool(total and total > min(len(shas), _MAX_COMMITS_FOR_PR_LOOKUP)),
    }


def _error(message: str, *, error_type: str = "validation", suggestion: str | None = None) -> dict:
    return build_error_response(error_type=error_type, message=message, suggestion=suggestion)


class MergeReadinessInput(RepositoryTarget):
    """Input for pull-request merge readiness diagnostics."""

    pull_number: int = Field(gt=0)


async def get_pull_request_merge_readiness(params: MergeReadinessInput) -> dict:
    """Explain why a pull request can or cannot merge.

    The response combines PR mergeability, reviews, checks, branch protection,
    rulesets, and ahead/behind counts into a diagnosis with a suggested action.
    """
    try:
        repo = _repository(params)
        gh = get_service_factory().gh()
        pr = await _json(gh, ["api", f"repos/{repo}/pulls/{params.pull_number}"])
        if not isinstance(pr, dict) or not pr.get("number"):
            return _error(f"Pull request #{params.pull_number} was not found.", error_type="not_found")
        base = ((pr.get("base") or {}).get("ref") or "")
        head = ((pr.get("head") or {}).get("ref") or "")
        sha = ((pr.get("head") or {}).get("sha") or "")
        checks = await _json(gh, ["api", f"repos/{repo}/commits/{sha}/check-runs?per_page=100"], {"check_runs": []})
        statuses = await _json(gh, ["api", f"repos/{repo}/commits/{sha}/status"], {"statuses": []})
        reviews = await _json(gh, ["api", f"repos/{repo}/pulls/{params.pull_number}/reviews?per_page=100"], [])
        try:
            protection = await _json(gh, ["api", f"repos/{repo}/branches/{base}/protection"], {})
        except Exception:
            protection = {}
        try:
            rulesets = await _json(gh, ["api", f"repos/{repo}/rulesets?includes_parents=true&per_page=100"], [])
        except Exception:
            rulesets = []
        compare = await _json(gh, ["api", f"repos/{repo}/compare/{base}...{head}"], {})
        check_runs = checks.get("check_runs", []) if isinstance(checks, dict) else []
        check_states = [str(item.get("conclusion") or item.get("status") or "unknown").lower() for item in check_runs]
        status_states = [str(item.get("state") or "unknown").lower() for item in (statuses.get("statuses", []) if isinstance(statuses, dict) else [])]
        required_checks = [item for item in check_runs if item.get("name")]
        failed = any(state in {"failure", "cancelled", "timed_out", "error"} for state in check_states + status_states)
        pending = any(state in {"queued", "in_progress", "pending"} for state in check_states + status_states)
        latest_reviews: dict[str, str] = {}
        for review in reviews if isinstance(reviews, list) else []:
            user = ((review.get("user") or {}).get("login") or "unknown")
            state = str(review.get("state") or "PENDING").upper()
            # A comment or a pending draft does not change a reviewer's decision; a dismissal withdraws it.
            if state in {"COMMENTED", "PENDING"}:
                continue
            if state == "DISMISSED":
                latest_reviews.pop(user, None)
                continue
            latest_reviews[user] = state
        review_decision = "CHANGES_REQUESTED" if "CHANGES_REQUESTED" in latest_reviews.values() else ("APPROVED" if latest_reviews and all(v == "APPROVED" for v in latest_reviews.values()) else "REVIEW_REQUIRED")
        mergeable = pr.get("mergeable")
        merge_state = str(pr.get("mergeable_state") or "UNKNOWN").upper()
        reasons: list[str] = []
        if pr.get("state") != "open":
            reasons.append(f"the pull request is {pr.get('state')}")
        if pr.get("draft"):
            reasons.append("the pull request is a draft")
        if mergeable is False or merge_state in {"DIRTY", "BLOCKED", "BEHIND", "UNSTABLE"}:
            reasons.append(f"GitHub reports merge state {merge_state}")
        if failed:
            reasons.append("one or more checks failed")
        if pending:
            reasons.append("one or more checks are still pending")
        if review_decision != "APPROVED":
            reasons.append(f"review decision is {review_decision}")
        diagnosis = "Ready to merge." if not reasons else "Cannot merge: " + "; ".join(reasons) + "."
        action = "Merge the pull request." if not reasons else (
            "Update the branch and rerun failed checks." if merge_state in {"BEHIND", "DIRTY"} else "Resolve the listed blockers, then re-run this diagnostic."
        )
        return _success({
            "repository": repo,
            "pull_number": params.pull_number,
            "mergeable": mergeable,
            "mergeStateStatus": merge_state,
            "reviewDecision": review_decision,
            "checks": {"required": required_checks, "states": check_states + status_states, "failed": failed, "pending": pending},
            "branch_rules": {"protection": protection, "matching_rulesets": rulesets if isinstance(rulesets, list) else []},
            "commits": {"ahead": compare.get("ahead_by"), "behind": compare.get("behind_by")},
            "diagnosis": diagnosis,
            "suggested_action": action,
        })
    except Exception as exc:
        logger.error("Merge readiness failed: %s", exc)
        return handle_tool_error(exc, context="Get pull request merge readiness failed")


class CompareBranchesInput(RepositoryTarget):
    """Input for comparing two branches."""

    base: str = Field(min_length=1)
    head: str = Field(min_length=1)


async def compare_branches(params: CompareBranchesInput) -> dict:
    """Return ahead/behind counts, commits, PRs, and file statistics."""
    try:
        repo = _repository(params)
        gh = get_service_factory().gh()
        comparison = await _json(gh, ["api", f"repos/{repo}/compare/{params.base}...{params.head}"], {})
        included = await _pull_requests_in_comparison(gh, repo, comparison, params.base, params.head)
        files = comparison.get("files", []) if isinstance(comparison, dict) else []
        return _success({
            "repository": repo,
            "base": params.base,
            "head": params.head,
            "ahead": comparison.get("ahead_by"),
            "behind": comparison.get("behind_by"),
            "status": comparison.get("status"),
            "commits": [{"sha": c.get("sha"), "message": (c.get("commit") or {}).get("message", "").splitlines()[0]} for c in comparison.get("commits", [])],
            "pull_requests": included["pull_requests"],
            "pull_requests_truncated": included["truncated"],
            "files": [{"filename": f.get("filename"), "status": f.get("status"), "additions": f.get("additions", 0), "deletions": f.get("deletions", 0), "changes": f.get("changes", 0)} for f in files],
            "file_totals": {"files": len(files), "additions": sum(f.get("additions", 0) for f in files), "deletions": sum(f.get("deletions", 0) for f in files)},
        })
    except Exception as exc:
        logger.error("Branch comparison failed: %s", exc)
        return handle_tool_error(exc, context="Compare branches failed")


class PromotionPullRequestInput(RepositoryTarget):
    """Input for creating a generated promotion pull request."""

    head: str = Field(min_length=1)
    base: str = Field(min_length=1)
    title: str | None = None
    body: str | None = None
    draft: bool = False
    back_merge: bool = False


async def create_promotion_pull_request(params: PromotionPullRequestInput) -> dict:
    """Create a promotion PR with an evidence table from included PRs."""
    try:
        repo = _repository(params)
        gh = get_service_factory().gh()
        comparison = await _json(gh, ["api", f"repos/{repo}/compare/{params.base}...{params.head}"], {})
        included = await _pull_requests_in_comparison(gh, repo, comparison, params.base, params.head)
        prs = included["pull_requests"]
        rows = []
        for p in prs:
            title = str(p.get("title") or "").replace("|", "\\|")
            rows.append(f"| #{p.get('number')} | {title} |")
        generated = "## Included pull requests\n\n| PR | Title |\n| --- | --- |\n" + ("\n".join(rows) or "| — | No pull requests found |")
        if included["truncated"]:
            generated += "\n\n> The comparison has more commits than were scanned; the table may be incomplete."
        if comparison.get("behind_by", 0):
            generated += f"\n\n> The base branch is {comparison['behind_by']} commit(s) ahead of the promotion head. Consider a back-merge before merging."
        body = (params.body.rstrip() + "\n\n" if params.body else "") + generated
        result = await gh.run(["api", "--method", "POST", f"repos/{repo}/pulls", "-f", f"title={params.title or f'{params.head} → {params.base} promotion'}", "-f", f"head={params.head}", "-f", f"base={params.base}", "-f", f"body={body}", "-F", f"draft={'true' if params.draft else 'false'}"])
        pr = json.loads(result.stdout)
        data = {"repository": repo, "pr_number": pr.get("number"), "pr_url": pr.get("html_url"), "head": params.head, "base": params.base, "ahead": comparison.get("ahead_by"), "behind": comparison.get("behind_by"), "included_pull_requests": [p.get("number") for p in prs], "back_merge_required": bool(comparison.get("behind_by")), "back_merge_requested": params.back_merge}
        if params.back_merge and comparison.get("behind_by"):
            back_result = await gh.run(["api", "--method", "POST", f"repos/{repo}/pulls", "-f", f"title=Back-merge {params.base} into {params.head}", "-f", f"head={params.base}", "-f", f"base={params.head}", "-f", "body=Back-merge the promoted base branch to keep the source branch current."])
            back_pr = json.loads(back_result.stdout)
            data["back_merge_pr"] = {"pr_number": back_pr.get("number"), "pr_url": back_pr.get("html_url"), "head": params.base, "base": params.head}
        return _success(data)
    except Exception as exc:
        logger.error("Promotion PR creation failed: %s", exc)
        return handle_tool_error(exc, context="Create promotion pull request failed")


class RulesetInput(RepositoryTarget):
    """Input for repository ruleset inspection."""

    diff_ruleset: str | None = Field(default=None, description="JSON object to compare with effective rulesets")


async def list_repository_rulesets(params: RulesetInput) -> dict:
    """List repository rulesets and optionally report JSON drift."""
    try:
        repo = _repository(params)
        rulesets = await _json(get_service_factory().gh(), ["api", f"repos/{repo}/rulesets?includes_parents=true&per_page=100"], [])
        if isinstance(rulesets, list):
            details = []
            gh = get_service_factory().gh()
            for ruleset in rulesets:
                if isinstance(ruleset, dict) and ruleset.get("id"):
                    try:
                        details.append(await _json(gh, ["api", f"repos/{repo}/rulesets/{ruleset['id']}"], ruleset))
                    except Exception:
                        details.append(ruleset)
            rulesets = details
        data: dict[str, Any] = {"repository": repo, "rulesets": rulesets}
        if params.diff_ruleset:
            expected = json.loads(params.diff_ruleset)
            actual = rulesets if isinstance(rulesets, list) else []
            data["drift"] = {"expected_count": len(expected) if isinstance(expected, list) else 1, "actual_count": len(actual), "matches": expected == actual, "expected": expected}
        return _success(data)
    except json.JSONDecodeError:
        return _error("diff_ruleset must be valid JSON")
    except Exception as exc:
        logger.error("Ruleset listing failed: %s", exc)
        return handle_tool_error(exc, context="List repository rulesets failed")


class BranchRulesInput(RepositoryTarget):
    """Input for effective branch rule inspection."""

    branch: str = Field(min_length=1)


async def get_branch_rules(params: BranchRulesInput) -> dict:
    """Return branch protection and matching rulesets for a branch."""
    try:
        repo = _repository(params)
        gh = get_service_factory().gh()
        try:
            protection = await _json(gh, ["api", f"repos/{repo}/branches/{params.branch}/protection"], {})
        except Exception:
            protection = None
        rulesets = await _json(gh, ["api", f"repos/{repo}/rulesets?includes_parents=true&per_page=100"], [])
        matching = [r for r in rulesets if isinstance(r, dict) and (not r.get("conditions") or params.branch in json.dumps(r.get("conditions")))] if isinstance(rulesets, list) else []
        detailed = []
        for ruleset in matching:
            if ruleset.get("id"):
                try:
                    detailed.append(await _json(gh, ["api", f"repos/{repo}/rulesets/{ruleset['id']}"], ruleset))
                except Exception:
                    detailed.append(ruleset)
            else:
                detailed.append(ruleset)
        ruleset_rules = [
            rule
            for ruleset in detailed
            for rule in ruleset.get("rules", [])
            if isinstance(rule, dict)
        ]
        pull_request_rules = [r for r in ruleset_rules if r.get("type") == "pull_request"]
        status_rules = [r for r in ruleset_rules if r.get("type") == "required_status_checks"]
        required_approvals = max(
            [int((r.get("parameters") or {}).get("required_approving_review_count", 0)) for r in pull_request_rules] or [0]
        )
        strict = any(bool((r.get("parameters") or {}).get("strict_required_status_checks_policy")) for r in status_rules)
        return _success({"repository": repo, "branch": params.branch, "branch_protection": protection, "rulesets": detailed, "effective_rules": {"pull_request_required": bool((protection or {}).get("required_pull_request_reviews")) or bool(pull_request_rules), "strict": strict or bool(((protection or {}).get("required_status_checks") or {}).get("strict")), "required_approvals": max(required_approvals, int(((protection or {}).get("required_pull_request_reviews") or {}).get("required_approving_review_count", 0)))}})
    except Exception as exc:
        logger.error("Branch rules failed: %s", exc)
        return handle_tool_error(exc, context="Get branch rules failed")


class ProjectProgressInput(RepositoryTarget):
    """Input for a Project V2 progress report."""

    numeric_field: str = Field(min_length=1)
    group_by: str = "Status"
    repository_filter: str | None = None
    label: str | None = None
    epic_issue_number: int | None = Field(default=None, gt=0)


async def project_progress_report(params: ProjectProgressInput) -> dict:
    """Aggregate Project V2 items by status or another field."""
    try:
        graphql = await get_service_factory().graphql()
        gh = get_service_factory().gh()
        discovery = await get_service_factory().discovery_service(graphql)
        project = await get_service_factory().project_service(graphql, gh)
        metadata = await discovery.get_cached_or_discover()
        items = await project.list_all_items(metadata)
        selected = items
        if params.repository_filter:
            selected = [i for i in selected if (i.issue_url or "").lower().startswith(params.repository_filter.lower().rstrip("/") + "/")]
        if params.label:
            selected = [i for i in selected if params.label in i.labels]
        if params.epic_issue_number:
            sub = await _json(gh, ["api", f"repos/{_repository(params)}/issues/{params.epic_issue_number}/sub_issues?per_page=100"], [])
            numbers = {x.get("number") for x in sub if isinstance(x, dict)}
            selected = [i for i in selected if i.issue_number in numbers]
        groups: dict[str, dict[str, float | int]] = defaultdict(lambda: {"items": 0, "hours": 0.0})
        for item in selected:
            key = getattr(item, "status", None) if params.group_by.casefold() == "status" else item.fields.get(params.group_by)
            key = str(key or "Unassigned")
            raw = item.fields.get(params.numeric_field, item.estimate if params.numeric_field.casefold() == "estimate" else 0) or 0
            try:
                value = float(raw)
            except (TypeError, ValueError):
                value = 0.0
            groups[key]["items"] += 1
            groups[key]["hours"] += value
        total_hours = sum(float(v["hours"]) for v in groups.values())
        total_items = len(selected)
        result_groups = {k: {"items": int(v["items"]), "hours": round(float(v["hours"]), 2), "item_pct": round(int(v["items"]) / max(total_items, 1) * 100, 2), "hours_pct": round(float(v["hours"]) / max(total_hours, 1) * 100, 2)} for k, v in sorted(groups.items())}
        return _success({"project_number": metadata.project_number, "numeric_field": params.numeric_field, "group_by": params.group_by, "total_items": total_items, "total_hours": round(total_hours, 2), "groups": result_groups})
    except Exception as exc:
        logger.error("Project progress failed: %s", exc)
        return handle_tool_error(exc, context="Project progress report failed")


class UpsertIssueBodySectionInput(RepositoryTarget):
    """Input for idempotent issue or pull-request body section updates."""

    number: int = Field(gt=0)
    section: str = Field(min_length=1, max_length=100)
    content: str = Field(max_length=65536)
    kind: Literal["issue", "pull_request"] = "issue"


async def upsert_issue_body_section(params: UpsertIssueBodySectionInput) -> dict:
    """Insert or replace a marker-delimited section in an issue/PR body."""
    try:
        repo = _repository(params)
        gh = get_service_factory().gh()
        endpoint_kind = "pulls" if params.kind == "pull_request" else "issues"
        current = await _json(gh, ["api", f"repos/{repo}/{endpoint_kind}/{params.number}"], {})
        old = current.get("body") or ""
        marker = re.sub(r"[^A-Za-z0-9_.-]+", "-", params.section).strip("-").lower()
        start = f"<!-- mcp:{marker}:start -->"
        end = f"<!-- mcp:{marker}:end -->"
        block = f"{start}\n{params.content}\n{end}"
        pattern = re.compile(re.escape(start) + r".*?" + re.escape(end), re.DOTALL)
        body = pattern.sub(block, old, count=1) if pattern.search(old) else (old.rstrip() + "\n\n" + block).lstrip()
        await gh.run(["api", "--method", "PATCH", f"repos/{repo}/{endpoint_kind}/{params.number}", "-f", f"body={body}"])
        return _success({"repository": repo, "number": params.number, "kind": params.kind, "section": params.section, "changed": body != old})
    except Exception as exc:
        logger.error("Body section upsert failed: %s", exc)
        return handle_tool_error(exc, context="Upsert issue body section failed")


class BotCommentsInput(RepositoryTarget):
    """Input for bot-comment and Terraform-plan extraction."""

    pull_number: int = Field(gt=0)
    author: str | None = None
    pattern: str | None = None
    extract_terraform_plan: bool = False


async def get_pull_request_bot_comments(params: BotCommentsInput) -> dict:
    """List bot-authored PR comments, optionally extracting plan summaries."""
    try:
        repo = _repository(params)
        comments = await _json(get_service_factory().gh(), ["api", f"repos/{repo}/issues/{params.pull_number}/comments?per_page=100"], [])
        regex = re.compile(params.pattern, re.IGNORECASE) if params.pattern else None
        selected = []
        for comment in comments if isinstance(comments, list) else []:
            user = comment.get("user") or {}
            login = str(user.get("login") or "")
            if params.author and login.casefold() != params.author.casefold():
                continue
            if not params.author and user.get("type") != "Bot" and not login.endswith("[bot]"):
                continue
            body = str(comment.get("body") or "")
            if regex and not regex.search(body):
                continue
            item = {"id": comment.get("id"), "author": login, "body": body, "created_at": comment.get("created_at"), "url": comment.get("html_url")}
            if params.extract_terraform_plan:
                match = re.search(r"Plan:\s*(\d+) to add,\s*(\d+) to change,\s*(\d+) to destroy", body, re.IGNORECASE)
                item["terraform_plan"] = ({"add": int(match.group(1)), "change": int(match.group(2)), "destroy": int(match.group(3))} if match else ({"add": 0, "change": 0, "destroy": 0} if re.search(r"No changes", body, re.IGNORECASE) else None))
            selected.append(item)
        return _success({"repository": repo, "pull_number": params.pull_number, "comments": selected, "count": len(selected)})
    except re.error as exc:
        return _error(f"Invalid comment pattern: {exc}")
    except Exception as exc:
        logger.error("Bot comments failed: %s", exc)
        return handle_tool_error(exc, context="Get pull request bot comments failed")


class BulkLinkInput(RepositoryTarget):
    """Input for bulk PR-to-issue traceability links."""

    links: list[dict[str, int]] = Field(min_length=1, max_length=100)
    keyword: Literal["closes", "fixes", "resolves", "refs", "part_of"] = "closes"


async def bulk_link_pull_requests(params: BulkLinkInput) -> dict:
    """Link many pull requests and return one outcome per requested pair."""
    from tools.meta.nice_to_have import LinkPullRequestInput, link_pull_request
    results = []
    for link in params.links:
        try:
            result = await link_pull_request(LinkPullRequestInput(issue_number=link["issue_number"], pr_number=link["pr_number"], keyword=params.keyword, repository=params.repository, owner=params.owner, repo=params.repo))
            results.append({"issue_number": link.get("issue_number"), "pr_number": link.get("pr_number"), "result": result})
        except Exception as exc:
            results.append({"issue_number": link.get("issue_number"), "pr_number": link.get("pr_number"), "result": _error(str(exc), error_type="internal")})
    return _success({"results": results, "total": len(results), "linked": sum(1 for r in results if r["result"].get("ok")), "unlinked": [r for r in results if not r["result"].get("ok")]})


class EpicRollupInput(RepositoryTarget):
    """Input for sub-issue rollups."""

    epic_issue_number: int = Field(gt=0)
    numeric_field: str = "Estimate"


async def epic_rollup(params: EpicRollupInput) -> dict:
    """Aggregate sub-issue states, estimates, blockers, and linked PRs."""
    try:
        repo = _repository(params)
        gh = get_service_factory().gh()
        subissues = await _json(gh, ["api", f"repos/{repo}/issues/{params.epic_issue_number}/sub_issues?per_page=100"], [])
        project_items: dict[int, Any] = {}
        hours_by_state: defaultdict[str, float] = defaultdict(float)
        try:
            graphql = await get_service_factory().graphql()
            discovery = await get_service_factory().discovery_service(graphql)
            project = await get_service_factory().project_service(graphql, gh)
            metadata = await discovery.get_cached_or_discover()
            for item in await project.list_all_items(metadata):
                if item.issue_number is not None:
                    project_items[item.issue_number] = item
        except Exception as project_exc:
            logger.warning("Epic rollup project fields unavailable: %s", project_exc)
        state_counts = Counter(str(x.get("state") or "unknown").lower() for x in subissues if isinstance(x, dict))
        blockers = []
        prs = []
        for issue in subissues if isinstance(subissues, list) else []:
            number = issue.get("number")
            project_item = project_items.get(number)
            if project_item is not None:
                raw_hours = project_item.fields.get(params.numeric_field, project_item.estimate if params.numeric_field.casefold() == "estimate" else 0) or 0
                try:
                    hours_by_state[str(issue.get("state") or "unknown").lower()] += float(raw_hours)
                except (TypeError, ValueError):
                    pass
            deps = await _json(gh, ["api", f"repos/{repo}/issues/{number}/dependencies/blocked_by"], [])
            if deps:
                blockers.append({"issue_number": number, "blocked_by": [d.get("number") for d in deps]})
            linked = await _json(gh, ["api", f"repos/{repo}/issues/{number}/timeline?per_page=100"], [])
            prs.extend({"issue_number": number, "pr_number": e.get("source", {}).get("issue", {}).get("number")} for e in linked if isinstance(e, dict) and e.get("event") == "cross-referenced" and e.get("source", {}).get("issue", {}).get("pull_request"))
        return _success({"repository": repo, "epic_issue_number": params.epic_issue_number, "sub_issue_count": len(subissues) if isinstance(subissues, list) else 0, "states": dict(state_counts), "hours_by_state": {state: round(hours, 2) for state, hours in hours_by_state.items()}, "blockers": blockers, "open_linked_prs": prs})
    except Exception as exc:
        logger.error("Epic rollup failed: %s", exc)
        return handle_tool_error(exc, context="Epic rollup failed")


class ConsistencyCheckInput(RepositoryTarget):
    """Input for comparing expected Project V2 fields with actual values."""

    expected: dict[str, dict[str, Any]] = Field(default_factory=dict)


async def project_items_consistency_check(params: ConsistencyCheckInput) -> dict:
    """Compare expected field values with the live Project V2 board."""
    try:
        graphql = await get_service_factory().graphql()
        gh = get_service_factory().gh()
        discovery = await get_service_factory().discovery_service(graphql)
        project = await get_service_factory().project_service(graphql, gh)
        metadata = await discovery.get_cached_or_discover()
        items = await project.list_all_items(metadata)
        by_key = {str(i.issue_number): i for i in items}
        differences = []
        for key, expected in params.expected.items():
            item = by_key.get(str(key)) or next((i for i in items if i.node_id == key), None)
            if not item:
                differences.append({"item": key, "kind": "missing_item", "expected": expected})
                continue
            for field, value in expected.items():
                actual = item.fields.get(field)
                if field.casefold() == "status":
                    actual = item.status
                if actual != value:
                    differences.append({"item": key, "field": field, "expected": value, "actual": actual})
        return _success({"project_number": metadata.project_number, "checked": len(params.expected), "consistent": not differences, "differences": differences})
    except Exception as exc:
        logger.error("Project consistency check failed: %s", exc)
        return handle_tool_error(exc, context="Project items consistency check failed")


class StaleBranchesInput(RepositoryTarget):
    """Input for stale branch discovery."""

    stale_days: int = Field(default=30, ge=1, le=3650)
    include_protected: bool = False


async def find_stale_branches(params: StaleBranchesInput) -> dict:
    """Find branches with no recent commit and no active work."""
    try:
        repo = _repository(params)
        gh = get_service_factory().gh()
        branches = await _json(gh, ["api", f"repos/{repo}/branches?per_page=100"], [])
        prs = await _json(gh, ["api", f"repos/{repo}/pulls?state=open&per_page=100"], [])
        cutoff = datetime.now(UTC) - timedelta(days=params.stale_days)
        stale = []
        for branch in branches if isinstance(branches, list) else []:
            if not params.include_protected and branch.get("protected"):
                continue
            name = branch.get("name")
            commit = branch.get("commit") or {}
            detail = await _json(gh, ["api", f"repos/{repo}/commits/{commit.get('sha')}"], {})
            date_text = ((detail.get("commit") or {}).get("committer") or {}).get("date")
            if not date_text:
                continue
            committed = datetime.fromisoformat(date_text.replace("Z", "+00:00"))
            active = [p.get("number") for p in prs if ((p.get("head") or {}).get("ref") == name)]
            if committed < cutoff and not active:
                stale.append({"name": name, "last_commit": date_text, "open_pull_requests": active, "protected": bool(branch.get("protected"))})
        return _success({"repository": repo, "stale_days": params.stale_days, "branches": stale, "count": len(stale)})
    except Exception as exc:
        logger.error("Stale branch scan failed: %s", exc)
        return handle_tool_error(exc, context="Find stale branches failed")


class OpenPRsByBaseInput(RepositoryTarget):
    """Input for listing open PRs targeting a base branch."""

    base: str | None = None


async def list_open_prs_by_base(params: OpenPRsByBaseInput) -> dict:
    """List open PRs grouped by destination branch."""
    try:
        repo = _repository(params)
        prs = await _json(get_service_factory().gh(), ["api", f"repos/{repo}/pulls?state=open&per_page=100"], [])
        selected = [p for p in prs if not params.base or ((p.get("base") or {}).get("ref") == params.base)]
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for p in selected:
            base = (p.get("base") or {}).get("ref") or "unknown"
            grouped[base].append({"number": p.get("number"), "title": p.get("title"), "head": (p.get("head") or {}).get("ref"), "url": p.get("html_url"), "draft": p.get("draft", False)})
        return _success({"repository": repo, "base": params.base, "count": len(selected), "by_base": dict(grouped)})
    except Exception as exc:
        logger.error("Open PR listing failed: %s", exc)
        return handle_tool_error(exc, context="List open pull requests by base failed")


__all__ = [name for name in globals() if not name.startswith("_")]
