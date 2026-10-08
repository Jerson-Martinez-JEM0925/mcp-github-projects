"""Shared safety helpers for the GitHub Project MCP.

The helpers in this module are deliberately dependency-free so the standalone
MCP image and the backend-embedded package can use the same safeguards.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar, Token
from pathlib import Path
from typing import Any

_REQUEST_SECRETS: ContextVar[tuple[str, ...]] = ContextVar("mcp_request_secrets", default=())


@contextmanager
def request_secret_scope(secret: str) -> Iterator[None]:
    """Add one ephemeral request secret to every diagnostic redaction."""
    previous = _REQUEST_SECRETS.get()
    handle: Token[tuple[str, ...]] = _REQUEST_SECRETS.set((*previous, secret))
    try:
        yield
    finally:
        _REQUEST_SECRETS.reset(handle)

_SECRET_PATTERNS = (
    re.compile(r"(Bearer\s+)[^\s,;]+", re.IGNORECASE),
    re.compile(r"(token[=:]\s*)[^\s,;]+", re.IGNORECASE),
    re.compile(r"(password[=:]\s*)[^\s,;]+", re.IGNORECASE),
)
_TOKEN_PATTERNS = (
    re.compile(r"\b(?:ghu_|ghr_|ghs_|github_pat_)[A-Za-z0-9_]+\b", re.IGNORECASE),
)
_REDACTED = "[REDACTED]"


def redact_sensitive(value: object, secrets: Iterable[str] = ()) -> str:
    """Return text safe for logs and user-facing diagnostics."""
    text = str(value)
    active_secrets = (*_REQUEST_SECRETS.get(), *secrets)
    for secret in active_secrets:
        if secret and len(secret) >= 4:
            text = text.replace(secret, _REDACTED)
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(rf"\1{_REDACTED}", text)
    for pattern in _TOKEN_PATTERNS:
        text = pattern.sub(_REDACTED, text)
    return text


def bounded_text(value: object, limit: int = 2_000) -> str:
    """Redact and cap diagnostic text to prevent log/response amplification."""
    text = redact_sensitive(value)
    if len(text) <= limit:
        return text
    return f"{text[:limit]}… [truncated]"


def parse_json_object(raw: str, *, context: str) -> dict[str, Any]:
    """Parse a JSON object and raise a bounded, actionable ValueError."""
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON from {context}: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object from {context}")
    return value


def parse_json_array(raw: str, *, context: str) -> list[Any]:
    """Parse a JSON array and raise a bounded, actionable ValueError."""
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON from {context}: {exc.msg}") from exc
    if not isinstance(value, list):
        raise ValueError(f"Expected a JSON array from {context}")
    return value


def atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Write JSON atomically with owner-only permissions."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        # The caller may provide a shared or pre-existing directory. The
        # cache file itself is still protected with mode 0600 below.
        pass
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        os.chmod(temporary_path, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        os.chmod(path, 0o600)
    except Exception:
        try:
            temporary_path.unlink(missing_ok=True)
        finally:
            raise


def parse_json_items(raw: str, *, context: str) -> list[Any]:
    """Parse a project item-list response from array or ``items`` envelope."""
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON from {context}: {exc.msg}") from exc

    if isinstance(value, list):
        return value
    if isinstance(value, dict) and isinstance(value.get("items"), list):
        return value["items"]
    raise ValueError(f"Expected a JSON array or items envelope from {context}")


def normalize_unique(values: Iterable[str] | None) -> list[str]:
    """Normalize optional string collections while preserving user order."""
    if not values:
        return []
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = value.strip()
        if normalized and normalized not in seen:
            result.append(normalized)
            seen.add(normalized)
    return result
