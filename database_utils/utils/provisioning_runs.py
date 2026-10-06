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
- Producers never lock. Every child is inserted with device_lock_key NULL;
  only the worker's claim writes the lock (provisioning-concurrency fix). A
  lock written at INSERT made the commit that recorded child N's outcome fail
  whenever child N+1's device was busy with another run or a probe (the
  2026-10-06 incident). The worker's claim skips a child whose device is held.
- Every run-row write goes through advance_run, which locks the run row and
  no-ops on a terminal run or a stale/duplicate advance, so a late settle can
  never resurrect a finished run or queue a child twice.

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
from datetime import timedelta
from typing import Any, Dict, Optional, Tuple

import sqlalchemy as sa
from loguru import logger
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database_utils.models.isp import (
    ClientService,
    ProvisioningJob,
    ProvisioningJobStatus,
    ProvisioningRun,
    ProvisioningTrigger,
    PURPOSE_ACTIVATION,
    PURPOSE_DEPROVISION,
)
from database_utils.utils.provisioning_resolution import (
    ResolvedProvisioning,
    resolve_provisioning,
)
from database_utils.utils.timezone_utils import make_aware_gt, now_gt

# Statuses in which a run (or job) is still live. Must mirror the predicate on
# uq_provisioning_run_company_idem — if these disagree, the dedupe check and
# the unique index disagree, and one of them starts raising IntegrityError.
IN_FLIGHT = (
    ProvisioningJobStatus.QUEUED,
    ProvisioningJobStatus.RUNNING,
    ProvisioningJobStatus.PENDING_INFORM,
)

TERMINAL_OK = (ProvisioningJobStatus.SUCCEEDED,)


# A run with no in-flight child is only "stranded" once it has been quiet this
# long; younger ones may still have a settle in progress.
STRANDED_RUN_GRACE = timedelta(seconds=30)
# ...and one quiet for longer than this is closed FAILED, not advanced: the
# reaper repairs a fresh stranding within a minute, so anything older (e.g. a
# run stranded before this backstop existed) would configure the next device
# from a plan resolved long ago, whatever the service's state is now.
STRANDED_RUN_MAX_AGE = timedelta(hours=1)


