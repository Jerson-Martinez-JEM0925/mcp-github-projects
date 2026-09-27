"""MCP tools for PR-to-issue lifecycle automation.

Exposes tools for:
- Verifying acceptance criteria on issues
- Extracting linked issues from PR bodies/commits
- Validating issue closure readiness
- Closing issues automatically when linked PRs are merged
"""

from __future__ import annotations

import json
import logging
import re
from typing import Optional

from pydantic import BaseModel, Field

from clients.gh_cli_client import CLIError
from core.config import get_settings
from core.error_handling import build_error_response, handle_tool_error
from models.responses import ToolSuccess
from core.factory import get_service_factory
from services.project_service import bare_option_name

logger = logging.getLogger(__name__)

# Regex for closing keywords in PR bodies/commits (case-insensitive).
_CLOSING_PATTERN = re.compile(
    r"(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)",
    re.IGNORECASE,
)

# Regex for checkbox items.
_CHECKBOX_CHECKED = re.compile(r"^\s*-\s*\[x\]\s*(.+)", re.IGNORECASE)
_CHECKBOX_UNCHECKED = re.compile(r"^\s*-\s*\[\s\]\s*(.+)")


# ── Input Models ─────────────────────────────────────────────────────────────


class VerifyAcceptanceCriteriaInput(BaseModel):
    """Input schema for the verify_acceptance_criteria tool."""

    issue_number: int = Field(description="GitHub issue number to check")


class GetPRLinkedIssuesInput(BaseModel):
    """Input schema for the get_pr_linked_issues tool."""

    pr_number: int = Field(description="GitHub pull request number")


class ValidateIssueClosureReadinessInput(BaseModel):
    """Input schema for the validate_issue_closure_readiness tool."""

    issue_number: int = Field(description="GitHub issue number to validate")
    required_labels: list[str] = Field(
        default_factory=list,
        description="Labels that must be present on the issue (empty means no label requirement)",
    )


class CloseIssueOnPRMergeInput(BaseModel):
    """Input schema for the close_issue_on_pr_merge tool."""

    pr_number: int = Field(description="GitHub pull request number that was merged")


# ── Helper Functions ─────────────────────────────────────────────────────────


def _extract_acceptance_criteria(body: str) -> tuple[list[str], list[str]]:
    """Parse acceptance criteria checkboxes from an issue body.

    Looks for a section headed '## Criterio de aceptación' or
    '## Acceptance Criteria' (case-insensitive) and extracts all
    checkbox items within it.

    Returns:
        A tuple of (checked_items, unchecked_items).
    """
    if not body:
        return [], []

    lines = body.splitlines()
    in_section = False
    checked: list[str] = []
    unchecked: list[str] = []

    for line in lines:
        stripped = line.strip()

        # Detect the acceptance criteria section header.
        if stripped.startswith("##"):
            header_text = stripped.lstrip("#").strip().lower()
            if header_text in (
                "criterio de aceptación",
                "criterios de aceptación",
                "acceptance criteria",
            ):
                in_section = True
                continue
            elif in_section:
                # We hit another ## section — stop parsing.
                break

        if not in_section:
            continue

        # Parse checkboxes.
        match_checked = _CHECKBOX_CHECKED.match(line)
        if match_checked:
            checked.append(match_checked.group(1).strip())
            continue

        match_unchecked = _CHECKBOX_UNCHECKED.match(line)
        if match_unchecked:
            unchecked.append(match_unchecked.group(1).strip())

    return checked, unchecked


def _extract_closing_references(text: str) -> list[int]:
    """Extract issue numbers from closing keyword references.

    Parses 'Closes #N', 'Fixes #N', 'Resolves #N' patterns
    (case-insensitive) from the given text.

    Returns:
        Deduplicated, sorted list of issue numbers.
    """
    if not text:
        return []
    matches = _CLOSING_PATTERN.findall(text)
    return sorted(set(int(m) for m in matches))


# ── Tool Functions ───────────────────────────────────────────────────────────


