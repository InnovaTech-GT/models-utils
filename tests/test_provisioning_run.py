"""Service-path provisioning runs (doc 35 §5).

Reuses the seeded plant from the resolver tests: one company's
CORE-1 -> OLT-1 -> SPL-1 -> SPL-2 -> ONT-1 chain, splitters passive, ACTIVATION
and SUSPENSION bound to the router/olt/onu device types. The plant's playbooks
are one legacy `steps` simulator step each, which normalizes to one
CONFIGURATION entry per device (no probe); ACTIVATION runs in build order
(core bottom-up, CPE last, doc 42 §5). Engine v2 phases, rollback, revert and
secrets: tests/test_provisioning_run_v2.py.
"""

import uuid
from datetime import timedelta

import sqlalchemy as sa

from database_utils.models import ProvisioningRun
from database_utils.models.isp import (
    ProvisioningJob,
    ProvisioningJobStatus,
    PURPOSE_ACTIVATION,
)
from database_utils.utils import provisioning_runs
from database_utils.utils.provisioning_runs import (
    advance_run,
    create_or_get_run,
    create_run,
    find_in_flight_run,
    repair_stranded_runs,
    run_idempotency_key,
)
from database_utils.utils.timezone_utils import now_gt



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
    assert jobs[0].inventory_item_id == plant.olt.id


def test_the_plan_is_build_order_and_excludes_passives(db, plant):
    run = create_run(db, plant.service, PURPOSE_ACTIVATION)
    assert [p["category_key"] for p in run.plan] == ["olt", "router", "onu"]
    assert [p["category_key"] for p in run.path] == [
        "ONU", "SPLITTER", "SPLITTER", "OLT", "ROUTER"]


def test_the_path_snapshot_keeps_the_passives_visible(db, plant):
    run = create_run(db, plant.service, PURPOSE_ACTIVATION)
    passives = [p for p in run.path if p["is_passive"]]
    assert len(passives) == 2, "the run detail must show what was skipped"


def test_each_child_gets_shared_plus_its_own_device_frame(db, plant):
    run = create_run(db, plant.service, PURPOSE_ACTIVATION)
    first = _children(db, run)[0]
    assert first.variables["device.category"] == "olt"
    assert first.variables["path.olt.serial"] == "OLT-1"
    assert first.variables["cpe.serial"] == "ONT-1"


def test_children_are_created_without_device_lock(db, plant):
    """Producers never lock: only the worker's claim writes device_lock_key."""
    run = create_run(db, plant.service, PURPOSE_ACTIVATION)
    first = _children(db, run)[0]
    assert first.device_lock_key is None
    first.status = ProvisioningJobStatus.SUCCEEDED
    assert advance_run(db, first).device_lock_key is None


def test_create_run_on_busy_device_succeeds(db, plant):
    """The 2026-10-06 incident: a probe (or a parked TR-069 job) holding the
    device must not make opening or advancing a run fail. SQLite builds
    uq_provisioning_job_device_lock without its predicate, so a keyed child
    would collide here too."""
    pb = plant._playbook("core-connectivity")
    for item in (plant.cpe, plant.olt):
        db.add(ProvisioningJob(
            id=uuid.uuid4(), company_id=plant.company_id, playbook_id=pb.id,
            inventory_item_id=item.id, variables={},
            status=ProvisioningJobStatus.PENDING_INFORM,
            device_lock_key=f"{plant.company_id}:item:{item.id}"))
    db.flush()
    run = create_run(db, plant.service, PURPOSE_ACTIVATION)
    first = _children(db, run)[0]
    assert first.status == ProvisioningJobStatus.QUEUED
    assert first.device_lock_key is None
    first.status = ProvisioningJobStatus.SUCCEEDED
    nxt = advance_run(db, first)
    assert nxt.inventory_item_id == plant.core.id and nxt.device_lock_key is None


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
    assert nxt.inventory_item_id == plant.core.id
    assert nxt.variables["device.serial"] == "CORE-1"


