"""wf1_workflow_engine_v2 on SQLite (doc 63-workflow-engine §2): the new
tables build with create_all, the partial uniques hold (sqlite_where), one
flush of workflow + v1 + active_version_id needs no second flush, the legacy
engine never matches a v2 workflow, and the revision sits on up1."""
import uuid

import pytest
from _mi_helpers import load
from conftest import CO_A
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from database_utils.models import (
    AutomationTemplate,
    ExecutionStatus,
    ServiceBoost,
    ServiceBoostStatus,
    TriggerEventType,
    Workflow,
    WorkflowEvent,
    WorkflowExecution,
    WorkflowStepExecution,
    WorkflowTrigger,
    WorkflowVersion,
    WorkflowVersionStatus,
)
from database_utils.schemas.workflow_v2 import WorkflowDefinition
from database_utils.utils.timezone_utils import now_gt
from database_utils.utils.workflow_engine import find_matching_workflows

DEF = {"schema": 1, "trigger": {"type": "event", "key": "payment.confirmed", "config": {}},
       "start": "a", "nodes": [{"key": "a", "action": "flow.end"}], "edges": []}


def _workflow(db, **kw):
    wf = Workflow(id=uuid.uuid4(), name="wf", company_id=CO_A, **kw)
    db.add(wf)
    return wf


def _version(wf, number=1, status=WorkflowVersionStatus.PUBLISHED):
    return WorkflowVersion(workflow=wf, company_id=CO_A, number=number, status=status,
                           definition=DEF, trigger_type="event", trigger_key="payment.confirmed")


def test_revision_chain():
    wf1 = load("versions/wf1_workflow_engine_v2.py", "wf1_workflow_engine_v2")
    assert wf1.revision == "wf1_workflow_engine_v2" and wf1.down_revision == "up1_usage_pricing"


def test_install_flush_sets_active_version_in_one_go(db):
    wf = _workflow(db, engine_version=2)
    v1 = _version(wf)
    wf.active_version = v1
    db.flush()  # post_update: no CircularDependencyError
    db.expire_all()
    got = db.get(Workflow, wf.id)
    assert got.active_version_id == v1.id and got.active_version.number == 1
    assert [v.number for v in got.versions] == [1]


def test_defaults(db):
    wf = _workflow(db)
    run = WorkflowExecution(workflow=wf, trigger_event={})
    db.add(run)
    db.flush()
    assert wf.engine_version == 1 and wf.active_version_id is None
    assert run.depth == 0 and run.is_test is False and run.definition is None
    step = WorkflowStepExecution(execution=run, node_key="a")
    db.add(step)
    db.flush()
    assert step.attempts == 0 and step.simulated is False


def test_one_draft_per_workflow(db):
    wf = _workflow(db, engine_version=2)
    db.add_all([_version(wf, 1, WorkflowVersionStatus.PUBLISHED), _version(wf, 2, WorkflowVersionStatus.DRAFT)])
    db.flush()
    db.add(_version(wf, 3, WorkflowVersionStatus.DRAFT))
    with pytest.raises(IntegrityError):
        db.flush()


def test_version_number_unique(db):
    wf = _workflow(db, engine_version=2)
    db.add_all([_version(wf, 1, WorkflowVersionStatus.SUPERSEDED), _version(wf, 1)])
    with pytest.raises(IntegrityError):
        db.flush()


def test_run_dedupe_unique_only_when_set(db):
    wf = _workflow(db, engine_version=2)
    db.add_all([WorkflowExecution(workflow=wf, trigger_event={}) for _ in range(2)])
    db.add(WorkflowExecution(workflow=wf, trigger_event={}, dedupe_key="ev:1"))
    db.flush()
    db.add(WorkflowExecution(workflow=wf, trigger_event={}, dedupe_key="ev:1"))
    with pytest.raises(IntegrityError):
        db.flush()


def test_step_row_unique_per_node(db):
    wf = _workflow(db, engine_version=2)
    run = WorkflowExecution(workflow=wf, trigger_event={}, status=ExecutionStatus.WAITING)
    db.add_all([run, WorkflowStepExecution(execution=run, node_key="a")])
    db.flush()
    db.add(WorkflowStepExecution(execution=run, node_key="a"))
    with pytest.raises(IntegrityError):
        db.flush()


def _boost(cs_id, status):
    return ServiceBoost(company_id=CO_A, client_service_id=cs_id, extra_mb=10, status=status,
                        starts_at=now_gt(), expires_at=now_gt())


def test_one_open_boost_per_service(db):
    cs_id = uuid.uuid4()
    db.add_all([_boost(cs_id, ServiceBoostStatus.ENDED), _boost(cs_id, ServiceBoostStatus.FAILED),
                _boost(cs_id, ServiceBoostStatus.ACTIVE)])
    db.flush()
    db.add(_boost(cs_id, ServiceBoostStatus.END_FAILED))
    with pytest.raises(IntegrityError):
        db.flush()


def test_outbox_and_template_tables(db):
    ev = WorkflowEvent(company_id=CO_A, name="task.created", payload={"task_id": "x"})
    db.add(ev)
    db.add(AutomationTemplate(key="payment_promo", version=1, name={"es": "a", "en": "a"},
                              category="billing", definition=DEF, checksum="0" * 64))
    db.flush()
    assert ev.id and ev.depth == 0 and ev.attempts == 0 and ev.processed_at is None
    db.add(AutomationTemplate(key="payment_promo", version=1, name={"es": "b", "en": "b"},
                              category="billing", definition=DEF, checksum="1" * 64))
    with pytest.raises(IntegrityError):
        db.flush()


def test_legacy_engine_ignores_v2_workflows(db):
    for version in (1, 2):
        wf = _workflow(db, engine_version=version, is_active=True)
        db.add(WorkflowTrigger(workflow=wf, resource_type="order", event_type=TriggerEventType.CREATED))
    db.flush()
    matched = find_matching_workflows(db, CO_A, "order", "CREATED", None, {})
    assert [w.engine_version for w in matched] == [1]


def test_definition_schema_round_trip_and_structure():
    d = WorkflowDefinition.model_validate({**DEF, "edges": [{"from": "a", "to": "b", "branch": "yes"}]})
    dumped = d.model_dump(by_alias=True)
    assert dumped["schema"] == 1 and dumped["edges"][0]["from"] == "a" and dumped["edges"][0]["on"] == "completed"
    for bad in ({**DEF, "schema": 2}, {**DEF, "nodes": [{"key": "Bad Key", "action": "x"}]},
                {**DEF, "extra": 1}, {**DEF, "trigger": {"type": "cron", "key": "x"}}):
        with pytest.raises(ValidationError):
            WorkflowDefinition.model_validate(bad)


def test_workflows_permissions_are_seeded():
    rbac = load("seeds/rbac_seed.py", "rbac_seed_wf1_test")
    names = {p["name"]: p["action"] for p in rbac.PERMISSIONS_DATA if p["resource"] == "workflows"}
    assert names == {"workflows.read": "read", "workflows.create": "create",
                     "workflows.update": "update", "workflows.delete": "delete"}