async def verify_acceptance_criteria(params: VerifyAcceptanceCriteriaInput) -> dict:
    """Verify acceptance criteria completion on a GitHub issue.

    Fetches the issue body and parses all checkboxes in the
    '## Criterio de aceptación' (or '## Acceptance Criteria') section.
    Returns the total count, checked count, unchecked item texts, and
    whether all criteria are met.

    Args:
        params: Input containing the issue number.

    Returns:
        ToolSuccess with criteria verification results, or ToolError on failure.
    """
    try:
        await get_service_factory().ensure_auth()
        settings = get_settings()
        repo = f"{settings.org_name}/{settings.repo_name}"
        gh_client = get_service_factory().gh()

        result = await gh_client.run([
            "issue", "view", str(params.issue_number),
            "--repo", repo,
            "--json", "number,title,body,state",
        ])
        issue = json.loads(result.stdout)
        body = issue.get("body", "") or ""

        checked, unchecked = _extract_acceptance_criteria(body)
        total = len(checked) + len(unchecked)

        # No criteria section found — treat as all met with a note.
        if total == 0:
            return ToolSuccess(
                data={
                    "issue_number": params.issue_number,
                    "title": issue.get("title", ""),
                    "all_met": True,
                    "total": 0,
                    "checked": 0,
                    "unchecked_items": [],
                    "note": (
                        "No acceptance criteria section found in issue body. "
                        "Treating as implicitly satisfied."
                    ),
                },
            ).model_dump()

        return ToolSuccess(
            data={
                "issue_number": params.issue_number,
                "title": issue.get("title", ""),
                "all_met": len(unchecked) == 0,
                "total": total,
                "checked": len(checked),
                "unchecked_items": unchecked,
            },
        ).model_dump()

    except CLIError as exc:
        logger.error("CLI error in verify_acceptance_criteria: %s", exc)
        stderr_lower = exc.stderr.lower()
        if "not found" in stderr_lower or "could not resolve" in stderr_lower:
            return build_error_response(
                error_type="not_found",
                message=f"Issue #{params.issue_number} not found.",
                suggestion="Verify the issue number is correct.",
            )
        return build_error_response(
            error_type="internal",
            message=f"Failed to fetch issue: {exc.stderr.strip()}",
            suggestion="Check network connectivity and token permissions.",
        )
    except Exception as exc:
        logger.error("Error in verify_acceptance_criteria: %s", exc)
        return handle_tool_error(exc, context="Verify acceptance criteria failed")


async def get_pr_linked_issues(params: GetPRLinkedIssuesInput) -> dict:
    """Get all issues linked to a pull request via closing keywords.

    Parses the PR body and commit messages for 'Closes #N', 'Fixes #N',
    'Resolves #N' references (case-insensitive).

    Args:
        params: Input containing the PR number.

    Returns:
        ToolSuccess with linked issue numbers and PR state, or ToolError on failure.
    """
    try:
        await get_service_factory().ensure_auth()
        settings = get_settings()
        repo = f"{settings.org_name}/{settings.repo_name}"
        gh_client = get_service_factory().gh()

        # Fetch PR details.
        pr_result = await gh_client.run([
            "pr", "view", str(params.pr_number),
            "--repo", repo,
            "--json", "number,title,body,state,commits",
        ])
        pr_data = json.loads(pr_result.stdout)

        pr_body = pr_data.get("body", "") or ""
        pr_state = pr_data.get("state", "UNKNOWN")

        # Collect closing references from PR body.
        linked_issues = _extract_closing_references(pr_body)

        # Also scan commit messages for closing references.
        commits = pr_data.get("commits", []) or []
        for commit in commits:
            msg = commit.get("messageHeadline", "") or ""
            msg_body = commit.get("messageBody", "") or ""
            commit_refs = _extract_closing_references(f"{msg} {msg_body}")
            for ref in commit_refs:
                if ref not in linked_issues:
                    linked_issues.append(ref)

        linked_issues.sort()

        return ToolSuccess(
            data={
                "pr_number": params.pr_number,
                "pr_title": pr_data.get("title", ""),
                "pr_state": pr_state,
                "linked_issues": linked_issues,
                "linked_count": len(linked_issues),
            },
        ).model_dump()

    except CLIError as exc:
        logger.error("CLI error in get_pr_linked_issues: %s", exc)
        stderr_lower = exc.stderr.lower()
        if "not found" in stderr_lower or "could not resolve" in stderr_lower:
            return build_error_response(
                error_type="not_found",
                message=f"Pull request #{params.pr_number} not found.",
                suggestion="Verify the PR number is correct.",
            )
        return build_error_response(
            error_type="internal",
            message=f"Failed to fetch PR: {exc.stderr.strip()}",
            suggestion="Check network connectivity and token permissions.",
        )
    except Exception as exc:
        logger.error("Error in get_pr_linked_issues: %s", exc)
        return handle_tool_error(exc, context="Get PR linked issues failed")