def run_idempotency_key(client_service_id, purpose: str, dry_run: bool = False) -> str:
    """Stable key for "this service, this purpose, this mode" — the ONE key
    every producer uses (manual /provision, the lifecycle hooks, the workflow
    engine's default), so they dedupe against each other instead of racing.

    DEPROVISION is the bare `deprovision-{id}`: that is literally what the
    retired 'service-removal' workflow template composes, so a still-active
    copy dedupes against the native lifecycle run instead of opening a second
    one. Every other purpose keeps a purpose suffix so a queued ACTIVATION
    cannot block a SUSPENSION. A dry run gets `-dry` so it never collides with
    (or blocks) the live run it precedes.
    """
    if str(purpose).upper() == PURPOSE_DEPROVISION:
        key = f"deprovision-{client_service_id}"
    else:
        key = f"path-provision-{client_service_id}-{purpose.lower()}"
    return key + "-dry" if dry_run else key


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
        # Never locked at INSERT: the worker's claim takes the device lock.
        device_lock_key=None,
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
        # playbook_version lets the worker refuse a child whose playbook was
        # edited after the run was resolved (PLAYBOOK_CHANGED_DURING_RUN).
        plan=[{"item_id": str(n.item_id),
               "playbook_id": str(n.playbook_id),
               "playbook_version": n.playbook_version,
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

    Locks the run row (lock order everywhere: job row, then run row) and is a
    no-op when:
      - the run is already terminal — a late settle never resurrects it;
      - `job` is itself still in flight;
      - a later child already exists — a duplicate or stale advance (reaped
        executor, cancel racing a settle, repair_stranded_runs) queues nothing.
    """
    if job.run_id is None:
        return None
    run = db.execute(
        sa.select(ProvisioningRun)
        .where(ProvisioningRun.id == job.run_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalars().first()
    if run is None or run.status not in IN_FLIGHT:
        return None
    if job.status in IN_FLIGHT:
        return None
    later = db.execute(
        sa.select(ProvisioningJob.id).where(
            ProvisioningJob.run_id == run.id,
            ProvisioningJob.run_position > (job.run_position or 0),
        ).limit(1)
    ).first()
    if later is not None:
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


def create_or_get_run(
    db: Session,
    client_service: ClientService,
    purpose: str = PURPOSE_ACTIVATION,
    dry_run: bool = False,
    idempotency_key: Optional[str] = None,
    **kw: Any,
) -> Tuple[ProvisioningRun, bool]:
    """create_run, deduped on the run key: returns (run, created).

    An in-flight run with the same key wins and comes back with created=False.
    The INSERT runs in a SAVEPOINT, so losing the race to a concurrent producer
    (uq_provisioning_run_company_idem) rolls back only the savepoint: the
    caller's session stays usable (a workflow execution still commits) and the
    winner's run is returned instead of a 500. Any other error propagates.
    """
    key = idempotency_key or run_idempotency_key(client_service.id, purpose, dry_run)
    existing = find_in_flight_run(db, client_service.company_id, key)
    if existing is not None:
        return existing, False
    try:
        with db.begin_nested():
            run = create_run(db, client_service, purpose=purpose, dry_run=dry_run,
                             idempotency_key=key, **kw)
    except IntegrityError:
        existing = find_in_flight_run(db, client_service.company_id, key)
        if existing is None:
            raise
        return existing, False
    return run, True


def repair_stranded_runs(db: Session, limit: int = 100) -> int:
    """Backstop: advance in-flight runs that have no in-flight child.

    A run strands when its child reached a terminal state but advance_run did
    not commit with it (the settle isolates it in a savepoint, so a failure
    there no longer rolls back the job's outcome). Each candidate is taken FOR
    UPDATE SKIP LOCKED — a run a settle is advancing right now is skipped — and
    repaired in its own savepoint. Only run rows are locked here, so there is no
    deadlock cycle with a settle (job row, then run row). A run whose last
    child finished more than STRANDED_RUN_MAX_AGE ago is closed FAILED
    (STRANDED_RUN_EXPIRED in the log) instead of advanced. Returns the number
    of runs repaired; the caller commits.
    """
    has_live_child = sa.exists().where(
        ProvisioningJob.run_id == ProvisioningRun.id,
        ProvisioningJob.status.in_(IN_FLIGHT),
    )
    runs = db.execute(
        sa.select(ProvisioningRun)
        .where(
            ProvisioningRun.status.in_(IN_FLIGHT),
            ProvisioningRun.updated_at < now_gt() - STRANDED_RUN_GRACE,
            ~has_live_child,
        )
        .order_by(ProvisioningRun.updated_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    ).scalars().all()

    repaired = 0
    for run in runs:
        run_id = run.id
        try:
            with db.begin_nested():
                last = db.execute(
                    sa.select(ProvisioningJob)
                    .where(ProvisioningJob.run_id == run_id)
                    .order_by(ProvisioningJob.run_position.desc())
                    .limit(1)
                ).scalars().first()
                if last is not None and last.status in IN_FLIGHT:
                    continue
                quiet_since = (last.finished_at if last is not None else None) or run.updated_at
                if make_aware_gt(quiet_since) < now_gt() - STRANDED_RUN_MAX_AGE:
                    run.status = ProvisioningJobStatus.FAILED
                    run.finished_at = now_gt()
                    db.flush()
                    logger.bind(run_id=str(run_id)).warning(
                        "repair_stranded_runs: STRANDED_RUN_EXPIRED, run closed FAILED")
                elif last is None:
                    if _queue_child(db, run, 0) is None:
                        # An empty plan has nothing to configure.
                        run.status = ProvisioningJobStatus.SUCCEEDED
                        run.finished_at = now_gt()
                        db.flush()
                else:
                    advance_run(db, last)
            repaired += 1
        except Exception:
            logger.bind(run_id=str(run_id)).exception("repair_stranded_runs: run not repaired")
    return repaired
