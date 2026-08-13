"""OpenTelemetry tracing across agent hops.

The interesting property of a multi-agent system is that one logical request is
spread over several processes. Structured logs already carry a shared
``context_id``; tracing goes further and reconstructs the *shape* of the run —
which agent called which, in what order, and where the time went.

Propagation is standard W3C ``traceparent`` over HTTP:

* outbound — httpx instrumentation injects the header into every A2A call
* inbound  — ASGI instrumentation extracts it and continues the trace

so the coordinator's span becomes the parent of the researcher's, one process
over. The A2A SDK instruments its own internals, so protocol-level spans appear
underneath without any extra work here.

Tracing is off unless ``TELEMETRY_ENABLED=true``; nothing else in the codebase
imports OpenTelemetry, so the feature is genuinely optional.
"""

from __future__ import annotations

from contextlib import AbstractContextManager, nullcontext
from typing import TYPE_CHECKING, Any

from research_desk.logging import get_logger

if TYPE_CHECKING:
    from starlette.types import ASGIApp

logger = get_logger(__name__)

_configured = False


def configure_tracing(service_name: str, *, enabled: bool, endpoint: str) -> bool:
    """Install a tracer provider exporting over OTLP/HTTP.

    Returns whether tracing is *active*, not whether this call did the work — a
    process that builds several agents (``research-desk dev``) configures once
    but must instrument every app it builds.

    ``endpoint`` is the collector root (Jaeger's OTLP port, say
    ``http://jaeger:4318``); the signal path is appended here so operators
    configure one obvious value.
    """
    global _configured
    if not enabled:
        return False
    if _configured:
        return True

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        logger.warning(
            "telemetry enabled but OpenTelemetry is not installed; "
            "reinstall with the 'telemetry' extra"
        )
        return False

    provider = TracerProvider(
        resource=Resource.create(
            {"service.name": service_name, "service.namespace": "research-desk"}
        )
    )
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint.rstrip('/')}/v1/traces"))
    )
    trace.set_tracer_provider(provider)

    # Injects traceparent into outbound A2A calls — the other half of the hop.
    HTTPXClientInstrumentor().instrument()

    _configured = True
    logger.info("tracing enabled", extra={"service": service_name, "otlp_endpoint": endpoint})
    return True


def instrument_app(app: ASGIApp, *, enabled: bool) -> ASGIApp:
    """Wrap an ASGI app so inbound requests continue the caller's trace."""
    if not enabled:
        return app
    try:
        from opentelemetry.instrumentation.asgi import OpenTelemetryMiddleware
    except ImportError:
        return app
    return OpenTelemetryMiddleware(app)


def annotate_task(context_id: str, task_id: str, **attributes: str) -> None:
    """Tag the active span with the A2A identifiers.

    Makes a trace searchable by the same ``context_id`` that appears in the
    logs, which is what turns "I have a log line" into "I have the whole run".
    """
    if not _configured:
        return
    from opentelemetry import trace

    span = trace.get_current_span()
    if not span.is_recording():
        return
    span.set_attribute("a2a.context_id", context_id)
    span.set_attribute("a2a.task_id", task_id)
    for key, value in attributes.items():
        span.set_attribute(f"a2a.{key}", value)


def span(name: str, **attributes: str) -> AbstractContextManager[Any]:
    """Start a span as a context manager, or a no-op when tracing is off."""
    if not _configured:
        return nullcontext()
    from opentelemetry import trace

    return trace.get_tracer("research-desk").start_as_current_span(
        name, attributes={f"a2a.{k}": v for k, v in attributes.items()}
    )