async def validate_issue_closure_readiness(params: ValidateIssueClosureReadinessInput) -> dict:
    """Validate whether a GitHub issue is ready to be closed.

    Checks three conditions:
    1. Acceptance criteria are all met (or no criteria section exists).
    2. At least one merged PR is linked to the issue.
    3. Required labels (if specified) are present on the issue.

    Args:
        params: Input containing the issue number and optional required labels.

    Returns:
        ToolSuccess with readiness report, or ToolError on failure.
    """
    try:
        await get_service_factory().ensure_auth()
        settings = get_settings()
        repo = f"{settings.org_name}/{settings.repo_name}"
        gh_client = get_service_factory().gh()

        # Fetch issue details.
        issue_result = await gh_client.run([
            "issue", "view", str(params.issue_number),
            "--repo", repo,
            "--json", "number,title,body,state,labels",
        ])
        issue = json.loads(issue_result.stdout)

        blockers: list[str] = []
        linked_prs: list[dict] = []

        # 1. Check acceptance criteria.
        body = issue.get("body", "") or ""
        checked, unchecked = _extract_acceptance_criteria(body)
        total_criteria = len(checked) + len(unchecked)

        if total_criteria > 0 and len(unchecked) > 0:
            blockers.append(
                f"Acceptance criteria not fully met: {len(unchecked)}/{total_criteria} "
                f"items pending ({', '.join(unchecked[:5])})"
            )

        # 2. Check for linked merged PRs.
        # Search PRs that reference this issue.
        try:
            search_result = await gh_client.run([
                "pr", "list",
                "--repo", repo,
                "--state", "all",
                "--search", f"linked:issue:{params.issue_number}",
                "--json", "number,title,state",
                "--limit", "20",
            ])
            prs = json.loads(search_result.stdout) if search_result.stdout.strip() else []
        except CLIError:
            # Fallback: search by body text mention.
            try:
                search_result = await gh_client.run([
                    "pr", "list",
                    "--repo", repo,
                    "--state", "all",
                    "--search", f"#{params.issue_number} in:body",
                    "--json", "number,title,state",
                    "--limit", "20",
                ])
                prs = json.loads(search_result.stdout) if search_result.stdout.strip() else []
            except CLIError:
                prs = []

        for pr in prs:
            linked_prs.append({
                "number": pr.get("number"),
                "title": pr.get("title"),
                "state": pr.get("state"),
            })

        merged_prs = [p for p in linked_prs if p.get("state") == "MERGED"]
        if not merged_prs:
            blockers.append("No merged pull request found linked to this issue.")

        # 3. Check required labels.
        issue_labels = {lbl.get("name", "") for lbl in issue.get("labels", [])}
        if params.required_labels:
            missing_labels = [lbl for lbl in params.required_labels if lbl not in issue_labels]
            if missing_labels:
                blockers.append(
                    f"Missing required labels: {', '.join(missing_labels)}"
                )

        ready = len(blockers) == 0

        return ToolSuccess(
            data={
                "issue_number": params.issue_number,
                "title": issue.get("title", ""),
                "state": issue.get("state"),
                "ready": ready,
                "blockers": blockers,
                "linked_prs": linked_prs,
                "merged_prs_count": len(merged_prs),
            },
        ).model_dump()

    except CLIError as exc:
        logger.error("CLI error in validate_issue_closure_readiness: %s", exc)
        stderr_lower = exc.stderr.lower()
        if "not found" in stderr_lower or "could not resolve" in stderr_lower:
            return build_error_response(
                error_type="not_found",
                message=f"Issue #{params.issue_number} not found.",
                suggestion="Verify the issue number is correct.",
            )
        return build_error_response(
            error_type="internal",
            message=f"Failed to validate issue readiness: {exc.stderr.strip()}",
            suggestion="Check network connectivity and token permissions.",
        )
    except Exception as exc:
        logger.error("Error in validate_issue_closure_readiness: %s", exc)
        return handle_tool_error(exc, context="Validate issue closure readiness failed")


