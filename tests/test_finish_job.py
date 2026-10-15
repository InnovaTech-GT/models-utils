"""finish_job, the one terminal transition (moved from backend-erp worker.py
`_finish`, provisioning worker split R1): job columns first, then each
follow-on in its own SAVEPOINT, in order; a failing follow-on is rolled back
alone and the outcome is kept. R2: the last follow-on emits JOB_FINISHED."""
import uuid

from database_utils.models.isp import (
    InventoryItemStatus,
    ProvisioningJob,
    ProvisioningJobStatus,
)
from database_utils.utils import provisioning_jobs as pj


def _job(db, plant, name="olt-activation", **kw):
    job = ProvisioningJob(id=uuid.uuid4(), company_id=plant.company_id,
                          playbook_id=plant.playbooks[name].id, variables={},
                          status=ProvisioningJobStatus.RUNNING, claim_token=uuid.uuid4(),
                          device_lock_key="lock", **kw)
    db.add(job)
    db.flush()
    return job


def _record(calls, name, fail=False):
    def fn(db, job):
        calls.append(name)
        if fail:
            raise RuntimeError(name)
    return fn


def test_follow_on_order(db, plant, monkeypatch):
    calls = []
    for attr in ("write_device_action_log", "stamp_connectivity", "advance_run"):
        monkeypatch.setattr(pj, attr, _record(calls, attr))
    monkeypatch.setattr(pj, "_emit_finished", lambda probed: _record(calls, "event"))
    job = _job(db, plant)
    pj.finish_job(db, job, ProvisioningJobStatus.SUCCEEDED, None, probed=True)
    assert calls == ["write_device_action_log", "stamp_connectivity", "advance_run", "event"]

    calls.clear()
    pj.finish_job(db, _job(db, plant), ProvisioningJobStatus.FAILED, "X", probed=False)
    assert calls == ["advance_run", "event"]  # not probed: no device_action_log / stamp


def test_failing_follow_on_keeps_the_outcome(db, plant, monkeypatch):
    calls = []
    job = _job(db, plant, log={"inflight_step": 0, "resume_at": 1, "steps": []})

    def dirty_then_fail(db, job):
        calls.append("advance_run")
        job.error = "overwritten"
        db.flush()
        raise RuntimeError("boom")

    monkeypatch.setattr(pj, "advance_run", dirty_then_fail)
    monkeypatch.setattr(pj, "_emit_finished", lambda probed: _record(calls, "event"))
    pj.finish_job(db, job, ProvisioningJobStatus.FAILED, "STEP_FAILED", probed=False)
    db.commit()
    db.expire_all()
    job = db.get(ProvisioningJob, job.id)
    assert calls == ["advance_run", "event"]  # later follow-ons still run
    assert job.status == ProvisioningJobStatus.FAILED
    assert job.error == "STEP_FAILED"  # the failing SAVEPOINT was rolled back alone
    assert job.finished_at is not None
    assert job.device_lock_key is None and job.claim_token is None
    assert job.log == {"steps": []}  # journal keys cleared


def test_stamp_connectivity_stamps_and_never_touches_status(db, plant):
    """R2: the IN_STOCK/RESERVED -> INSTALLED promotion moved to backend-erp's
    JOB_FINISHED handler; the stamp is timestamps only."""
    pb = plant._playbook("core_connectivity_check_ssh")
    for status in (InventoryItemStatus.IN_STOCK, InventoryItemStatus.RESERVED):
        plant.core.status = status
        job = ProvisioningJob(id=uuid.uuid4(), company_id=plant.company_id,
                              playbook_id=pb.id, inventory_item_id=plant.core.id,
                              variables={}, status=ProvisioningJobStatus.SUCCEEDED)
        db.add(job)
        db.flush()
        pj.stamp_connectivity(db, job)
        assert plant.core.mgmt_last_check_ok is True
        assert plant.core.mgmt_last_check_at is not None
        assert plant.core.status == status
