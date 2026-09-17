"""Service-path provisioning runs (doc 35 §5).

Reuses the seeded plant from the resolver tests: one company's
CORE-1 -> OLT-1 -> SPL-1 -> SPL-2 -> ONT-1 chain, splitters passive, ACTIVATION
and SUSPENSION bound to the router/olt/onu device types.
"""

import uuid

import sqlalchemy as sa

from database_utils.models import ProvisioningRun
from database_utils.models.isp import (
    ProvisioningJob,
    ProvisioningJobStatus,
    PURPOSE_ACTIVATION,
)
from database_utils.utils.provisioning_runs import (
    advance_run,
    create_run,
    find_in_flight_run,
    run_idempotency_key,
)



def _children(db, run):
    return db.execute(
        sa.select(ProvisioningJob)
        .where(ProvisioningJob.run_id == run.id)
        .order_by(ProvisioningJob.run_position)
    ).scalars().all()


# ------------------------------------------------------------------ schema

def test_run_snapshots_the_path_the_plan_and_the_variable_frames():
    cols = set(ProvisioningRun.__table__.c.keys())
    assert {"path", "plan", "frames", "purpose", "dry_run", "status",
            "client_service_id", "idempotency_key"} <= cols


def test_jobs_can_belong_to_a_run_but_need_not():
    cols = ProvisioningJob.__table__.c
    assert cols["run_id"].nullable is True, "standalone jobs must still work"
    assert cols["run_position"].nullable is True


def test_child_jobs_cascade_with_their_run():
    fk = next(iter(ProvisioningJob.__table__.c["run_id"].foreign_keys))
    assert fk.ondelete == "CASCADE"


# ------------------------------------------------------------- run creation

def test_a_run_creates_only_its_first_child(db, plant):
    run = create_run(db, plant.service, PURPOSE_ACTIVATION)
    jobs = _children(db, run)
    assert len(jobs) == 1, "children are created lazily, one at a time"
    assert jobs[0].run_position == 0
    assert jobs[0].inventory_item_id == plant.cpe.id


def test_the_plan_is_leaf_to_root_and_excludes_passives(db, plant):
    run = create_run(db, plant.service, PURPOSE_ACTIVATION)
    assert [p["category_key"] for p in run.plan] == ["onu", "olt", "router"]
    assert [p["category_key"] for p in run.path] == [
        "ONU", "SPLITTER", "SPLITTER", "OLT", "ROUTER"]


def test_the_path_snapshot_keeps_the_passives_visible(db, plant):
    run = create_run(db, plant.service, PURPOSE_ACTIVATION)
    passives = [p for p in run.path if p["is_passive"]]
    assert len(passives) == 2, "the run detail must show what was skipped"


def test_each_child_gets_shared_plus_its_own_device_frame(db, plant):
    run = create_run(db, plant.service, PURPOSE_ACTIVATION)
    first = _children(db, run)[0]
    assert first.variables["device.category"] == "onu"
    assert first.variables["path.olt.serial"] == "OLT-1"
    assert first.variables["cpe.serial"] == "ONT-1"


def test_each_child_locks_exactly_the_device_it_configures(db, plant):
    run = create_run(db, plant.service, PURPOSE_ACTIVATION)
    first = _children(db, run)[0]
    assert first.device_lock_key == f"{plant.company_id}:item:{plant.cpe.id}"


def test_author_variables_are_namespaced_and_cannot_shadow(db, plant):
    run = create_run(db, plant.service, PURPOSE_ACTIVATION,
                     extra_variables={"input.probe": "x"})
    assert run.frames["shared"]["input.probe"] == "x"
    assert run.frames["shared"]["cpe.serial"] == "ONT-1"


# ----------------------------------------------------------------- advance