async def close_issue_on_pr_merge(params: CloseIssueOnPRMergeInput) -> dict:
    """Close linked issues when a pull request is merged.

    The main automation workflow:
    1. Verifies the PR is merged.
    2. Extracts all linked issues from the PR body and commits.
    3. For each linked issue:
       - Verifies acceptance criteria are met.
       - If all met: closes the issue with a completion comment.
       - If not met: comments on the issue with pending criteria.
    4. Returns a summary of all processed issues.

    Args:
        params: Input containing the merged PR number.

    Returns:
        ToolSuccess with processing summary, or ToolError on failure.
    """
    try:
        await get_service_factory().ensure_auth()
        settings = get_settings()
        repo = f"{settings.org_name}/{settings.repo_name}"
        gh_client = get_service_factory().gh()

        # Step 1: Fetch PR and verify it's merged.
        pr_result = await gh_client.run([
            "pr", "view", str(params.pr_number),
            "--repo", repo,
            "--json", "number,title,body,state,commits,mergedAt,mergedBy",
        ])
        pr_data = json.loads(pr_result.stdout)
        pr_state = pr_data.get("state", "UNKNOWN")

        if pr_state != "MERGED":
            return build_error_response(
                error_type="validation",
                message=(
                    f"PR #{params.pr_number} is not merged (current state: {pr_state}). "
                    "This tool only processes merged PRs."
                ),
                suggestion="Wait for the PR to be merged before running this tool.",
            )

        # Step 2: Extract linked issues.
        pr_body = pr_data.get("body", "") or ""
        linked_issues = _extract_closing_references(pr_body)

        commits = pr_data.get("commits", []) or []
        for commit in commits:
            msg = commit.get("messageHeadline", "") or ""
            msg_body = commit.get("messageBody", "") or ""
            commit_refs = _extract_closing_references(f"{msg} {msg_body}")
            for ref in commit_refs:
                if ref not in linked_issues:
                    linked_issues.append(ref)

        linked_issues.sort()

        if not linked_issues:
            return ToolSuccess(
                data={
                    "pr_number": params.pr_number,
                    "pr_title": pr_data.get("title", ""),
                    "processed_issues": [],
                    "summary": "No linked issues found in PR body or commits.",
                },
            ).model_dump()

        # Step 3: Process each linked issue.
        processed: list[dict] = []

        for issue_number in linked_issues:
            issue_result_data: dict = {
                "issue_number": issue_number,
                "action": "skipped",
                "reason": "",
            }

            try:
                # Fetch the issue.
                issue_res = await gh_client.run([
                    "issue", "view", str(issue_number),
                    "--repo", repo,
                    "--json", "number,title,body,state",
                ])
                issue = json.loads(issue_res.stdout)

                # Skip if already closed.
                if issue.get("state", "").upper() == "CLOSED":
                    issue_result_data["action"] = "skipped"
                    issue_result_data["reason"] = "Issue already closed."
                    issue_result_data["title"] = issue.get("title", "")
                    processed.append(issue_result_data)
                    continue

                # Check acceptance criteria.
                body = issue.get("body", "") or ""
                checked, unchecked = _extract_acceptance_criteria(body)
                total_criteria = len(checked) + len(unchecked)

                if total_criteria > 0 and len(unchecked) > 0:
                    # Not all criteria met — comment with pending items.
                    pending_list = "\n".join(f"- [ ] {item}" for item in unchecked)
                    comment_body = (
                        f"⚠️ PR #{params.pr_number} was merged but the following "
                        f"acceptance criteria are still pending:\n\n"
                        f"{pending_list}\n\n"
                        f"Please verify these items before closing this issue."
                    )
                    await gh_client.run([
                        "issue", "comment", str(issue_number),
                        "--repo", repo,
                        "--body", comment_body,
                    ])
                    issue_result_data["action"] = "commented"
                    issue_result_data["reason"] = (
                        f"{len(unchecked)}/{total_criteria} criteria pending."
                    )
                    issue_result_data["unchecked_items"] = unchecked
                    issue_result_data["title"] = issue.get("title", "")
                else:
                    # All criteria met (or none defined) — close the issue.
                    merged_by = pr_data.get("mergedBy", {}).get("login", "unknown")
                    close_comment = (
                        f"✅ All acceptance criteria verified. "
                        f"Closed via PR #{params.pr_number} "
                        f"(merged by @{merged_by})."
                    )
                    await gh_client.run([
                        "issue", "comment", str(issue_number),
                        "--repo", repo,
                        "--body", close_comment,
                    ])
                    await gh_client.run([
                        "issue", "close", str(issue_number),
                        "--repo", repo,
                    ])
                    issue_result_data["action"] = "closed"
                    issue_result_data["reason"] = "All criteria met. Issue closed."
                    issue_result_data["title"] = issue.get("title", "")

            except CLIError as issue_exc:
                logger.warning(
                    "Failed to process issue #%d: %s", issue_number, issue_exc.stderr
                )
                issue_result_data["action"] = "error"
                issue_result_data["reason"] = f"CLI error: {issue_exc.stderr.strip()}"

            processed.append(issue_result_data)

        # Step 4: Build summary.
        closed_count = sum(1 for p in processed if p["action"] == "closed")
        commented_count = sum(1 for p in processed if p["action"] == "commented")
        error_count = sum(1 for p in processed if p["action"] == "error")

        return ToolSuccess(
            data={
                "pr_number": params.pr_number,
                "pr_title": pr_data.get("title", ""),
                "processed_issues": processed,
                "summary": (
                    f"Processed {len(processed)} linked issue(s): "
                    f"{closed_count} closed, {commented_count} partially met, "
                    f"{error_count} error(s)."
                ),
                "closed_count": closed_count,
                "commented_count": commented_count,
                "error_count": error_count,
            },
        ).model_dump()

    except CLIError as exc:
        logger.error("CLI error in close_issue_on_pr_merge: %s", exc)
        stderr_lower = exc.stderr.lower()
        if "not found" in stderr_lower or "could not resolve" in stderr_lower:
            return build_error_response(
                error_type="not_found",
                message=f"PR #{params.pr_number} not found.",
                suggestion="Verify the PR number is correct.",
            )
        return build_error_response(
            error_type="internal",
            message=f"Failed to process PR merge: {exc.stderr.strip()}",
            suggestion="Check network connectivity and token permissions.",
        )
    except Exception as exc:
        logger.error("Error in close_issue_on_pr_merge: %s", exc)
        return handle_tool_error(exc, context="Close issue on PR merge failed")