def test_advance_stops_the_run_on_failure(db, plant):
    run = create_run(db, plant.service, PURPOSE_ACTIVATION)
    first = _children(db, run)[0]
    first.status = ProvisioningJobStatus.FAILED
    assert advance_run(db, first) is None
    assert run.status == ProvisioningJobStatus.FAILED
    assert run.finished_at is not None
    assert run.error_code == "CONFIGURATION_FAILED", "nothing ran, nothing to roll back"
    assert len(_children(db, run)) == 1, "the router must never be touched"


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


def test_advance_run_noop_on_terminal_run(db, plant):
    """A late settle (reaped executor, cancel race) never resurrects a run."""
    run = create_run(db, plant.service, PURPOSE_ACTIVATION)
    first = _children(db, run)[0]
    run.status = ProvisioningJobStatus.CANCELLED
    db.flush()
    first.status = ProvisioningJobStatus.SUCCEEDED
    assert advance_run(db, first) is None
    assert run.status == ProvisioningJobStatus.CANCELLED
    assert len(_children(db, run)) == 1


def test_advance_run_noop_while_job_in_flight(db, plant):
    run = create_run(db, plant.service, PURPOSE_ACTIVATION)
    first = _children(db, run)[0]
    first.status = ProvisioningJobStatus.RUNNING
    assert advance_run(db, first) is None
    assert run.status == ProvisioningJobStatus.QUEUED
    assert len(_children(db, run)) == 1


def test_double_advance_is_noop(db, plant):
    run = create_run(db, plant.service, PURPOSE_ACTIVATION)
    first = _children(db, run)[0]
    first.status = ProvisioningJobStatus.SUCCEEDED
    assert advance_run(db, first).run_position == 1
    assert advance_run(db, first) is None, "a duplicate settle queues nothing"
    assert [j.run_position for j in _children(db, run)] == [0, 1]
    assert run.status == ProvisioningJobStatus.RUNNING


def test_stale_child_advance_is_noop(db, plant):
    """An older child's late terminal write cannot stop a run that moved on."""
    run = create_run(db, plant.service, PURPOSE_ACTIVATION)
    first = _children(db, run)[0]
    first.status = ProvisioningJobStatus.SUCCEEDED
    advance_run(db, first)
    first.status = ProvisioningJobStatus.FAILED
    assert advance_run(db, first) is None
    assert run.status == ProvisioningJobStatus.RUNNING
    assert run.finished_at is None


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


def test_run_idempotency_key_shapes():
    sid = uuid.uuid4()
    assert run_idempotency_key(sid, "ACTIVATION", False) == f"path-provision-{sid}-activation"
    assert run_idempotency_key(sid, "SUSPENSION", False) == f"path-provision-{sid}-suspension"
    assert run_idempotency_key(sid, "DEPROVISION", False) == f"deprovision-{sid}"
    assert run_idempotency_key(sid, "ACTIVATION", True) == f"path-provision-{sid}-activation-dry"
    assert run_idempotency_key(sid, "DEPROVISION", True) == f"deprovision-{sid}-dry"


def test_create_or_get_run_dedupes(db, plant):
    run, created = create_or_get_run(db, plant.service, PURPOSE_ACTIVATION)
    assert created is True
    assert run.idempotency_key == run_idempotency_key(plant.service.id, PURPOSE_ACTIVATION)
    again, created = create_or_get_run(db, plant.service, PURPOSE_ACTIVATION)
    assert created is False and again.id == run.id
    dry, created = create_or_get_run(db, plant.service, PURPOSE_ACTIVATION, dry_run=True)
    assert created is True and dry.id != run.id


def test_create_or_get_run_race_loser_gets_the_winner(db, plant, monkeypatch):
    """The pre-check misses a concurrent winner; the unique index catches it,
    only the savepoint rolls back and the session stays usable."""
    winner = create_run(db, plant.service, PURPOSE_ACTIVATION)
    real = provisioning_runs.find_in_flight_run
    calls = []

    def _blind_once(*a, **kw):
        calls.append(1)
        return None if len(calls) == 1 else real(*a, **kw)

    monkeypatch.setattr(provisioning_runs, "find_in_flight_run", _blind_once)
    run, created = create_or_get_run(db, plant.service, PURPOSE_ACTIVATION)
    assert created is False and run.id == winner.id
    db.commit()
    assert db.execute(sa.select(sa.func.count()).select_from(ProvisioningRun)).scalar() == 1
    assert len(_children(db, winner)) == 1