def test_advance_enqueues_the_next_child_on_success(db, plant):
    run = create_run(db, plant.service, PURPOSE_ACTIVATION)
    first = _children(db, run)[0]
    first.status = ProvisioningJobStatus.SUCCEEDED
    nxt = advance_run(db, first)
    assert nxt.run_position == 1
    assert nxt.inventory_item_id == plant.olt.id
    assert nxt.variables["device.serial"] == "OLT-1"


def test_advance_stops_the_run_on_failure(db, plant):
    run = create_run(db, plant.service, PURPOSE_ACTIVATION)
    first = _children(db, run)[0]
    first.status = ProvisioningJobStatus.FAILED
    assert advance_run(db, first) is None
    assert run.status == ProvisioningJobStatus.FAILED
    assert run.finished_at is not None
    assert len(_children(db, run)) == 1, "the OLT must never be touched"


def test_the_last_child_finishes_the_run(db, plant):
    run = create_run(db, plant.service, PURPOSE_ACTIVATION)
    job = _children(db, run)[0]
    while job is not None:
        job.status = ProvisioningJobStatus.SUCCEEDED
        job = advance_run(db, job)
    assert run.status == ProvisioningJobStatus.SUCCEEDED
    assert run.finished_at is not None
    assert len(_children(db, run)) == 3


def test_a_successful_activation_clears_the_drift_stamp(db, plant):
    from database_utils.utils.timezone_utils import now_gt
    plant.service.path_changed_at = now_gt()
    db.flush()
    run = create_run(db, plant.service, PURPOSE_ACTIVATION)
    job = _children(db, run)[0]
    while job is not None:
        job.status = ProvisioningJobStatus.SUCCEEDED
        job = advance_run(db, job)
    db.refresh(plant.service)
    assert plant.service.path_changed_at is None


def test_a_dry_run_clears_nothing(db, plant):
    from database_utils.utils.timezone_utils import now_gt
    plant.service.path_changed_at = now_gt()
    db.flush()
    run = create_run(db, plant.service, PURPOSE_ACTIVATION, dry_run=True)
    job = _children(db, run)[0]
    while job is not None:
        job.status = ProvisioningJobStatus.SUCCEEDED
        job = advance_run(db, job)
    db.refresh(plant.service)
    assert plant.service.path_changed_at is not None, "a simulation proves nothing"


def test_standalone_jobs_are_untouched(db, plant):
    """ACS reboots and connectivity probes have no run and must not gain one."""
    pb = plant._playbook("core-connectivity")
    job = ProvisioningJob(id=uuid.uuid4(), company_id=plant.company_id,
                          playbook_id=pb.id, variables={})
    db.add(job)
    db.flush()
    assert job.run_id is None
    assert advance_run(db, job) is None


# ------------------------------------------------------------- idempotency

def test_an_in_flight_run_is_found_by_its_key(db, plant):
    run = create_run(db, plant.service, PURPOSE_ACTIVATION)
    found = find_in_flight_run(db, plant.company_id, run.idempotency_key)
    assert found is not None and found.id == run.id


def test_a_finished_run_no_longer_blocks_a_new_one(db, plant):
    run = create_run(db, plant.service, PURPOSE_ACTIVATION)
    run.status = ProvisioningJobStatus.SUCCEEDED
    db.flush()
    assert find_in_flight_run(db, plant.company_id, run.idempotency_key) is None


def test_dry_runs_and_live_runs_have_different_keys(db, plant):
    assert run_idempotency_key(plant.service.id, "ACTIVATION", True) != \
        run_idempotency_key(plant.service.id, "ACTIVATION", False)


def test_child_keys_are_derived_from_the_run_key(db, plant):
    run = create_run(db, plant.service, PURPOSE_ACTIVATION)
    first = _children(db, run)[0]
    assert first.idempotency_key == f"{run.idempotency_key}#0"


def test_a_run_is_company_scoped(db, plant):
    run = create_run(db, plant.service, PURPOSE_ACTIVATION)
    assert find_in_flight_run(db, uuid.uuid4(), run.idempotency_key) is None
