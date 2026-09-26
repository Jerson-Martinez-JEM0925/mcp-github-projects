"""Centralized error handling for the GitHub Project Management MCP Server.

Provides error classification logic and a unified response builder so that
all tool handlers return consistent error responses. Maps exceptions from
the client/service layers to the five defined error types and constructs
ToolError envelopes including the GitHub API request ID when available.

Error types:
    - authentication: Token invalid, expired, or insufficient scopes.
    - validation: Input fails business rules or field value checks.
    - not_found: Requested resource does not exist.
    - rate_limit: GitHub API rate limit exceeded.
    - internal: Unexpected failures, timeouts, network errors.
"""

from __future__ import annotations

import logging
from typing import Any

from core.exceptions import (
    AccessDeniedError,
    AuthenticationError,
    GitHubProjectError,
    GraphQLError,
    RateLimitError,
    ScopeLockError,
    TimeoutError,
    ValidationError,
)
from core.hardening import bounded_text
from models.responses import ToolError

logger = logging.getLogger(__name__)

# Valid error type identifiers per the design spec.
ERROR_TYPE_AUTHENTICATION = "authentication"
ERROR_TYPE_VALIDATION = "validation"
ERROR_TYPE_NOT_FOUND = "not_found"
ERROR_TYPE_RATE_LIMIT = "rate_limit"
ERROR_TYPE_INTERNAL = "internal"

VALID_ERROR_TYPES: frozenset[str] = frozenset({
    ERROR_TYPE_AUTHENTICATION,
    ERROR_TYPE_VALIDATION,
    ERROR_TYPE_NOT_FOUND,
    ERROR_TYPE_RATE_LIMIT,
    ERROR_TYPE_INTERNAL,
})

# Default suggestions per error type.
_DEFAULT_SUGGESTIONS: dict[str, str] = {
    ERROR_TYPE_AUTHENTICATION: (
        "Verify that GITHUB_TOKEN or GH_TOKEN is set with "
        "repo, project, and read:org scopes."
    ),
    ERROR_TYPE_VALIDATION: (
        "Check the input values and run discover_ids to see "
        "valid field options."
    ),
    ERROR_TYPE_NOT_FOUND: (
        "Verify the resource ID is correct. Run list_project_items "
        "or discover_ids to find valid identifiers."
    ),
    ERROR_TYPE_RATE_LIMIT: (
        "Wait until the rate limit resets or use a token with higher limits."
    ),
    ERROR_TYPE_INTERNAL: (
        "An unexpected error occurred. Check server logs for details "
        "and verify network connectivity to the GitHub API."
    ),
}


def classify_error(exc: Exception) -> str:
    """Classify an exception into one of the five error types.

    Maps exception types to error categories following the design spec:
        - AuthenticationError → "authentication"
        - ValidationError → "validation"
        - GraphQLError with NOT_FOUND → "not_found"
        - RateLimitError → "rate_limit"
        - All others → "internal"

    Args:
        exc: The exception to classify.

    Returns:
        One of: "authentication", "validation", "not_found",
        "rate_limit", or "internal".
    """
    if isinstance(exc, AuthenticationError):
        return ERROR_TYPE_AUTHENTICATION

    if isinstance(exc, (ValidationError, ScopeLockError, AccessDeniedError)):
        return ERROR_TYPE_VALIDATION

    if isinstance(exc, RateLimitError):
        return ERROR_TYPE_RATE_LIMIT

    if isinstance(exc, GraphQLError):
        # Check if the GraphQL error indicates a NOT_FOUND condition.
        if _is_not_found_graphql_error(exc):
            return ERROR_TYPE_NOT_FOUND
        return ERROR_TYPE_INTERNAL

    if isinstance(exc, TimeoutError):
        return ERROR_TYPE_INTERNAL

    if isinstance(exc, GitHubProjectError):
        return ERROR_TYPE_INTERNAL

    return ERROR_TYPE_INTERNAL


