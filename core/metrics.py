"""In-process operational metrics for MCP tool invocations.

The registry intentionally stores only aggregate metadata: tool names, counts,
latency summaries and rate-limit counters. It never stores arguments, issue
content, repository data, bearer tokens, or error messages. Metrics are local
to the process and reset when the server restarts; OpenTelemetry remains the
optional durable observability path.
"""

from __future__ import annotations

import threading
import time
from typing import Any

_LOCK = threading.RLock()
_STARTED_MONOTONIC = time.monotonic()
_MAX_TOOL_SERIES = 512
_MAX_RATE_LIMIT_SERIES = 64
_TOOL_METRICS: dict[str, dict[str, Any]] = {}
_RATE_LIMITS: dict[str, int] = {}


def _new_series() -> dict[str, Any]:
    """Return an empty per-tool aggregate series."""
    return {
        "calls": 0,
        "errors": 0,
        "total_ms": 0,
        "max_ms": 0,
        "last_ms": 0,
        "last_ok": True,
        "updated_at": 0.0,
    }


def record_tool_invocation(tool_name: str, elapsed_ms: int, *, ok: bool) -> None:
    """Record one bounded aggregate observation for a tool invocation."""
    if not tool_name:
        return
    elapsed = max(0, int(elapsed_ms))
    with _LOCK:
        if tool_name not in _TOOL_METRICS and len(_TOOL_METRICS) >= _MAX_TOOL_SERIES:
            return
        series = _TOOL_METRICS.setdefault(tool_name, _new_series())
        series["calls"] += 1
        series["errors"] += 0 if ok else 1
        series["total_ms"] += elapsed
        series["max_ms"] = max(int(series["max_ms"]), elapsed)
        series["last_ms"] = elapsed
        series["last_ok"] = bool(ok)
        series["updated_at"] = time.time()


def record_rate_limit(kind: str) -> None:
    """Increment a bounded rate-limit counter by category."""
    normalized = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in kind.strip().lower())
    normalized = normalized.strip("_")[:64] or "unknown"
    with _LOCK:
        if normalized not in _RATE_LIMITS and len(_RATE_LIMITS) >= _MAX_RATE_LIMIT_SERIES:
            return
        _RATE_LIMITS[normalized] = _RATE_LIMITS.get(normalized, 0) + 1


def metrics_snapshot(*, tool_name: str | None = None, limit: int = 50) -> dict[str, Any]:
    """Return a JSON-safe snapshot of aggregate operational metrics."""
    bounded_limit = max(1, min(int(limit), 200))
    with _LOCK:
        items = list(_TOOL_METRICS.items())
        if tool_name:
            items = [item for item in items if item[0] == tool_name]
        items.sort(key=lambda item: (-int(item[1]["calls"]), item[0]))
        selected = items[:bounded_limit]
        tools = []
        total_calls = 0
        total_errors = 0
        for name, series in items:
            total_calls += int(series["calls"])
            total_errors += int(series["errors"])
        for name, series in selected:
            calls = int(series["calls"])
            tools.append(
                {
                    "tool": name,
                    "calls": calls,
                    "errors": int(series["errors"]),
                    "error_rate": round(int(series["errors"]) / calls, 4) if calls else 0.0,
                    "avg_ms": round(int(series["total_ms"]) / calls, 2) if calls else 0.0,
                    "max_ms": int(series["max_ms"]),
                    "last_ms": int(series["last_ms"]),
                    "last_ok": bool(series["last_ok"]),
                    "updated_at": int(series["updated_at"]),
                }
            )
        return {
            "uptime_seconds": int(time.monotonic() - _STARTED_MONOTONIC),
            "totals": {
                "calls": total_calls,
                "errors": total_errors,
                "tools_with_calls": len(items),
            },
            "rate_limits": dict(sorted(_RATE_LIMITS.items())),
            "tools": tools,
            "truncated": len(items) > len(selected),
        }


def reset_metrics() -> None:
    """Reset the in-process registry (intended for tests)."""
    with _LOCK:
        _TOOL_METRICS.clear()
        _RATE_LIMITS.clear()


__all__ = ["metrics_snapshot", "record_rate_limit", "record_tool_invocation", "reset_metrics"]
