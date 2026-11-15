"""doc 70 §5.4 (70-A1, A21/A22 models-utils half): only a staff run or a
self-managed tenant's dry run clears «Requiere simulación», and
`unsimulated_counts` finds the bound playbooks still waiting for one."""
import ast
import pathlib
import uuid

import sqlalchemy as sa

from database_utils.models.auth import Company
from database_utils.models.isp import PURPOSE_ACTIVATION, ProvisioningJob
from database_utils.models.isp import ProvisioningJobStatus as S
from database_utils.utils import provisioning_gates
from database_utils.utils.provisioning_gates import gate_failure, may_stamp_dry_run, unsimulated_counts
from database_utils.utils.provisioning_runs import advance_run, create_run
from database_utils.utils.timezone_utils import now_gt

from _managed_helpers import _tier

APPROVED = {"status": "APPROVED"}


def _company(db, plant, management=None):
    co = Company(id=plant.company_id, name="Tenant", tier_id=_tier(db).id,
                 playbook_management=management)
    db.add(co)
    db.flush()
    return co


def _undry(plant, key="olt-activation"):
    pb = plant.playbooks[key]
    pb.version, pb.last_dry_run_version = 2, 1
    plant.db.flush()
    return pb


def _finish_success(db, run, before_last=None):
    """Succeed every child, the way the executor writes job.log. Calls
    `before_last()` just before the run closes (the last child settles)."""
    while True:
        jobs = db.execute(sa.select(ProvisioningJob).where(ProvisioningJob.run_id == run.id)
                          .order_by(ProvisioningJob.run_position)).scalars().all()
        job = next((j for j in jobs if j.status == S.QUEUED), None)
        if job is None:
            return
        if before_last and job.run_position == len(run.plan) - 1:
            before_last()
        job.status, job.finished_at = S.SUCCEEDED, now_gt()
        job.log = {"steps": [{"name": s["name"], "status": "SUCCEEDED"}
                             for s in run.plan[job.run_position]["steps"]]}
        db.flush()
        advance_run(db, job)


def test_may_stamp_dry_run():
    class Co:
        playbook_management = None
    assert may_stamp_dry_run(Co(), staff_actor=True) is True
    assert may_stamp_dry_run(None, staff_actor=True) is True
    assert may_stamp_dry_run(Co(), staff_actor=False) is False
    assert may_stamp_dry_run(None, staff_actor=False) is False
    Co.playbook_management = APPROVED
    assert may_stamp_dry_run(Co(), staff_actor=False) is True


def test_managed_tenant_dry_run_never_stamps(db, plant):
    _company(db, plant)
    pb = _undry(plant)
    run = create_run(db, plant.service, dry_run=True)
    _finish_success(db, run)
    assert run.status == S.SUCCEEDED
    assert pb.last_dry_run_version == 1
    assert gate_failure(db, plant.company_id, pb, None, dry_run=False)["code"] == "DRY_RUN_REQUIRED"


def test_staff_dry_run_stamps(db, plant):
    _company(db, plant)
    pb = _undry(plant)
    run = create_run(db, plant.service, dry_run=True, staff_actor=True)
    _finish_success(db, run)
    assert run.staff_actor is True and pb.last_dry_run_version == pb.version
    assert gate_failure(db, plant.company_id, pb, None, dry_run=False) is None


def test_self_managed_dry_run_stamps(db, plant):
    _company(db, plant, APPROVED)
    pb = _undry(plant)
    _finish_success(db, create_run(db, plant.service, dry_run=True))
    assert pb.last_dry_run_version == pb.version


def test_revoked_mid_run_does_not_stamp(db, plant):
    co = _company(db, plant, APPROVED)
    pb = _undry(plant)
    run = create_run(db, plant.service, dry_run=True)

    def revoke():
        co.playbook_management = {"status": "REVOKED"}
        db.flush()

    _finish_success(db, run, before_last=revoke)
    assert run.status == S.SUCCEEDED
    assert pb.last_dry_run_version == 1


def test_create_run_defaults_staff_actor_false(db, plant):
    run = create_run(db, plant.service, dry_run=True)
    db.flush()
    db.expire(run)
    assert run.staff_actor is False


def test_unsimulated_counts(db, plant):
    co = plant.company_id
    assert unsimulated_counts(db, [co]) == {}
    assert unsimulated_counts(db, []) == {}
    # unbound and unstamped -> not counted
    plant._playbook("loose")
    plant.playbooks["loose"].version = 5
    # system-named, bound and unstamped -> not counted
    sys_pb = _undry(plant, "router-activation")
    sys_pb.name = "cpe_reboot"
    # bound on a device type AND as a node override, unstamped -> counted once
    pb = _undry(plant)
    plant.bind_node(plant.olt, "SUSPENSION", pb)
    db.flush()
    assert unsimulated_counts(db, [co]) == {co: 1}
    # another company's rows only count under its own id
    assert unsimulated_counts(db, [uuid.uuid4()]) == {}
    # a NULL last_dry_run_version is distinct from any version
    plant.playbooks["onu-activation"].last_dry_run_version = None
    db.flush()
    assert unsimulated_counts(db, [co]) == {co: 2}
    # stamped -> not counted
    pb.last_dry_run_version = pb.version
    plant.playbooks["onu-activation"].last_dry_run_version = 1
    db.flush()
    assert unsimulated_counts(db, [co]) == {}


def test_provisioning_gates_stays_fastapi_free():
    tree = ast.parse(pathlib.Path(provisioning_gates.__file__).read_text())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
            names.update(f"{node.module}.{a.name}" for a in node.names)
    banned = ("fastapi", "database_utils.dependencies", "database_utils.utils.permission_utils")
    assert not [n for n in names if any(n == b or n.startswith(b + ".") for b in banned)]