class SyncClosedItemsToDoneInput(BaseModel):
    """Input schema for the sync_closed_items_to_done tool."""

    dry_run: bool = Field(
        default=False,
        description="If true, only report what WOULD move; do not mutate the board.",
    )
    issue_or_pr_number: Optional[int] = Field(
        default=None,
        description="Limit reconciliation to a single issue/PR number. None = scan the whole board.",
    )
    done_status: str = Field(
        default="✅ Done",
        description="Name of the destination Status column (default '✅ Done'). "
        "A leading emoji and case are optional ('Done' matches '✅ Done').",
    )
    keep_statuses: list[str] = Field(
        default_factory=lambda: ["✅ Done", "🗑️ Trash"],
        description="Status columns treated as terminal (never moved). The destination "
        "done_status is ALWAYS treated as terminal too, even if omitted here.",
    )


async def sync_closed_items_to_done(params: SyncClosedItemsToDoneInput) -> dict:
    """Reconcile the board: move items whose linked issue/PR is CLOSED or MERGED to Done.

    The board does not auto-advance an item to Done when its issue/PR is closed
    or its PR is merged, so cards linger in 'In Progress'. This tool scans the
    project, finds every item that is NOT in a terminal column (``keep_statuses``
    plus ``done_status`` itself) but whose linked content state is CLOSED
    (issue/PR) or MERGED (PR), and moves it to ``done_status``. Idempotent:
    items already at the destination (or in another terminal column) are never
    candidates, including when a custom ``done_status`` is supplied.

    Owner-type (organization vs user) is handled transparently by
    ``ProjectService.list_all_items`` / ``_fetch_all_items``. The scan is
    EXHAUSTIVE — it pages the entire board past ``GH_PROJECT_MAX_ITEMS``, so
    boards with more than 200 cards are fully reconciled in a single call
    (including the ``issue_or_pr_number`` scope, which filters the full board).

    Args:
        params: dry_run (report only), issue_or_pr_number (limit to one),
            done_status, and keep_statuses (terminal columns).

    Returns:
        ToolSuccess with counters (scanned, moved/would_move, skipped, errors)
        and the affected item numbers, or ToolError on failure.
    """

    # Content states that mean "the work is finished" and the card should be Done.
    _CLOSED_STATES = {"CLOSED", "MERGED"}

    try:
        graphql_client = await get_service_factory().graphql()
        gh_client = get_service_factory().gh()
        discovery = await get_service_factory().discovery_service(graphql_client)
        project_service = await get_service_factory().project_service(graphql_client, gh_client)
        metadata = await discovery.get_cached_or_discover()

        items = await project_service.list_all_items(metadata=metadata)
        # The destination is ALWAYS terminal, so a custom done_status stays
        # idempotent even if the caller did not list it in keep_statuses.
        # Compared emoji- and case-insensitively (same rule update_field uses
        # to resolve an option), so a board whose column is a bare "Done"
        # does not see every finished card as a candidate for "✅ Done".
        terminal_statuses = {
            bare_option_name(name)
            for name in (*params.keep_statuses, params.done_status)
        }

        moved: list[dict] = []
        would_move: list[dict] = []
        skipped_open = 0
        errors: list[dict] = []

        for item in items:
            # Optional single-item scope.
            if (
                params.issue_or_pr_number is not None
                and item.issue_number != params.issue_or_pr_number
            ):
                continue
            # Already terminal — never a candidate (idempotent).
            if item.status and bare_option_name(item.status) in terminal_statuses:
                continue
            # Only reconcile items whose linked content is closed/merged.
            if (item.content_state or "").upper() not in _CLOSED_STATES:
                skipped_open += 1
                continue

            entry = {
                "number": item.issue_number,
                "type": item.content_type,
                "content_state": item.content_state,
                "from_status": item.status,
                "item_id": item.node_id,
            }

            if params.dry_run:
                would_move.append(entry)
                continue

            try:
                await project_service.update_field(
                    metadata=metadata,
                    item_id=item.node_id,
                    field_name="Status",
                    value=params.done_status,
                )
                moved.append(entry)
            except Exception as move_exc:  # noqa: BLE001 - collect per-item errors
                logger.error(
                    "sync_closed_items_to_done: failed to move item %s (#%s): %s",
                    item.node_id,
                    item.issue_number,
                    move_exc,
                )
                errors.append({**entry, "error": str(move_exc)})

        data = {
            "dry_run": params.dry_run,
            "done_status": params.done_status,
            "scanned": len(items),
            "skipped_open": skipped_open,
            "errors": errors,
        }
        if params.dry_run:
            data["would_move_count"] = len(would_move)
            data["would_move"] = would_move
            data["message"] = (
                f"Dry run: {len(would_move)} item(s) would move to "
                f"'{params.done_status}'."
            )
        else:
            data["moved_count"] = len(moved)
            data["moved"] = moved
            data["message"] = (
                f"Moved {len(moved)} item(s) to '{params.done_status}'"
                + (f"; {len(errors)} error(s)." if errors else ".")
            )

        return ToolSuccess(data=data).model_dump()

    except Exception as exc:
        logger.error("Error in sync_closed_items_to_done: %s", exc)
        return handle_tool_error(exc, context="Sync closed items to Done failed")
