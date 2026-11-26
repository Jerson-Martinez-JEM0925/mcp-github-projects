"""GitHub CLI client for executing `gh` commands asynchronously.

Provides a thin async wrapper around the `gh` CLI tool using
asyncio.create_subprocess_exec. Handles timeouts, non-zero exit codes,
and JSON output parsing.
"""

import asyncio
import json
import os
from dataclasses import dataclass
from typing import Any

from clients.rest_client import GitHubRESTClient, RESTError
from core.config import get_settings
from core.hardening import bounded_text, redact_sensitive
from models.context import GitHubContext


# ── Custom Exceptions ────────────────────────────────────────────────────────


class CLIError(Exception):
    """Raised when `gh` exits with a non-zero return code."""

    def __init__(self, message: str, return_code: int, stderr: str) -> None:
        self.return_code = return_code
        self.stderr = bounded_text(stderr)
        # The public run() wrapper always overwrites this based on command
        # idempotency. Keeping the legacy default True preserves manually
        # constructed CLI errors and their established error-envelope contract.
        self.safe_to_retry = True
        super().__init__(bounded_text(message))


class CLITimeoutError(Exception):
    """Raised when a `gh` command exceeds the configured timeout."""

    def __init__(self, message: str, timeout_seconds: int, command: str = "") -> None:
        self.timeout_seconds = timeout_seconds
        self.command = command
        self.attempts = 1
        # The public run() wrapper downgrades writes to False. The default
        # preserves the established envelope for directly constructed timeouts.
        self.safe_to_retry = True
        super().__init__(message)


_TRANSIENT_PATTERNS = (
    "rate limit",
    "secondary rate",
    "http 429",
    "http 500",
    "http 502",
    "http 503",
    "http 504",
    "connection reset",
    "connection refused",
    "temporarily unavailable",
)


def _is_transient_cli_error(exc: CLIError) -> bool:
    """Return True when a non-zero `gh` exit looks like a transient upstream fault."""
    text = f"{exc} {exc.stderr}".lower()
    return any(pattern in text for pattern in _TRANSIENT_PATTERNS)


_READ_VERBS = frozenset({"list", "view", "status", "search", "diff", "checks", "browse", "watch"})


def _is_read_only_command(args: list[str]) -> bool:
    """Return True when the `gh` invocation cannot mutate GitHub state.

    Automatic retries are restricted to these commands: a write that times
    out may already have been applied upstream, so replaying it risks
    duplicates (duplicate issues, comments, merges).
    """
    if not args:
        return False
    if args[0] == "api":
        method: str | None = None
        has_fields = False
        for i, arg in enumerate(args):
            if arg in ("-X", "--method") and i + 1 < len(args):
                method = args[i + 1].upper()
            elif arg.startswith("--method="):
                method = arg.split("=", 1)[1].upper()
            elif arg in ("-f", "-F", "--field", "--raw-field", "--input") or arg.startswith(("-f", "-F")):
                has_fields = True
        # gh api defaults to GET, but switches to POST when field/input
        # parameters are present unless -X/--method was given explicitly.
        effective = method or ("POST" if has_fields else "GET")
        return effective == "GET"
    if args[0] in ("search", "auth"):
        return True
    verb = args[1] if len(args) > 1 else ""
    return verb in _READ_VERBS


# ── Data Classes ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Result of a successful `gh` CLI command execution."""

    stdout: str
    stderr: str
    return_code: int


def _safe_command(args: list[str]) -> str:
    """Render a command without exposing tokens or unbounded user content."""
    secrets = [os.environ.get("GITHUB_TOKEN", ""), os.environ.get("GH_TOKEN", "")]
    rendered = [bounded_text(redact_sensitive(arg, secrets), 200) for arg in args]
    return "gh " + " ".join(rendered)


# ── REST fast-path helpers ───────────────────────────────────────────────────


def _option_value(args: list[str], name: str) -> str | None:
    """Return the value of a ``--name value`` or ``--name=value`` option."""
    for index, arg in enumerate(args):
        if arg == name and index + 1 < len(args):
            return args[index + 1]
        if arg.startswith(f"{name}="):
            return arg.split("=", 1)[1]
    return None




