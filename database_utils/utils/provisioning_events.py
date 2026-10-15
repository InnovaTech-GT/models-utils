"""
The provisioning outbox writer (provisioning worker split R2, design §3.2).

Device-side code (the worker, and the models-utils state machine it drives)
records "something happened" as a `provisioning_event` row in the SAME
transaction as the state change, instead of calling business code in
process. backend-erp's `services/provisioning_events.py` drains the rows in
`id` order and runs the follow-on (run settlement, notifications, the
install_state recompute and INSTALLED promotion, workflow triggers).

`emit_event` adds and flushes; it never commits (the caller owns the
transaction, so the event commits or rolls back with the change).
"""
from __future__ import annotations

from typing import Any, Optional

from database_utils.models.isp import ProvisioningEvent

RUN_CLOSED = "RUN_CLOSED"                    # close_run, non-dry runs
MANUAL_WAITING = "MANUAL_WAITING"            # worker park on a manual step
JOB_FINISHED = "JOB_FINISHED"                # finish_job, every terminal transition
JOB_UPDATED = "JOB_UPDATED"                  # worker, the two workflow-trigger sites
INSTALL_STATE_STALE = "INSTALL_STATE_STALE"  # acs_sync claim / first-inform stamp

KINDS = (RUN_CLOSED, MANUAL_WAITING, JOB_FINISHED, JOB_UPDATED, INSTALL_STATE_STALE)


def emit_event(db, kind: str, *, job=None, run=None, company_id=None,
               payload: Optional[dict[str, Any]] = None) -> ProvisioningEvent:
    """Insert one event. `company_id` / `run_id` / `job_id` come from `job`
    and `run` when given; an explicit `company_id` wins. `payload` must be
    JSON-serialisable."""
    if kind not in KINDS:
        raise ValueError(f"unknown provisioning event kind {kind!r}")
    company_id = company_id or (job.company_id if job is not None else None) \
        or (run.company_id if run is not None else None)
    if company_id is None:
        raise ValueError("emit_event needs a job, a run or a company_id")
    run_id = run.id if run is not None else (job.run_id if job is not None else None)
    event = ProvisioningEvent(company_id=company_id, kind=kind, run_id=run_id,
                              job_id=job.id if job is not None else None, payload=payload)
    db.add(event)
    db.flush()
    return event
