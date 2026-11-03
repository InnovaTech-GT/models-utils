"""Workflow v2 business events: the outbox emit helper (doc 63-workflow-engine §2.6).

Core code calls `emit_business_event` inside the transaction that made the
state change; the row commits (or rolls back) with it. The WorkflowWorker
drains `workflow_event` and starts the matching workflows. Never commits.

`current_depth` / `current_run_id` are set by the v2 executor while a step
runs, so an event caused by a workflow carries the loop-guard depth and the
run that caused it.
"""
from __future__ import annotations

from contextvars import ContextVar
from uuid import UUID

from sqlalchemy.orm import Session

from database_utils.models.workflow import WorkflowEvent
from database_utils.utils.audit_utils import serialize_for_audit

EVENT_NAMES = (
    "payment.confirmed",
    "client_service.activated",
    "client_service.suspended",
    "client_service.reactivated",
    "client_service.cancelled",
    "client_service.plan_changed",  # internal: boost early end; not offered as a trigger
    "task.completed",
    "task.created",
    "provisioning_run.closed",
)

current_depth: ContextVar[int] = ContextVar("workflow_event_depth", default=0)
current_run_id: ContextVar[UUID | None] = ContextVar("workflow_event_run_id", default=None)


def emit_business_event(db: Session, company_id: UUID, name: str, payload: dict, *,
                        flush: bool = True) -> WorkflowEvent:
    """Add one outbox row. `flush=False` is for callers inside an attribute
    validator (ClientService.status), where a flush would write a half-set row."""
    if name not in EVENT_NAMES:
        raise ValueError(f"unknown workflow event: {name}")
    event = WorkflowEvent(
        company_id=company_id, name=name, payload=serialize_for_audit(payload or {}),
        depth=current_depth.get(), source_run_id=current_run_id.get(),
    )
    db.add(event)
    if flush:
        db.flush()
    return event