def _valid_repository(value: str | None) -> bool:
    """Return true for a simple owner/repository CLI target."""
    if not value or value.count("/") != 1:
        return False
    owner, repo = value.split("/", 1)
    return bool(owner and repo) and all(
        part not in ("", ".", "..") and not part.startswith(("/", "\\"))
        for part in (owner, repo)
    )


def _rest_issue_json(issue: dict[str, Any], fields: list[str]) -> dict[str, Any]:
    """Project a REST issue into the camelCase shape used by ``gh issue list``."""
    labels = [
        {
            "name": label.get("name"),
            "color": label.get("color"),
            "description": label.get("description"),
        }
        for label in issue.get("labels", [])
        if isinstance(label, dict)
    ]
    assignees = [
        {"login": user.get("login")}
        for user in issue.get("assignees", [])
        if isinstance(user, dict)
    ]
    milestone = issue.get("milestone") if isinstance(issue.get("milestone"), dict) else None
    available: dict[str, Any] = {
        "number": issue.get("number"),
        "title": issue.get("title"),
        "body": issue.get("body"),
        "state": str(issue.get("state") or "").upper(),
        "labels": labels,
        "assignees": assignees,
        "milestone": {"title": milestone.get("title")} if milestone else None,
        "createdAt": issue.get("created_at"),
        "updatedAt": issue.get("updated_at"),
        "closedAt": issue.get("closed_at"),
        "url": issue.get("html_url"),
        "comments": issue.get("comments"),
    }
    if not fields:
        return available
    return {field: available.get(field) for field in fields}


def _typed_gh_value(value: str) -> Any:
    """Convert the small typed-value subset used by this repository's ``-F`` fields."""
    lowered = value.lower()
    if lowered == "null":
        return None
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def _assign_gh_field(body: dict[str, Any], assignment: str, *, typed: bool) -> bool:
    """Assign one ``key=value`` or ``object[key]=value`` field into a JSON body."""
    if "=" not in assignment:
        return False
    key, raw_value = assignment.split("=", 1)
    if not key:
        return False
    value: Any = _typed_gh_value(raw_value) if typed else raw_value
    if "[" in key and key.endswith("]"):
        outer, inner = key[:-1].split("[", 1)
        if not outer or not inner:
            return False
        nested = body.setdefault(outer, {})
        if not isinstance(nested, dict):
            return False
        nested[inner] = value
        return True
    if "[" in key or "]" in key:
        return False
    body[key] = value
    return True


def _normalize_api_endpoint(endpoint: str) -> str | None:
    """Normalize a GitHub API endpoint for the direct REST transport."""
    endpoint = endpoint.strip()
    if endpoint.startswith("https://api.github.com/"):
        endpoint = endpoint.removeprefix("https://api.github.com")
    if endpoint.startswith("http://") or endpoint.startswith("https://"):
        return None
    if not endpoint.startswith("/"):
        endpoint = "/" + endpoint
    if endpoint.startswith("//") or "/../" in endpoint:
        return None
    return endpoint


# ── Client ───────────────────────────────────────────────────────────────────