def test_workflow_enqueue_dedupes_on_the_shared_key(db, plant):
    from database_utils.utils.workflow_engine import _execute_enqueue_provisioning_path
    cfg = {"client_service_id": str(plant.service.id), "purpose": "ACTIVATION"}
    first = _execute_enqueue_provisioning_path(db, cfg, plant.company_id)
    assert first["enqueued"] is True
    second = _execute_enqueue_provisioning_path(db, cfg, plant.company_id)
    assert second["deduped"] is True and second["run_id"] == first["run_id"]


def _age(db, run):
    run.updated_at = now_gt() - timedelta(minutes=5)
    db.flush()


def test_repair_stranded_runs(db, plant):
    from database_utils.models.isp import PURPOSE_SUSPENSION
    ok = create_run(db, plant.service, PURPOSE_ACTIVATION)
    bad = create_run(db, plant.service, PURPOSE_SUSPENSION)
    live = create_run(db, plant.service, PURPOSE_ACTIVATION, dry_run=True)
    young = create_run(db, plant.service, PURPOSE_SUSPENSION, dry_run=True)
    # Terminal children whose advance never committed (the stranded state).
    _children(db, ok)[0].status = ProvisioningJobStatus.SUCCEEDED
    _children(db, bad)[0].status = ProvisioningJobStatus.FAILED
    _children(db, young)[0].status = ProvisioningJobStatus.SUCCEEDED
    for r in (ok, bad, live):
        _age(db, r)

    assert repair_stranded_runs(db) == 2

    assert ok.status == ProvisioningJobStatus.RUNNING
    assert [j.run_position for j in _children(db, ok)] == [0, 1]
    assert bad.status == ProvisioningJobStatus.FAILED and bad.finished_at is not None
    assert len(_children(db, bad)) == 1
    assert live.status == ProvisioningJobStatus.QUEUED, "an in-flight child is left alone"
    assert len(_children(db, live)) == 1
    assert young.status == ProvisioningJobStatus.QUEUED, "inside the grace window"
    assert len(_children(db, young)) == 1
    # Idempotent: the repaired run now has a live child.
    assert repair_stranded_runs(db) == 0


def test_repair_requeues_a_run_with_no_child(db, plant):
    run = create_run(db, plant.service, PURPOSE_ACTIVATION)
    db.delete(_children(db, run)[0])
    _age(db, run)
    assert repair_stranded_runs(db) == 1
    assert [j.run_position for j in _children(db, run)] == [0]


def test_repair_closes_a_run_stranded_past_the_max_age(db, plant):
    """A run stranded before the backstop existed (e.g. by the 2026-10-06
    incident) must not wake up weeks later and configure the next device from
    a stale plan: it is closed FAILED instead of advanced."""
    run = create_run(db, plant.service, PURPOSE_ACTIVATION)
    _children(db, run)[0].status = ProvisioningJobStatus.SUCCEEDED
    run.updated_at = now_gt() - timedelta(days=21)
    db.flush()

    assert repair_stranded_runs(db) == 1
    assert [j.run_position for j in _children(db, run)] == [0]
    assert run.status == ProvisioningJobStatus.FAILED and run.finished_at is not None


def test_repair_still_advances_a_long_run_whose_child_just_finished(db, plant):
    """Age is measured from the last child's finish, not the run row: a run row
    is not touched while its child sits in PENDING_INFORM for hours."""
    run = create_run(db, plant.service, PURPOSE_ACTIVATION)
    child = _children(db, run)[0]
    child.status = ProvisioningJobStatus.SUCCEEDED
    child.finished_at = now_gt() - timedelta(minutes=5)
    run.updated_at = now_gt() - timedelta(hours=3)
    db.flush()

    assert repair_stranded_runs(db) == 1
    assert [j.run_position for j in _children(db, run)] == [0, 1]
