"""Service-path provisioning runs (doc 35 §5).

Lives in models-utils, not backend-erp, for the same reason
provisioning_resolution does: the workflow engine's ENQUEUE_PROVISIONING path
opens runs too, and the engine cannot import backend-erp. Import direction is
strictly downward.

A run is the container; each configured device gets its own child job. Children
are created LAZILY, one at a time, in `plan` order, so at most one child of a
run is QUEUED or RUNNING at any moment. Two consequences worth stating plainly:

- No new ProvisioningJobStatus value was needed. A "BLOCKED" state would have
  had to be understood by every status consumer across three services and the
  automations run list.
- The worker's claim query is untouched. It still picks the oldest QUEUED job;
  it simply never sees a child that has not been created yet.

WHAT THIS MODULE DOES NOT DO: it does not evaluate the provisioning gates
(kill switch, dry-run gate). Those live in backend-erp and are called by its
routers before create_run, exactly as they are today. The workflow-engine path
still does not call them — a pre-existing gap recorded in doc 33 and doc 35
§10. Closing it here would silently change automation behaviour mid-cycle;
it is filed, not smuggled in.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict
from typing import Any, Dict, Optional

import sqlalchemy as sa
from sqlalchemy.orm import Session

from database_utils.models.isp import (
    ClientService,
    ProvisioningJob,
    ProvisioningJobStatus,
    ProvisioningRun,
    ProvisioningTrigger,
    PURPOSE_ACTIVATION,
)
from database_utils.utils.provisioning_resolution import (
    ResolvedProvisioning,
    resolve_provisioning,
)
from database_utils.utils.timezone_utils import now_gt

# Statuses in which a run (or job) is still live. Must mirror the predicate on
# uq_provisioning_run_company_idem — if these disagree, the dedupe check and
# the unique index disagree, and one of them starts raising IntegrityError.
IN_FLIGHT = (
    ProvisioningJobStatus.QUEUED,
    ProvisioningJobStatus.RUNNING,
    ProvisioningJobStatus.PENDING_INFORM,
)

TERMINAL_OK = (ProvisioningJobStatus.SUCCEEDED,)


def run_idempotency_key(client_service_id, purpose: str, dry_run: bool) -> str:
    """Stable key for "this service, this purpose, this mode".

    Deliberately mirrors the shape backend-erp's manual endpoint has always
    used, so a run and a legacy job never collide in the same namespace.
    """
    suffix = "-dry" if dry_run else ""
    return f"path-{client_service_id}-{purpose.lower()}{suffix}"


def find_in_flight_run(
    db: Session, company_id, idempotency_key: str
) -> Optional[ProvisioningRun]:
    if not idempotency_key:
        return None
    return db.execute(
        sa.select(ProvisioningRun).where(
            ProvisioningRun.company_id == company_id,
            ProvisioningRun.idempotency_key == idempotency_key,
            ProvisioningRun.status.in_(IN_FLIGHT),
        )
    ).scalars().first()


def _child_variables(run: ProvisioningRun, item_id: str) -> Dict[str, Any]:
    """shared | device — one flat dict, exactly what the renderer expects.

    The renderer contract (doc 33) is that `variables` is a flat dict whose keys
    are the whole dotted strings. Splitting the frames on the run and merging
    them here keeps that contract intact while letting `device.*` differ per
    node.
    """
    frames = run.frames or {}
    merged = dict(frames.get("shared") or {})
    merged.update((frames.get("device") or {}).get(str(item_id)) or {})
    return merged


def _device_lock_key(company_id, item_id) -> Optional[str]:
    return f"{company_id}:item:{item_id}" if item_id else None


def _queue_child(db: Session, run: ProvisioningRun, position: int) -> Optional[ProvisioningJob]:
    """Create and queue the child at `position`, or None if the plan is done."""
    plan = run.plan or []
    if position >= len(plan):
        return None
    entry = plan[position]
    item_id = entry["item_id"]
    job = ProvisioningJob(
        id=uuid.uuid4(),
        company_id=run.company_id,
        playbook_id=uuid.UUID(entry["playbook_id"]),
        client_service_id=run.client_service_id,
        inventory_item_id=uuid.UUID(item_id),
        variables=_child_variables(run, item_id),
        dry_run=run.dry_run,
        # Derived from the run's key so a child is still individually unique
        # under uq_provisioning_job_company_idem.
        idempotency_key=(
            f"{run.idempotency_key}#{position}" if run.idempotency_key else None
        ),
        triggered_by=run.triggered_by,
        triggered_by_user_id=run.triggered_by_user_id,
        device_lock_key=_device_lock_key(run.company_id, item_id),
        run_id=run.id,
        run_position=position,
        status=ProvisioningJobStatus.QUEUED,
    )
    db.add(job)
    db.flush()
    return job


def create_run(
    db: Session,
    client_service: ClientService,
    purpose: str = PURPOSE_ACTIVATION,
    dry_run: bool = False,
    triggered_by: ProvisioningTrigger = ProvisioningTrigger.USER,
    triggered_by_user_id=None,
    idempotency_key: Optional[str] = None,
    extra_variables: Optional[Dict[str, Any]] = None,
    resolution: Optional[ResolvedProvisioning] = None,
) -> ProvisioningRun:
    """Resolve the path and open a run with only its first child queued.

    `resolution` may be passed by a caller that already resolved (the manual
    endpoint resolves first so it can 422 with the error list before touching
    anything); otherwise it is resolved here. Either way it is resolved EXACTLY
    ONCE per run — the frames are snapshotted so later children cannot silently
    follow a path the operator never saw.
    """
    resolved = resolution or resolve_provisioning(db, client_service, purpose)

    shared = dict(resolved.shared_variables)
    if extra_variables:
        shared.update(extra_variables)

    run = ProvisioningRun(
        id=uuid.uuid4(),
        company_id=client_service.company_id,
        client_service_id=client_service.id,
        purpose=purpose,
        dry_run=dry_run,
        status=ProvisioningJobStatus.QUEUED,
        path=[asdict(n) | {"item_id": str(n.item_id),
                           "device_type_id": str(n.device_type_id),
                           "playbook_id": str(n.playbook_id) if n.playbook_id else None}
              for n in resolved.path],
        plan=[{"item_id": str(n.item_id),
               "playbook_id": str(n.playbook_id),
               "category_key": (n.category_key or "").lower()}
              for n in resolved.steps],
        frames={
            "shared": shared,
            "device": {str(k): v for k, v in resolved.device_variables.items()},
        },
        idempotency_key=idempotency_key
        or run_idempotency_key(client_service.id, purpose, dry_run),
        triggered_by=triggered_by,
        triggered_by_user_id=triggered_by_user_id,
    )
    db.add(run)
    db.flush()

    _queue_child(db, run, 0)
    return run


def advance_run(db: Session, job: ProvisioningJob) -> Optional[ProvisioningJob]:
    """Called when a job reaches a terminal state. Returns the next child, if any.

    A standalone job (run_id NULL) is a no-op — ACS reboots and connectivity
    probes must keep behaving exactly as they did.
    """
    if job.run_id is None:
        return None
    run = db.get(ProvisioningRun, job.run_id)
    if run is None:
        return None

    if job.status not in TERMINAL_OK:
        # Any non-success stops the run and takes that status. Continuing to the
        # OLT after the CPE step failed would leave the network configured for a
        # subscriber whose own device is not.
        run.status = job.status
        run.finished_at = now_gt()
        db.flush()
        return None

    nxt = _queue_child(db, run, (job.run_position or 0) + 1)
    if nxt is None:
        run.status = ProvisioningJobStatus.SUCCEEDED
        run.finished_at = now_gt()
        # The path this service was provisioned against is now the path it sits
        # on, so any re-parent drift recorded earlier is settled. Dry runs prove
        # nothing about the device, so they clear nothing.
        if not run.dry_run and run.purpose == PURPOSE_ACTIVATION:
            svc = db.get(ClientService, run.client_service_id)
            if svc is not None:
                svc.path_changed_at = None
    else:
        run.status = ProvisioningJobStatus.RUNNING
    db.flush()
    return nxt
