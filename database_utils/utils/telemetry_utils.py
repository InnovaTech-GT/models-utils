"""
Shared OpenTelemetry instrumentation utilities for the ERP system.

Provides tracer access and span attribute enrichment from request context.
Both backend-erp and auth-erp import from here.
"""
from typing import Optional
from opentelemetry import trace
from opentelemetry.trace import Span

from database_utils.utils.logging_utils import (
    request_id_context,
    user_id_context,
    company_id_context,
    user_roles_context,
    request_client_context,
)


def get_tracer(name: str) -> trace.Tracer:
    """
    Get an OpenTelemetry tracer for the given module name.

    Usage:
        tracer = get_tracer(__name__)
        with tracer.start_as_current_span("order.create") as span:
            ...
    """
    return trace.get_tracer(name)


def set_request_span_attributes(span: Optional[Span] = None) -> None:
    """
    Enrich a span with request-scoped identity attributes from contextvars.

    Reads user_id, company_id, request_id, and user_roles from the request
    context (populated by LoggingMiddleware) and sets them as filterable
    span attributes.

    If span is None, targets the currently active span via trace.get_current_span().
    If there is no active span (e.g., background task), this is a no-op — safe
    to call unconditionally.
    """
    target = span if span is not None else trace.get_current_span()

    # get_current_span() returns a NonRecordingSpan when there is no active span.
    # is_recording() check ensures we skip all attribute work safely.
    if not target.is_recording():
        return

    if request_id := request_id_context.get():
        target.set_attribute("request.id", request_id)

    if user_id := user_id_context.get():
        target.set_attribute("enduser.id", str(user_id))

    if company_id := company_id_context.get():
        target.set_attribute("company.id", str(company_id))

    if roles := user_roles_context.get():
        target.set_attribute("enduser.role", roles[0] if roles else "")
        target.set_attribute("enduser.roles", ",".join(str(r) for r in roles))

    if client := request_client_context.get():
        target.set_attribute("uplink.client", client)
