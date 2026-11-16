"""nm1: client_service.network_managed + office-task columns on task (doc 71 §9)."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from _mi_helpers import load
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from database_utils.models.crm import OFFICE_ACTIONS, Task, TaskJobKind
from database_utils.models.isp import ClientService, ProvisioningJobStatus, ProvisioningRun
from database_utils.schemas import client_service as cs_schemas
from database_utils.schemas import task as task_schemas
from database_utils.schemas.client_service import ClientServiceOut, OfficeTaskRef, ServiceNetworkRun
from database_utils.schemas.task import TaskOut
from database_utils.utils.network_managed import MANAGED_EVIDENCE_PURPOSES, backfill_value

NM1 = "versions/nm1_network_managed.py"


def _nm1():
    return load(NM1, "nm1_network_managed")


def test_nm1_revision_chain():
    m = _nm1()
    assert m.revision == "nm1_network_managed" and m.down_revision == "rf1_referral_programs"
    assert len(m.revision) <= 32


def test_nm1_is_additive():
    body = open(_nm1().__file__).read()
    upgrade = body.split("def upgrade")[1].split("def downgrade")[0]
    for forbidden in ("drop_", "DROP COLUMN", "DROP TABLE"):
        assert forbidden not in upgrade
    assert "SET lock_timeout = '5s'" in upgrade


def test_columns_and_check_exist():
    assert "network_managed" in ClientService.__table__.c
    assert not ClientService.__table__.c.network_managed.nullable
    assert {"office_action", "provisioning_run_id", "completed_by", "completion_note"} <= set(
        Task.__table__.c.keys())
    assert "ck_task_office_action" in {c.name for c in Task.__table__.constraints}
    assert OFFICE_ACTIONS == ("NETWORK_CUT", "NETWORK_RECONNECT", "NETWORK_REMOVE",
                              "NETWORK_SWAP", "RUN_BY_HAND")
    assert MANAGED_EVIDENCE_PURPOSES == ("ACTIVATION", "CPE_SWAP", "SUSPENSION",
                                         "REACTIVATION", "BOOST")


def _task(**kw):
    return Task(id=uuid.uuid4(), company_id=uuid.uuid4(), name="t", **kw)


def test_check_rejects_office_action_with_job_kind(db):
    db.add(_task(office_action="NETWORK_CUT"))
    db.add(_task(job_kind=TaskJobKind.INSTALL))
    db.flush()
    with pytest.raises(IntegrityError):
        with db.begin_nested():
            db.add(_task(office_action="NETWORK_CUT", job_kind=TaskJobKind.INSTALL))
            db.flush()
    with pytest.raises(IntegrityError):
        with db.begin_nested():
            db.add(_task(office_action="NOPE"))
            db.flush()


T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)

# (purpose, status, dry_run) per run, in creation order.
OK = ProvisioningJobStatus.SUCCEEDED
CASES = {
    "activation_ok": ([("ACTIVATION", OK, False)], True),
    "activation_then_deprovision": ([("ACTIVATION", OK, False), ("DEPROVISION", OK, False)], False),
    "deprovision_then_activation": ([("DEPROVISION", OK, False), ("ACTIVATION", OK, False)], True),
    "activation_reverted": ([("ACTIVATION", ProvisioningJobStatus.ROLLED_BACK, False)], False),
    "dry_activation": ([("ACTIVATION", OK, True)], False),
    "dry_deprovision_keeps": ([("ACTIVATION", OK, False), ("DEPROVISION", OK, True)], True),
    "failed_deprovision_keeps": ([("ACTIVATION", OK, False),
                                  ("DEPROVISION", ProvisioningJobStatus.FAILED, False)], True),
    "cpe_swap_ok": ([("CPE_SWAP", OK, False)], True),
    "suspension_only": ([("SUSPENSION", OK, False)], True),
    "custom_purpose_only": ([("WIFI_PASSWORD", OK, False)], False),
    "no_runs": ([], False),
}


def _service(db, company_id):
    cs = ClientService(id=uuid.uuid4(), company_id=company_id, client_id=uuid.uuid4(),
                       service_plan_id=uuid.uuid4())
    db.add(cs)
    db.flush()
    return cs


def _runs(db, cs, runs):
    for i, (purpose, status, dry) in enumerate(runs):
        db.add(ProvisioningRun(
            id=uuid.uuid4(), company_id=cs.company_id, client_service_id=cs.id,
            purpose=purpose, status=status, dry_run=dry, path=[], plan=[], frames={},
            created_at=T0 + timedelta(hours=i),
        ))
    db.flush()


@pytest.mark.parametrize("case", sorted(CASES))
def test_backfill_cases(db, case):
    runs, expected = CASES[case]
    cs = _service(db, uuid.uuid4())
    _runs(db, cs, runs)
    assert backfill_value(db, cs.id) is expected
    db.execute(text(_nm1().BACKFILL_SQL))
    db.expire_all()
    assert db.get(ClientService, cs.id).network_managed is expected


def test_backfill_two_tenants(db):
    a = _service(db, uuid.uuid4())
    b = _service(db, uuid.uuid4())
    _runs(db, a, [("ACTIVATION", OK, False)])
    db.execute(text(_nm1().BACKFILL_SQL))
    db.expire_all()
    assert db.get(ClientService, a.id).network_managed is True
    assert db.get(ClientService, b.id).network_managed is False
    assert backfill_value(db, a.id) is True and backfill_value(db, b.id) is False


def test_schemas():
    out = ClientServiceOut.model_fields
    assert out["network_managed"].default is False
    assert out["network_run"].default is None
    assert out["open_office_tasks"].default_factory is not None or out["open_office_tasks"].default == []
    assert set(ServiceNetworkRun.model_fields) == {
        "id", "purpose", "status", "error_code", "outcome", "created_at", "finished_at", "unresolved"}
    assert ServiceNetworkRun.model_fields["unresolved"].default is False
    assert set(OfficeTaskRef.model_fields) == {"id", "office_action", "name", "created_at"}
    assert {"office_action", "provisioning_run_id", "completed_by", "completed_by_name",
            "completion_note"} <= set(TaskOut.model_fields)
    for mod in (cs_schemas, task_schemas):
        for name in dir(mod):
            if ("Create" in name or "Update" in name) and hasattr(getattr(mod, name), "model_fields"):
                fields = getattr(mod, name).model_fields
                assert not {"office_action", "network_managed", "completion_note",
                            "completed_by", "provisioning_run_id"} & set(fields), name


def test_out_reads_orm_defaults(db):
    cs = _service(db, uuid.uuid4())
    assert cs.network_managed is False
