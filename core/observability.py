"""Optional OpenTelemetry tracing for MCP tool invocations.

Tracing is disabled by default. When enabled, the server exports one span per
tool invocation with the tool name, access tier, correlation ID and elapsed
runtime. No tool arguments, issue text, repository content, tokens, or error
messages are added as span attributes because those values may contain secrets
or user data.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

logger = logging.getLogger(__name__)

_CONFIGURED = False
_TRACER: Any | None = None


def configure_observability(settings: Any) -> bool:
    """Configure OpenTelemetry from validated settings.

    Returns True when tracing is active. Import and exporter failures degrade
    to disabled tracing rather than preventing the MCP server from starting;
    logging remains the always-available diagnostic path.
    """
    global _CONFIGURED, _TRACER
    if not settings.otel_enabled:
        return False
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        resource = Resource.create({"service.name": settings.otel_service_name})
        provider = TracerProvider(resource=resource)
        exporter_options: dict[str, str] = {}
        if settings.otel_exporter_endpoint:
            exporter_options["endpoint"] = settings.otel_exporter_endpoint
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(**exporter_options)))
        trace.set_tracer_provider(provider)
        _TRACER = trace.get_tracer("mcp-github-projects")
        _CONFIGURED = True
        logger.info("OpenTelemetry tracing enabled for service %s", settings.otel_service_name)
        return True
    except Exception as exc:
        _CONFIGURED = False
        _TRACER = None
        logger.warning("OpenTelemetry tracing disabled after setup failure: %s", exc)
        return False


def observability_enabled() -> bool:
    """Return whether tracing was configured successfully."""
    return _CONFIGURED and _TRACER is not None


@contextmanager
def tool_span(tool_name: str, *, correlation_id: str, access_tier: str) -> Iterator[Any | None]:
    """Create a bounded, metadata-only span for one tool invocation."""
    if not observability_enabled():
        yield None
        return

    from opentelemetry.trace import Status, StatusCode

    attributes = {
        "mcp.tool.name": tool_name,
        "mcp.tool.access_tier": access_tier,
        "mcp.correlation_id": correlation_id,
    }
    tracer = _TRACER
    assert tracer is not None  # guarded by observability_enabled()
    with tracer.start_as_current_span(f"mcp.tool {tool_name}", attributes=attributes) as span:
        try:
            yield span
        except Exception as exc:
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
            raise
        else:
            span.set_status(Status(StatusCode.OK))


__all__ = ["configure_observability", "observability_enabled", "tool_span"]