class GHCLIClient:
    """Async client for executing GitHub CLI commands."""

    def __init__(self, context: GitHubContext | None = None) -> None:
        self._context = context

    async def run(self, args: list[str]) -> CommandResult:
        """Execute a `gh` command, retrying transient failures with backoff.

        Retries `CLITimeoutError` and transient `CLIError`s (rate limits,
        5xx, connection faults) up to ``settings.retry_attempts`` extra
        times, sleeping ``settings.retry_delay_seconds`` between tries.

            args: List of arguments to pass to `gh`
                  (e.g., ["issue", "create", "--title", "Bug fix"]).

        Returns:
            CommandResult with stdout, stderr, and return_code (always 0).

        Raises:
            CLIError: If the command exits with a non-zero return code.
            CLITimeoutError: If the command exceeds the configured timeout.
        """
        settings = get_settings()
        attempts = 1 + max(0, settings.retry_attempts)
        read_only = _is_read_only_command(args)
        last_exc: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                return await self._run_once(args)
            except (CLITimeoutError, CLIError) as exc:
                last_exc = exc
                if isinstance(exc, CLITimeoutError):
                    exc.attempts = attempt
                transient = isinstance(exc, CLITimeoutError) or _is_transient_cli_error(exc)
                retryable = transient and read_only
                # safe_to_retry feeds the error envelope: on a write it must
                # be False — a timed-out mutation may already be applied.
                exc.safe_to_retry = retryable
                if not retryable or attempt >= attempts:
                    raise
                await asyncio.sleep(settings.retry_delay_seconds)
        assert last_exc is not None  # unreachable: loop returns or raises
        raise last_exc

    async def _try_rest_fast_path(self, args: list[str]) -> CommandResult | None:
        """Execute selected high-frequency commands through pooled REST.

        Returns ``None`` when the exact CLI shape is unsupported; those calls
        continue through the ``gh`` subprocess unchanged. REST errors are
        deliberately surfaced as CLI-compatible errors instead of falling back
        silently, so callers keep their normal typed-error behavior.
        """
        if self._context is None or len(args) < 2:
            return None
        rest = GitHubRESTClient(context=self._context)
        try:
            if args[0] == "api":
                return await self._rest_api(rest, args)
            if args[:2] == ["issue", "list"]:
                return await self._rest_issue_list(rest, args)
            if args[:2] == ["label", "list"]:
                return await self._rest_label_list(rest, args)
            if args[:2] == ["issue", "comment"]:
                return await self._rest_issue_comment(rest, args)
            if args[:2] == ["issue", "close"]:
                return await self._rest_issue_close(rest, args)
        except RESTError as exc:
            if "timed out" in str(exc).lower():
                raise CLITimeoutError(
                    str(exc),
                    timeout_seconds=get_settings().timeout_seconds,
                    command=_safe_command(args),
                ) from exc
            raise CLIError(
                f"gh command failed (REST fast path): {exc}",
                return_code=exc.status_code or 1,
                stderr=str(exc),
            ) from exc
        return None

    async def _rest_api(self, rest: GitHubRESTClient, args: list[str]) -> CommandResult | None:
        """Implement the common non-interactive ``gh api`` subset over pooled REST.

        Supported shape: one endpoint, an optional explicit method, and simple
        ``-f``/``-F`` fields. Output-transforming or transport-specific options
        (``--jq``, custom headers, pagination expansion, input files, host
        overrides) intentionally fall back to the real CLI because changing
        them would alter the public contract.
        """
        unsupported_prefixes = (
            "--jq",
            "--template",
            "--paginate",
            "--input",
            "--hostname",
            "--include",
            "--verbose",
            "--cache",
            "-H",
            "--header",
        )
        method: str | None = None
        endpoint: str | None = None
        body: dict[str, Any] = {}
        has_fields = False
        index = 1
        while index < len(args):
            arg = args[index]
            key = arg.split("=", 1)[0]
            if key in unsupported_prefixes:
                return None
            if arg in ("-X", "--method"):
                if index + 1 >= len(args):
                    return None
                method = args[index + 1].upper()
                index += 2
                continue
            if arg.startswith("--method="):
                method = arg.split("=", 1)[1].upper()
                index += 1
                continue
            if arg in ("-f", "-F", "--field", "--raw-field"):
                if index + 1 >= len(args):
                    return None
                assignment = args[index + 1]
                typed = arg in ("-F", "--field")
                if not _assign_gh_field(body, assignment, typed=typed):
                    return None
                has_fields = True
                index += 2
                continue
            if arg.startswith(("-f", "-F")) and len(arg) > 2:
                assignment = arg[2:]
                if not _assign_gh_field(body, assignment, typed=arg.startswith("-F")):
                    return None
                has_fields = True
                index += 1
                continue
            if arg.startswith("--field=") or arg.startswith("--raw-field="):
                assignment = arg.split("=", 1)[1]
                typed = arg.startswith("--field=")
                if not _assign_gh_field(body, assignment, typed=typed):
                    return None
                has_fields = True
                index += 1
                continue
            if arg.startswith("-"):
                return None
            if endpoint is not None:
                return None
            endpoint = arg
            index += 1

        if endpoint is None:
            return None
        normalized = _normalize_api_endpoint(endpoint)
        # ``gh api graphql`` has special variables semantics (-f variables are
        # not the top-level JSON body). Keep it on the real CLI rather than
        # silently changing the GraphQL contract.
        if normalized is None or normalized == "/graphql":
            return None
        effective_method = method or ("POST" if has_fields else "GET")
        if effective_method not in {"GET", "POST", "PATCH", "PUT", "DELETE"}:
            return None
        payload = await rest.request(
            effective_method,
            normalized,
            json_body=body if has_fields else None,
        )
        if payload is None:
            stdout = ""
        elif isinstance(payload, str):
            stdout = payload
        else:
            stdout = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        return CommandResult(stdout=stdout, stderr="", return_code=0)

    async def _rest_issue_list(self, rest: GitHubRESTClient, args: list[str]) -> CommandResult | None:
        """Implement the common JSON-shaped ``gh issue list`` invocation."""
        options: dict[str, str] = {}
        index = 2
        supported = {"--repo", "--state", "--limit", "--json", "--assignee"}
        while index < len(args):
            arg = args[index]
            key = arg.split("=", 1)[0]
            if key not in supported:
                return None
            if "=" in arg:
                options[key] = arg.split("=", 1)[1]
                index += 1
            elif index + 1 < len(args):
                options[key] = args[index + 1]
                index += 2
            else:
                return None
        repo = options.get("--repo")
        if not _valid_repository(repo):
            return None
        try:
            limit = max(1, min(1000, int(options.get("--limit", "30"))))
        except ValueError:
            return None
        state = (options.get("--state") or "open").lower()
        params: dict[str, Any] = {"per_page": min(100, limit), "page": 1}
        if state in {"open", "closed"}:
            params["state"] = state
        elif state != "all":
            return None
        if options.get("--assignee"):
            params["assignee"] = options["--assignee"]

        issues: list[dict[str, Any]] = []
        while len(issues) < limit:
            payload = await rest.request("GET", f"/repos/{repo}/issues", params=params)
            if not isinstance(payload, list) or not payload:
                break
            page_items = [
                item for item in payload
                if isinstance(item, dict) and "pull_request" not in item
            ]
            issues.extend(page_items)
            if len(payload) < int(params["per_page"]):
                break
            params["page"] += 1
        fields = [
            field.strip()
            for field in (options.get("--json") or "").split(",")
            if field.strip()
        ]
        shaped = [_rest_issue_json(issue, fields) for issue in issues[:limit]]
        return CommandResult(stdout=json.dumps(shaped, separators=(",", ":")), stderr="", return_code=0)

    async def _rest_label_list(self, rest: GitHubRESTClient, args: list[str]) -> CommandResult | None:
        """Implement the common JSON-shaped ``gh label list`` invocation."""
        options: dict[str, str] = {}
        index = 2
        supported = {"--repo", "--limit", "--json"}
        while index < len(args):
            arg = args[index]
            key = arg.split("=", 1)[0]
            if key not in supported:
                return None
            if "=" in arg:
                options[key] = arg.split("=", 1)[1]
                index += 1
            elif index + 1 < len(args):
                options[key] = args[index + 1]
                index += 2
            else:
                return None
        repo = options.get("--repo")
        if not _valid_repository(repo):
            return None
        try:
            limit = max(1, min(1000, int(options.get("--limit") or "30")))
        except ValueError:
            return None
        fields = [
            field.strip()
            for field in (options.get("--json") or "name,color,description").split(",")
            if field.strip()
        ]
        labels: list[dict[str, Any]] = []
        page = 1
        while len(labels) < limit:
            payload = await rest.request(
                "GET",
                f"/repos/{repo}/labels",
                params={"per_page": min(100, limit - len(labels)), "page": page},
            )
            if not isinstance(payload, list) or not payload:
                break
            labels.extend(item for item in payload if isinstance(item, dict))
            if len(payload) < 100:
                break
            page += 1
        shaped = [
            {field: label.get(field) for field in fields}
            for label in labels[:limit]
        ]
        return CommandResult(stdout=json.dumps(shaped, separators=(",", ":")), stderr="", return_code=0)

    async def _rest_issue_comment(self, rest: GitHubRESTClient, args: list[str]) -> CommandResult | None:
        """Implement ``gh issue comment N --repo R --body text``."""
        if len(args) < 3 or not args[2].isdigit():
            return None
        repo: str | None = None
        body: str | None = None
        index = 3
        while index < len(args):
            arg = args[index]
            if arg == "--repo" and index + 1 < len(args):
                repo = args[index + 1]
                index += 2
            elif arg.startswith("--repo="):
                repo = arg.split("=", 1)[1]
                index += 1
            elif arg == "--body" and index + 1 < len(args):
                body = args[index + 1]
                index += 2
            elif arg.startswith("--body="):
                body = arg.split("=", 1)[1]
                index += 1
            else:
                return None
        if not _valid_repository(repo) or body is None:
            return None
        payload = await rest.request(
            "POST",
            f"/repos/{repo}/issues/{args[2]}/comments",
            json_body={"body": body},
        )
        url = payload.get("html_url", "") if isinstance(payload, dict) else ""
        return CommandResult(stdout=f"{url}\n" if url else "", stderr="", return_code=0)

    async def _rest_issue_close(self, rest: GitHubRESTClient, args: list[str]) -> CommandResult | None:
        """Implement ``gh issue close N --repo R [--reason reason]``."""
        if len(args) < 3 or not args[2].isdigit():
            return None
        repo: str | None = None
        reason = "completed"
        index = 3
        while index < len(args):
            arg = args[index]
            if arg == "--repo" and index + 1 < len(args):
                repo = args[index + 1]
                index += 2
            elif arg.startswith("--repo="):
                repo = arg.split("=", 1)[1]
                index += 1
            elif arg == "--reason" and index + 1 < len(args):
                reason = args[index + 1]
                index += 2
            elif arg.startswith("--reason="):
                reason = arg.split("=", 1)[1]
                index += 1
            else:
                return None
        if not _valid_repository(repo) or reason not in {"completed", "not_planned"}:
            return None
        await rest.request(
            "PATCH",
            f"/repos/{repo}/issues/{args[2]}",
            json_body={"state": "closed", "state_reason": reason},
        )
        return CommandResult(stdout="", stderr="", return_code=0)

    async def _run_once(self, args: list[str]) -> CommandResult:
        """Execute a `gh` command once and return its result."""
        fast_result = await self._try_rest_fast_path(args)
        if fast_result is not None:
            return fast_result
        settings = get_settings()
        timeout = settings.timeout_seconds
        environment = os.environ.copy()
        if self._context is not None:
            environment["GH_TOKEN"] = await self._context.resolve_token()

        process = await asyncio.create_subprocess_exec(
            "gh",
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=environment,
        )

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise CLITimeoutError(
                f"gh command timed out after {timeout} seconds: {_safe_command(args)}",
                timeout_seconds=timeout,
                command=_safe_command(args),
            )

        stdout = stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else ""
        stderr = stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else ""
        return_code = process.returncode or 0
        if len(stdout) + len(stderr) > settings.max_cli_output_chars:
            raise CLIError(
                "gh command output exceeded the configured safety limit",
                return_code=return_code,
                stderr="Output truncated by MCP safety limit",
            )

        if return_code != 0:
            raise CLIError(
                f"gh command failed (exit {return_code}): {stderr.strip()}",
                return_code=return_code,
                stderr=stderr,
            )

        return CommandResult(
            stdout=stdout,
            stderr=stderr,
            return_code=return_code,
        )

    async def api_graphql(self, query: str, variables: dict[str, Any]) -> dict:
        """Execute a GraphQL query via `gh api graphql`.

        Constructs the `gh api graphql` command with the provided query
        and variables, parses the JSON output, and returns the result.

        Args:
            query: The GraphQL query string.
            variables: A dict of variable names to string values.
                       Each entry is passed as a `-f varName=value` flag.

        Returns:
            Parsed JSON response as a dict.

        Raises:
            CLIError: If the command exits with a non-zero return code.
            CLITimeoutError: If the command exceeds the configured timeout.
            ValueError: If the command output is not valid JSON.
        """
        args: list[str] = ["api", "graphql", "-f", f"query={query}"]

        for var_name, value in variables.items():
            if isinstance(value, str):
                args.extend(["-f", f"{var_name}={value}"])
            else:
                encoded = json.dumps(value, separators=(",", ":"))
                args.extend(["-F", f"{var_name}={encoded}"])

        result = await self.run(args)

        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Failed to parse JSON output from gh api graphql: {exc.msg}"
            ) from exc
        if not isinstance(value, dict):
            raise ValueError("Expected an object from gh api graphql")
        return value
