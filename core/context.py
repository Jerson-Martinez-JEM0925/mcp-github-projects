"""Per-invocation request context (HARDENING_200 item 7).

Every MCP tool call runs inside a :class:`RequestContext` that carries a local
correlation ID, the tool name and the start time. The context lives in a
``contextvars.ContextVar``, so it follows the call across ``await`` points
without being threaded through every signature, and concurrent calls never see
each other's context.

Two consumers read it:

* :class:`RequestContextFilter` stamps ``correlation_id`` and ``tool`` on every
  log record, so all stderr lines of one call can be grepped together.
* ``core.error_handling.build_error_response`` copies the correlation ID into
  the error envelope, so a user can quote it when reporting a failure.

The correlation ID is generated locally and is unrelated to GitHub's own
``X-Request-Id`` (``ToolError.request_id``).
"""

from __future__ import annotations

import functools
import inspect
import logging
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

_CURRENT: ContextVar[RequestContext | None] = ContextVar("mcp_request_context", default=None)


@dataclass(frozen=True, slots=True)
class RequestContext:
    """Metadata for one tool invocation."""

    tool: str
    correlation_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    started_at: float = field(default_factory=time.monotonic)

    def elapsed_ms(self) -> int:
        """Milliseconds since the invocation started."""
        return int((time.monotonic() - self.started_at) * 1000)


def current_request() -> RequestContext | None:
    """Return the context of the running tool call, or ``None`` outside one."""
    return _CURRENT.get()


@contextmanager
def request_scope(tool: str) -> Iterator[RequestContext]:
    """Bind a fresh :class:`RequestContext` for the duration of the block."""
    ctx = RequestContext(tool=tool)
    token = _CURRENT.set(ctx)
    try:
        yield ctx
    finally:
        _CURRENT.reset(token)


def with_request_context(fn: Callable[..., Any], tool: str | None = None) -> Callable[..., Any]:
    """Wrap a tool function so each call runs inside a request scope.

    ``functools.wraps`` keeps ``__name__``, the docstring, annotations and
    ``__wrapped__``, so FastMCP derives exactly the same name and input schema
    as for the undecorated function.
    """
    name = tool or fn.__name__
    logger = logging.getLogger("mcp.request")

    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def _async_wrapper(*args: Any, **kwargs: Any) -> Any:
            with request_scope(name) as ctx:
                try:
                    return await fn(*args, **kwargs)
                finally:
                    logger.debug("tool=%s finished in %dms", name, ctx.elapsed_ms())

        return _async_wrapper

    @functools.wraps(fn)
    def _sync_wrapper(*args: Any, **kwargs: Any) -> Any:
        with request_scope(name) as ctx:
            try:
                return fn(*args, **kwargs)
            finally:
                logger.debug("tool=%s finished in %dms", name, ctx.elapsed_ms())

    return _sync_wrapper


class RequestContextFilter(logging.Filter):
    """Logging filter that adds ``correlation_id`` and ``tool`` to records."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003 - logging API
        ctx = _CURRENT.get()
        record.correlation_id = ctx.correlation_id if ctx else "-"
        record.tool = ctx.tool if ctx else "-"
        return True


__all__ = [
    "RequestContext",
    "RequestContextFilter",
    "current_request",
    "request_scope",
    "with_request_context",
]