def build_error_response(
    error_type: str,
    message: str,
    suggestion: str | None = None,
    request_id: str | None = None,
) -> dict:
    """Construct a standardized error response dict.

    Builds a ToolError envelope with the given parameters. If no
    suggestion is provided, a default suggestion is used based on
    the error type.

    Args:
        error_type: Error classification (must be one of the five valid types).
        message: Human-readable description of the failure.
        suggestion: Actionable resolution suggestion. If None, a default
                    suggestion for the error type is used.
        request_id: GitHub API X-Request-Id header value, if available.

    Returns:
        A dict conforming to the ToolError model (ok=False, error_type,
        message, suggestion, request_id).
    """
    if error_type not in VALID_ERROR_TYPES:
        error_type = ERROR_TYPE_INTERNAL

    if suggestion is None:
        suggestion = _DEFAULT_SUGGESTIONS.get(error_type, _DEFAULT_SUGGESTIONS[ERROR_TYPE_INTERNAL])

    return ToolError(
        error_type=error_type,
        message=message,
        suggestion=suggestion,
        request_id=request_id,
    ).model_dump()


def handle_tool_error(
    exc: Exception,
    *,
    context: str = "",
    suggestion: str | None = None,
) -> dict:
    """Classify an exception and build a standardized error response.

    Convenience function combining classify_error() and build_error_response().
    Extracts the request_id from the exception if it's a GitHubProjectError,
    builds an appropriate message, and returns a consistent ToolError dict.

    For RateLimitError, the suggestion includes the reset timestamp.
    For other errors, a custom suggestion can be provided or the default
    will be used.

    Args:
        exc: The exception to handle.
        context: Optional context string prepended to generic messages
                 (e.g., "Discovery failed" or "List items failed").
        suggestion: Optional custom suggestion override.

    Returns:
        A dict conforming to the ToolError model.
    """
    error_type = classify_error(exc)
    request_id = _extract_request_id(exc)
    message = _build_message(exc, context=context)

    # For rate limit errors, enhance the suggestion with reset info.
    if error_type == ERROR_TYPE_RATE_LIMIT and isinstance(exc, RateLimitError):
        if suggestion is None:
            suggestion = (
                f"Rate limit resets at {exc.reset_at}. "
                "Wait until the reset time or use a token with higher limits."
            )

    return build_error_response(
        error_type=error_type,
        message=message,
        suggestion=suggestion,
        request_id=request_id,
    )


def _extract_request_id(exc: Exception) -> str | None:
    """Extract the GitHub API request ID from an exception.

    Args:
        exc: The exception to inspect.

    Returns:
        The request_id string if available, None otherwise.
    """
    if isinstance(exc, GitHubProjectError):
        return exc.request_id
    return None


def _build_message(exc: Exception, *, context: str = "") -> str:
    """Build the error message string from an exception.

    Args:
        exc: The exception to describe.
        context: Optional prefix for generic messages.

    Returns:
        A human-readable error message.
    """
    exc_message = bounded_text(exc, 2_000)

    if context:
        return bounded_text(f"{context}: {exc_message}", 2_000)

    return exc_message


def _is_not_found_graphql_error(exc: GraphQLError) -> bool:
    """Check if a GraphQL error indicates a NOT_FOUND condition.

    Inspects both the error type fields in the GraphQL errors list
    and common message patterns.

    Args:
        exc: The GraphQLError to inspect.

    Returns:
        True if the error represents a not-found condition.
    """
    # Check error type fields in the errors list.
    for error in exc.errors:
        error_type = error.get("type", "").upper()
        if error_type == "NOT_FOUND":
            return True

    # Check message content as fallback.
    message_lower = str(exc).lower()
    not_found_indicators = ("not found", "could not resolve", "does not exist")
    return any(indicator in message_lower for indicator in not_found_indicators)
