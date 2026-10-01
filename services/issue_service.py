"""Issue service for creating, closing, and updating GitHub issues.

Delegates all operations to the `gh` CLI via the GHCLIClient. Handles
DraftIssue edge cases and assignee/label replacement semantics.
"""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass, field

from clients.gh_cli_client import CLIError
from core.protocols import GHCLIRunner
from core.hardening import normalize_unique, parse_json_object
from models.context import GitHubContext


# ── Data Classes ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class CreatedIssue:
    """Result of a successful issue creation.

    ``warnings`` lists follow-up steps that failed after the issue already
    existed (for example, assignees the token may not set). The issue itself
    was created, so callers must not retry the creation.
    """

    number: int
    url: str
    warnings: tuple[str, ...] = field(default_factory=tuple)


# ── Exceptions ───────────────────────────────────────────────────────────────


class DraftIssueError(Exception):
    """Raised when an operation is attempted on a DraftIssue that requires a real Issue."""

    def __init__(self, message: str) -> None:
        super().__init__(message)


# ── Service ──────────────────────────────────────────────────────────────────


class IssueService:
    """Service for managing GitHub issues via the `gh` CLI.

    All operations target the repository configured in settings
    (org_name/repo_name).
    """

    def __init__(
        self,
        gh_client: GHCLIRunner,
        context: GitHubContext | None = None,
    ) -> None:
        self._gh = gh_client
        self._context = context or GitHubContext.from_settings(
            token_provider=lambda: ""
        )

    @property
    def _repo_flag(self) -> list[str]:
        """Return the --repo flag with the configured org/repo."""
        target = self._context.target
        repository = target.repository_full_name
        if repository is None:
            raise ValueError("Issue operations require a target repository")
        return ["--repo", repository]

    async def create(
        self,
        title: str,
        body: str = "",
        labels: list[str] | None = None,
        assignees: list[str] | None = None,
        milestone: str | None = None,
    ) -> CreatedIssue:
        """Create a new issue in the configured repository.

        Args:
            title: Issue title (1–256 characters).
            body: Issue body content (supports long markdown via temp file).
            labels: Optional list of label names to apply.
            assignees: Optional list of GitHub usernames to assign.
            milestone: Optional milestone title to associate.

        Returns:
            CreatedIssue with the issue number and URL.

        Raises:
            CLIError: If the `gh issue create` command fails.
            CLITimeoutError: If the command exceeds the configured timeout.
        """
        args: list[str] = [
            "issue",
            "create",
            *self._repo_flag,
            "--title",
            title,
        ]

        # Use a securely-created file for long UTF-8 bodies.
        temp_file_path: str | None = None
        if body:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                suffix=".md",
                prefix="gh_issue_",
                delete=False,
            ) as body_file:
                body_file.write(body)
                temp_file_path = body_file.name
            args.extend(["--body-file", temp_file_path])
        else:
            args.extend(["--body", ""])

        for label in normalize_unique(labels):
            args.extend(["--label", label])

        # Assignees are applied after creation: `gh issue create --assignee`
        # creates the issue and only then sets assignees, so a permission
        # error there exits non-zero for an issue that already exists and a
        # retry would create a duplicate.
        requested_assignees = normalize_unique(assignees)

        if milestone:
            args.extend(["--milestone", milestone])

        try:
            result = await self._gh.run(args)
        finally:
            if temp_file_path:
                try:
                    os.unlink(temp_file_path)
                except FileNotFoundError:
                    pass

        # gh issue create outputs the issue URL as the last non-empty line
        url = result.stdout.strip().splitlines()[-1].strip()
        number = self._extract_issue_number(url)

        warnings: list[str] = []
        if requested_assignees:
            try:
                await self._gh.run([
                    "issue", "edit", str(number), *self._repo_flag,
                    "--add-assignee", ",".join(requested_assignees),
                ])
            except CLIError as exc:
                reason = (exc.stderr or str(exc)).strip()
                warnings.append(
                    f"Issue #{number} was created, but assignees "
                    f"{', '.join(requested_assignees)} could not be set: {reason}"
                )

        return CreatedIssue(number=number, url=url, warnings=tuple(warnings))

    async def close(self, issue_number: int) -> None:
        """Close an issue.

        Args:
            issue_number: The issue number to close.

        Raises:
            DraftIssueError: If the issue is a DraftIssue (cannot be closed).
            CLIError: If the `gh issue close` command fails for other reasons.
            CLITimeoutError: If the command exceeds the configured timeout.
        """
        args: list[str] = [
            "issue",
            "close",
            str(issue_number),
            *self._repo_flag,
        ]

        try:
            await self._gh.run(args)
        except CLIError as exc:
            stderr_lower = exc.stderr.lower()
            if "draft" in stderr_lower or "draftissue" in stderr_lower:
                raise DraftIssueError(
                    f"Issue #{issue_number} is a DraftIssue and cannot be closed. "
                    "Convert it to a full issue first."
                ) from exc
            raise

    async def update(
        self,
        issue_number: int,
        body: str | None = None,
        assignees: list[str] | None = None,
        labels: list[str] | None = None,
    ) -> None:
        """Update an existing issue.

        For assignees and labels, replacement semantics are used: the current
        list is fully replaced by the provided list. An empty list removes all
        current values.

        Args:
            issue_number: The issue number to update.
            body: New body content (replaces existing body).
            assignees: New assignee list (replaces existing assignees).
            labels: New label list (replaces existing labels).

        Raises:
            CLIError: If any `gh` command fails.
            CLITimeoutError: If any command exceeds the configured timeout.
        """
        # Handle body update with a simple gh issue edit --body
        if body is not None:
            await self._edit_body(issue_number, body)

        # Handle assignee replacement
        if assignees is not None:
            await self._replace_assignees(issue_number, assignees)

        # Handle label replacement
        if labels is not None:
            await self._replace_labels(issue_number, labels)

    async def _edit_body(self, issue_number: int, body: str) -> None:
        """Update the issue body."""
        args: list[str] = [
            "issue",
            "edit",
            str(issue_number),
            *self._repo_flag,
            "--body",
            body,
        ]
        await self._gh.run(args)

    async def _replace_assignees(
        self, issue_number: int, new_assignees: list[str]
    ) -> None:
        """Replace the current assignee list with the provided list.

        Fetches current assignees, computes the diff, then adds/removes
        as needed via gh issue edit.
        """
        current_assignees = await self._get_current_assignees(issue_number)
        current_set = set(current_assignees)
        new_set = set(new_assignees)

        to_add = new_set - current_set
        to_remove = current_set - new_set

        if not to_add and not to_remove:
            return

        args: list[str] = [
            "issue",
            "edit",
            str(issue_number),
            *self._repo_flag,
        ]

        if to_add:
            args.extend(["--add-assignee", ",".join(sorted(to_add))])

        if to_remove:
            args.extend(["--remove-assignee", ",".join(sorted(to_remove))])

        await self._gh.run(args)

    async def _replace_labels(
        self, issue_number: int, new_labels: list[str]
    ) -> None:
        """Replace the current label list with the provided list.

        Fetches current labels, computes the diff, then adds/removes
        as needed via gh issue edit.
        """
        current_labels = await self._get_current_labels(issue_number)
        current_set = set(current_labels)
        new_set = set(new_labels)

        to_add = new_set - current_set
        to_remove = current_set - new_set

        if not to_add and not to_remove:
            return

        args: list[str] = [
            "issue",
            "edit",
            str(issue_number),
            *self._repo_flag,
        ]

        if to_add:
            args.extend(["--add-label", ",".join(sorted(to_add))])

        if to_remove:
            args.extend(["--remove-label", ",".join(sorted(to_remove))])

        await self._gh.run(args)

    async def _get_current_assignees(self, issue_number: int) -> list[str]:
        """Fetch the current assignees for an issue."""
        args: list[str] = [
            "issue",
            "view",
            str(issue_number),
            *self._repo_flag,
            "--json",
            "assignees",
        ]
        result = await self._gh.run(args)
        data = parse_json_object(result.stdout, context="gh issue view")
        return [a["login"] for a in data.get("assignees", [])]

    async def _get_current_labels(self, issue_number: int) -> list[str]:
        """Fetch the current labels for an issue."""
        args: list[str] = [
            "issue",
            "view",
            str(issue_number),
            *self._repo_flag,
            "--json",
            "labels",
        ]
        result = await self._gh.run(args)
        data = parse_json_object(result.stdout, context="gh issue view")
        return [label["name"] for label in data.get("labels", [])]

    @staticmethod
    def _extract_issue_number(url: str) -> int:
        """Extract the issue number from a GitHub issue URL.

        Args:
            url: GitHub issue URL (e.g., https://github.com/org/repo/issues/42).

        Returns:
            The issue number as an integer.

        Raises:
            ValueError: If the URL does not contain a valid issue number.
        """
        match = re.search(r"/issues/(\d+)", url)
        if not match:
            raise ValueError(
                f"Could not extract issue number from URL: {url}"
            )
        return int(match.group(1))
