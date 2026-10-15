"""
The provisioning job's terminal transition and its follow-ons, shared by the
worker (every settle, the reaper, the parked-job expiry) and the API («Abortar»
on a parked guided/manual step).

Moved from backend-erp provisioning/worker.py (provisioning worker split, R1):
`_finish` -> `finish_job`, `_write_device_action_log` ->
`write_device_action_log`, `_stamp_connectivity` -> `stamp_connectivity`,
`fail_parked`. R2 (6.10.0): the business follow-ons left this module. Every
terminal transition emits a `JOB_FINISHED` provisioning_event, and
backend-erp's event consumer runs the install_state recompute and the
IN_STOCK/RESERVED -> INSTALLED promotion from it. The caller always commits.
"""
from __future__ import annotations

from typing import Optional

from loguru import logger

from database_utils.models.isp import (
    DeviceActionLog,
    InventoryItem,
    ProvisioningJob,
    ProvisioningJobStatus,
    ProvisioningRun,
)
from database_utils.utils.provisioning_gates import CORE_CONNECTIVITY_PLAYBOOK_NAMES
from database_utils.utils.provisioning_events import JOB_FINISHED, emit_event
from database_utils.utils.provisioning_runs import advance_run, parked_step
from database_utils.utils.timezone_utils import now_gt


def write_device_action_log(db, job: ProvisioningJob) -> None:
    """canon C14: one append-only device_action_log row per terminal
    device-mutating job (reboot/factory-reset/tr069 provisioning steps).
    Skipped for dry-run jobs (nothing touched a device) and for jobs whose
    log has no tr069 step (e.g. a bare http-integration job — out of this
    table's scope). The DB trigger (nc1b) additionally enforces append-only
    at the schema level; this module never UPDATEs/DELETEs a row here."""
    if job.dry_run:
        return
    steps = (job.log or {}).get("steps") or []
    tr069_steps = [s for s in steps if s.get("driver") == "tr069"]
    if not tr069_steps:
        return

    serial = (job.variables or {}).get("serial") or (job.variables or {}).get("cpe_serial")
    actor_kind = "user" if job.triggered_by and job.triggered_by.value == "USER" else "automation"

    db.add(DeviceActionLog(
        company_id=job.company_id,
        actor_user_id=job.triggered_by_user_id,
        actor_kind=actor_kind,
        device_kind="cpe",
        device_identity=serial,
        action=job.playbook.name if job.playbook else "provisioning_job",
        after_data={"job_status": job.status.value, "steps": [s.get("name") for s in tr069_steps]},
        provisioning_job_id=job.id,
        detail={"error": job.error} if job.error else None,
    ))
    db.flush()


def stamp_connectivity(db, job: ProvisioningJob) -> None:
    """Cycle 7 (doc 25 §5.3): a job running a core_connectivity_check playbook
    stamps inventory_item.mgmt_last_check_at / mgmt_last_check_ok
    (ok = SUCCEEDED). Timestamps only: it never touches `status`. The
    IN_STOCK/RESERVED -> INSTALLED promotion of a core device that answered
    moved to backend-erp's JOB_FINISHED handler (R2)."""
    playbook_name = job.playbook.name if job.playbook else None
    if playbook_name not in CORE_CONNECTIVITY_PLAYBOOK_NAMES or job.inventory_item_id is None:
        return
    item = db.query(InventoryItem).filter(InventoryItem.id == job.inventory_item_id).first()
    if item is None:
        return
    item.mgmt_last_check_at = now_gt()
    item.mgmt_last_check_ok = job.status == ProvisioningJobStatus.SUCCEEDED
    db.flush()


