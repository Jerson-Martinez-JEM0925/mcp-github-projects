"""Authentication module for the GitHub Project Management MCP Server.

Provides token resolution from environment variables or GitHub CLI,
and scope validation against the GitHub API.

Security: The token value is NEVER logged, printed, or included in any output.
"""

from __future__ import annotations

import asyncio
import os
import sys
from contextlib import contextmanager
from contextvars import ContextVar, Token
from collections.abc import Iterator

import httpx

from core.config import get_settings
from core.hardening import request_secret_scope

REQUIRED_SCOPES: frozenset[str] = frozenset({"repo", "project", "read:org"})

# GitHub scope hierarchy: parent scopes implicitly grant child scopes.
# See: https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/scopes-for-oauth-apps
_SCOPE_HIERARCHY: dict[str, frozenset[str]] = {
    "admin:org": frozenset({"write:org", "read:org"}),
    "write:org": frozenset({"read:org"}),
    "admin:public_key": frozenset({"write:public_key", "read:public_key"}),
    "write:public_key": frozenset({"read:public_key"}),
    "admin:repo_hook": frozenset({"write:repo_hook", "read:repo_hook"}),
    "write:repo_hook": frozenset({"read:repo_hook"}),
    "admin:org_hook": frozenset({"read:org_hook"}),
    "admin:gpg_key": frozenset({"write:gpg_key", "read:gpg_key"}),
    "write:gpg_key": frozenset({"read:gpg_key"}),
    "admin:ssh_signing_key": frozenset({"write:ssh_signing_key", "read:ssh_signing_key"}),
    "write:ssh_signing_key": frozenset({"read:ssh_signing_key"}),
    "user": frozenset({"read:user", "user:email", "user:follow"}),
    "write:discussion": frozenset({"read:discussion"}),
    "write:packages": frozenset({"read:packages"}),
}

_GITHUB_API_URL = "https://api.github.com"
_SCOPE_VALIDATION_TIMEOUT = 10.0
_REQUEST_TOKEN: ContextVar[str | None] = ContextVar("mcp_request_token", default=None)


@contextmanager
def request_token_scope(token: str) -> Iterator[None]:
    """Bind one HTTP bearer token and its redaction secret to this context."""
    token_handle: Token[str | None] = _REQUEST_TOKEN.set(token)
    try:
        with request_secret_scope(token):
            yield
    finally:
        _REQUEST_TOKEN.reset(token_handle)


def current_request_token() -> str | None:
    """Return the bearer token for the current request, if one is bound."""
    return _REQUEST_TOKEN.get()


def _expand_scopes(granted: frozenset[str]) -> frozenset[str]:
    """Expand granted scopes with their implied child scopes.

    For example, if 'admin:org' is granted, this adds 'write:org' and
    'read:org' to the effective set.

    Args:
        granted: The literal scopes from the X-OAuth-Scopes header.

    Returns:
        The expanded set including all implied child scopes.
    """
    expanded = set(granted)
    for scope in granted:
        children = _SCOPE_HIERARCHY.get(scope)
        if children:
            expanded.update(children)
    return frozenset(expanded)


async def resolve_token() -> str:
    """Resolve the credential without falling back in request-auth mode.

    In stateless HTTP request mode, the bearer token must already be bound by
    the HTTP middleware. Environment variables and the GitHub CLI are never
    consulted in that mode. Stdio and explicitly guarded shared-token HTTP
    mode retain the legacy environment/CLI resolution order.

    Returns:
        The resolved token string.

    Raises:
        ValueError: If request mode has no bound request credential.
        SystemExit: If env-mode has no valid token (exits with code 1).
    """
    request_token = current_request_token()
    settings = get_settings()
    if settings.auth_mode == "request":
        if request_token:
            return request_token
        raise ValueError("No request credential is bound to this operation")

    # 1. Check GITHUB_TOKEN
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        return token

    # 2. Check GH_TOKEN
    token = os.environ.get("GH_TOKEN", "").strip()
    if token:
        return token

    # 3. Attempt gh auth token
    token = await _resolve_token_from_gh_cli()
    if token:
        return token

    # No token found — exit with descriptive error
    print(
        "Error: No GitHub token found.\n"
        "Provide a token via GITHUB_TOKEN or GH_TOKEN environment variable, "
        "or authenticate with `gh auth login`.\n"
        "Required scopes: repo, project, read:org",
        file=sys.stderr,
    )
    sys.exit(1)


async def validate_scopes(token: str) -> None:
    """Validate that the token has required scopes (repo, project, read:org).

    Queries GET https://api.github.com and checks the X-OAuth-Scopes
    response header for required scopes. Must complete within 10 seconds.

    Args:
        token: The GitHub token to validate.

    Raises:
        SystemExit: If scopes are insufficient or validation fails
                   (exits with code 1, message to stderr listing missing
                   and required scopes). Never includes the token value.
    """
    try:
        async with httpx.AsyncClient(timeout=_SCOPE_VALIDATION_TIMEOUT) as client:
            response = await client.get(
                _GITHUB_API_URL,
                headers={"Authorization": f"Bearer {token}"},
            )
    except httpx.TimeoutException:
        print(
            "Error: Scope validation timed out after 10 seconds.\n"
            "Could not reach the GitHub API to verify token scopes.",
            file=sys.stderr,
        )
        sys.exit(1)
    except httpx.HTTPError:
        print(
            "Error: Failed to connect to the GitHub API for scope validation.\n"
            "Check your network connection and try again.",
            file=sys.stderr,
        )
        sys.exit(1)

    if response.status_code == 401:
        print(
            "Error: The provided token is invalid or expired.\n"
            "Generate a new token with the required scopes: "
            "repo, project, read:org",
            file=sys.stderr,
        )
        sys.exit(1)

    # Parse X-OAuth-Scopes header and expand with implied children.
    scopes_header = response.headers.get("X-OAuth-Scopes", "")
    granted_scopes = frozenset(
        scope.strip() for scope in scopes_header.split(",") if scope.strip()
    )
    effective_scopes = _expand_scopes(granted_scopes)

    missing_scopes = REQUIRED_SCOPES - effective_scopes
    if missing_scopes:
        print(
            f"Error: Insufficient token scopes.\n"
            f"Missing scopes: {', '.join(sorted(missing_scopes))}\n"
            f"Required scopes: {', '.join(sorted(REQUIRED_SCOPES))}\n"
            "Generate a new token with all required scopes.",
            file=sys.stderr,
        )
        sys.exit(1)


async def _resolve_token_from_gh_cli() -> str | None:
    """Attempt to resolve a token from `gh auth token`.

    Returns:
        The token string if successful, None otherwise.
    """
    try:
        process = await asyncio.create_subprocess_exec(
            "gh",
            "auth",
            "token",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, _ = await asyncio.wait_for(
            process.communicate(),
            timeout=_SCOPE_VALIDATION_TIMEOUT,
        )
    except (FileNotFoundError, asyncio.TimeoutError, OSError):
        return None

    if process.returncode != 0:
        return None

    token = stdout_bytes.decode("utf-8", errors="replace").strip() if stdout_bytes else ""
    return token if token else None
