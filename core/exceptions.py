"""Custom exception classes for the GitHub Project Management MCP Server.

These exceptions represent classified error states from the GitHub API
and are used by the client and service layers to propagate structured
error information without exposing sensitive data (e.g., token values).
"""

from __future__ import annotations


class GitHubProjectError(Exception):
    """Base exception for all GitHub Project MCP errors."""

    def __init__(
        self,
        message: str,
        *,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.request_id = request_id


class AuthenticationError(GitHubProjectError):
    """Raised when the GitHub API returns HTTP 401 or token is invalid.

    Never includes the token value in the message.
    """


class RateLimitError(GitHubProjectError):
    """Raised when the GitHub API returns HTTP 403 with rate-limit headers.

    Attributes:
        reset_at: ISO 8601 timestamp when the rate limit resets.
        remaining: Number of requests remaining (always 0 when this fires).
    """

    def __init__(
        self,
        message: str,
        *,
        reset_at: str | None = None,
        remaining: int = 0,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message, request_id=request_id)
        self.reset_at = reset_at
        self.remaining = remaining


class TimeoutError(GitHubProjectError):
    """Raised when a GitHub API call exceeds the configured timeout."""


class GraphQLError(GitHubProjectError):
    """Raised when the GitHub GraphQL API returns errors in the response body.

    Attributes:
        errors: List of error objects from the GraphQL response.
    """

    def __init__(
        self,
        message: str,
        *,
        errors: list[dict] | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message, request_id=request_id)
        self.errors = errors or []


class ValidationError(GitHubProjectError):
    """Raised when input validation fails.

    Used by the service layer to reject values that don't meet
    business rules (e.g., estimate out of range or wrong granularity).
    """


class ScopeLockError(GitHubProjectError):
    """Raised when a call targets a resource outside the locked scope.

    When GH_PROJECT_SCOPE_LOCK=true, every tool is confined to the configured
    GH_PROJECT_ORG_NAME / GH_PROJECT_REPO_NAME / GH_PROJECT_PROJECT_NUMBER.
    A tool that receives an owner/repo/project override naming anything else
    raises this BEFORE any mutation reaches GitHub. The message names the
    GH_PROJECT_SCOPE_LOCK variable so the operator knows what to change.
    """


class AccessDeniedError(GitHubProjectError):
    """Raised when an operation is not permitted at the current access level.

    Permanent-delete tools are only exposed at MCP_ACCESS_LEVEL=full, and each
    additionally requires confirm=true. This is raised when a delete tool is
    invoked without confirmation.
    """
