"""The provisioning outbox (worker split R2, design §3.2): emit_event, the
RUN_CLOSED emitted by close_run, the JOB_FINISHED emitted by finish_job and
its id order against RUN_CLOSED, and the connectivity stamp no longer
promoting."""
import uuid

import pytest
import sqlalchemy as sa

from database_utils.models.isp import (
    PURPOSE_ACTIVATION,
    InventoryItemStatus,
    ProvisioningEvent,
    ProvisioningJob,
)
from database_utils.models.isp import ProvisioningJobStatus as S
from database_utils.utils import provisioning_events as pe
from database_utils.utils.provisioning_jobs import finish_job
from database_utils.utils.provisioning_runs import close_run, create_run


def _events(db):
    return db.execute(sa.select(ProvisioningEvent).order_by(ProvisioningEvent.id)).scalars().all()


def _next_child(db, run, after=-1):
    return db.execute(sa.select(ProvisioningJob).where(
        ProvisioningJob.run_id == run.id, ProvisioningJob.run_position > after)
        .order_by(ProvisioningJob.run_position)).scalars().first()


def test_emit_event_derives_ids_and_never_commits(db, plant):
    run = create_run(db, plant.service, PURPOSE_ACTIVATION)
    job = _next_child(db, run)
    ev = pe.emit_event(db, pe.MANUAL_WAITING, job=job)
    assert (ev.company_id, ev.run_id, ev.job_id) == (plant.company_id, run.id, job.id)
    assert ev.id is not None and ev.processed_at is None and ev.attempts == 0
    assert ev.created_at is not None
    db.rollback()
    assert _events(db) == []  # the caller owns the transaction

    ev = pe.emit_event(db, pe.INSTALL_STATE_STALE, company_id=plant.company_id,
                       payload={"client_service_id": "x"})
    assert ev.run_id is None and ev.job_id is None and ev.payload == {"client_service_id": "x"}


def test_emit_event_refuses_unknown_kind_and_missing_company(db):
    with pytest.raises(ValueError):
        pe.emit_event(db, "NOPE", company_id=uuid.uuid4())
    with pytest.raises(ValueError):
        pe.emit_event(db, pe.JOB_UPDATED)


def test_close_run_emits_once(db, plant):
    run = create_run(db, plant.service, PURPOSE_ACTIVATION)
    close_run(db, run, S.FAILED, "PRECONDITION_FAILED")
    [ev] = _events(db)
    assert ev.kind == pe.RUN_CLOSED and ev.run_id == run.id and ev.job_id is None
    assert ev.company_id == plant.company_id
    assert ev.payload == {"status": "FAILED", "error_code": "PRECONDITION_FAILED"}


def test_a_dry_run_emits_nothing(db, plant):
    run = create_run(db, plant.service, PURPOSE_ACTIVATION, dry_run=True)
    close_run(db, run, S.SUCCEEDED)
    assert _events(db) == []


def test_finish_job_emits_job_finished_after_run_closed(db, plant):
    run = create_run(db, plant.service, PURPOSE_ACTIVATION)
    job, last = _next_child(db, run), None
    while job is not None:
        names = [s["name"] for s in run.plan[job.run_position]["steps"]]
        job.status = S.RUNNING
        job.log = {"steps": [{"name": n, "status": "SUCCEEDED"} for n in names]}
        finish_job(db, job, S.SUCCEEDED, None, probed=False)
        last, job = job, _next_child(db, run, job.run_position)
    assert run.status == S.SUCCEEDED

    events = _events(db)
    assert [e.kind for e in events] == [pe.JOB_FINISHED] * 2 + [pe.RUN_CLOSED, pe.JOB_FINISHED]
    closed, final = events[-2:]
    assert closed.id < final.id, "the backend drains RUN_CLOSED before the final JOB_FINISHED"
    assert final.job_id == last.id and final.run_id == run.id
    assert final.payload == {"status": "SUCCEEDED", "error": None, "probed": False,
                             "playbook_name": last.playbook.name,
                             "client_service_id": str(plant.service.id)}


def test_a_connectivity_job_never_touches_inventory_status(db, plant):
    pb = plant._playbook("core_connectivity_check_ssh")
    plant.core.status = InventoryItemStatus.IN_STOCK
    job = ProvisioningJob(id=uuid.uuid4(), company_id=plant.company_id, playbook_id=pb.id,
                          inventory_item_id=plant.core.id, variables={}, status=S.RUNNING)
    db.add(job)
    db.flush()
    finish_job(db, job, S.SUCCEEDED, None, probed=True)
    assert plant.core.mgmt_last_check_ok is True
    assert plant.core.status == InventoryItemStatus.IN_STOCK  # promotion is the backend's now
    [ev] = _events(db)
    assert ev.kind == pe.JOB_FINISHED and ev.run_id is None
    assert ev.payload["probed"] is True
    assert ev.payload["playbook_name"] == "core_connectivity_check_ssh"
    assert ev.payload["client_service_id"] is None