def finish_job(db, job: ProvisioningJob, status: ProvisioningJobStatus, error: Optional[str],
               *, probed: bool) -> None:
    """The ONE terminal transition (success, failure, rollback, gate-blocked,
    INTEGRATION_DISABLED, PLAYBOOK_CHANGED, INFORM_TIMEOUT, LEASE_EXPIRED,
    UNKNOWN_STEP_OUTCOME, MAX_ATTEMPTS_EXCEEDED, INTERRUPTED_AFTER_STEP_FAILURE).

    The job's own columns are written and flushed first; they always commit
    with the caller's transaction. The follow-ons each run in their own
    SAVEPOINT, so one failing (I3/I9) is logged and rolled back alone instead
    of undoing the outcome and leaving the job RUNNING to be re-executed:
      - device_action_log and the connectivity stamp, only when `probed` (a
        device was actually talked to);
      - Cycle 10 (doc 35 §5) advance_run — queues the next device's child job
        or closes the run. `repair_stranded_runs` (reaper) is its backstop;
      - a JOB_FINISHED provisioning_event, AFTER advance_run (so its id
        follows the RUN_CLOSED that a final child's close_run emits). Payload
        `{status, error, probed, playbook_name, client_service_id}`. The
        backend's handler recomputes install_state, which reads run status,
        so it must see the run already closed; then it promotes a core
        device that answered a connectivity check.
    The caller commits."""
    job_id, company_id = job.id, job.company_id
    job.status = status
    job.error = error
    job.finished_at = now_gt()
    job.device_lock_key = None  # canon C11: only a terminal transition frees it
    job.claim_token = None
    job.pending_step_index = None
    job.pending_task_ids = None
    # Reassigned, never mutated in place: a JSON column only sees assignment.
    if job.log is not None:
        log = {k: v for k, v in job.log.items()
               if k not in ("inflight_step", "resume_at", "inflight_name")}
        # doc 42 §8.3: a run child that died mid-step (UNKNOWN_STEP_OUTCOME,
        # LEASE_EXPIRED, ...) keeps the step NAME, so models-utils ran_steps
        # puts its device in the rollback set even when it died on step 0.
        if (job.run_id is not None and status != ProvisioningJobStatus.SUCCEEDED
                and job.log.get("inflight_step") is not None and job.log.get("inflight_name")):
            log["interrupted_step"] = job.log["inflight_name"]
        job.log = log
    db.flush()

    follow_ons = []
    if probed:
        follow_ons += [("device_action_log", write_device_action_log),
                       ("connectivity_stamp", stamp_connectivity)]
    follow_ons += [("advance_run", advance_run), ("event", _emit_finished(probed))]
    for name, fn in follow_ons:
        try:
            with db.begin_nested():
                fn(db, job)
        except Exception:
            logger.bind(job_id=str(job_id), company_id=str(company_id), follow_on=name).exception(
                "provisioning job terminal follow-on failed (outcome kept)"
            )


def _emit_finished(probed: bool):
    def emit(db, job: ProvisioningJob) -> None:
        emit_event(db, JOB_FINISHED, job=job, payload={
            "status": job.status.value,
            "error": job.error,
            "probed": probed,
            "playbook_name": job.playbook.name if job.playbook else None,
            "client_service_id": str(job.client_service_id) if job.client_service_id else None,
        })
    return emit


def fail_parked(db, job: ProvisioningJob, code: str, *, error: str, display: str,
                **extra) -> None:
    """Fail a PENDING_MANUAL job the caller holds locked: its PENDING entry
    becomes FAILED `code`, then `finish_job` (rollback / close via
    advance_run, then JOB_FINISHED).
    A manual entry fails at stage command (the person may have changed the
    device, so ran_steps counts it); a guided one keeps stage render (never
    sent). Shared by the expiry (§6.4, doc 50 §4.5) and «Abortar» (doc 50
    §4.4, services/guided_steps.py). The caller commits."""
    run = db.get(ProvisioningRun, job.run_id) if job.run_id else None
    hint = parked_step(run, job).get("hint") if run is not None else None
    guided = (job.log or {}).get("pending_kind") == "guided"
    log = {k: v for k, v in (job.log or {}).items()
           if k not in ("pending_kind", "guided_preview")}
    steps = list(log.get("steps") or [])
    if steps and isinstance(steps[-1], dict) and steps[-1].get("status") == "PENDING":
        entry = {**steps[-1], **extra, "status": "FAILED", "code": code, "error": error,
                 "detail": {**(steps[-1].get("detail") or {}),
                            "stage": "render" if guided else "command", "code": code},
                 "display": display}
        if hint:
            entry["hint"] = hint
        steps[-1] = entry
    log.update(steps=steps, inflight_step=None, code=code)
    job.log = log
    finish_job(db, job, ProvisioningJobStatus.FAILED, code, probed=True)
