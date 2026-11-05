"""utils/workflow_events + the ClientService status emit point (doc
63-workflow-engine §2.6)."""
import uuid
from datetime import date

import pytest
from conftest import CO_A

from database_utils.models import ClientService, ClientServiceStatus, WorkflowEvent
from database_utils.utils import workflow_events
from database_utils.utils.workflow_events import EVENT_NAMES, emit_business_event

S = ClientServiceStatus


def test_unknown_name_raises(db):
    with pytest.raises(ValueError):
        emit_business_event(db, CO_A, "order.created", {})
    assert db.query(WorkflowEvent).count() == 0


def test_emit_flushes_never_commits_and_serializes(db):
    rid = uuid.uuid4()
    d_tok, r_tok = workflow_events.current_depth.set(2), workflow_events.current_run_id.set(rid)
    try:
        ev = emit_business_event(db, CO_A, "task.created", {"task_id": rid, "on": date(2026, 11, 3),
                                                            "status": S.ACTIVE})
    finally:
        workflow_events.current_depth.reset(d_tok)
        workflow_events.current_run_id.reset(r_tok)
    assert ev.id is not None  # flushed
    assert (ev.depth, ev.source_run_id) == (2, rid)
    assert ev.payload == {"task_id": str(rid), "on": "2026-11-03", "status": "ACTIVE"}
    db.rollback()  # nothing was committed
    assert db.query(WorkflowEvent).count() == 0
    assert emit_business_event(db, CO_A, "task.created", {}).depth == 0


def _service(db, status):
    cs = ClientService(id=uuid.uuid4(), company_id=CO_A, client_id=uuid.uuid4(),
                       service_plan_id=uuid.uuid4(), status=status)
    db.add(cs)
    db.flush()
    return cs


def _names(db):
    return [e.name for e in db.query(WorkflowEvent).order_by(WorkflowEvent.id)]


@pytest.mark.parametrize("old,new,name", [
    (S.PENDING_INSTALL, S.ACTIVE, "client_service.activated"),
    (S.SUSPENDED, S.ACTIVE, "client_service.reactivated"),
    (S.ACTIVE, S.SUSPENDED, "client_service.suspended"),
    (S.ACTIVE, S.CANCELLED, "client_service.cancelled"),
    (S.PENDING_INSTALL, S.CANCELLED, "client_service.cancelled"),
])
def test_status_write_emits_the_lifecycle_event(db, old, new, name):
    cs = _service(db, old)
    assert _names(db) == []  # creating the row is not a transition
    cs.status = new.value if new is S.SUSPENDED else new  # enum or plain string
    db.flush()
    (ev,) = db.query(WorkflowEvent).all()
    assert ev.name == name and ev.company_id == CO_A
    assert ev.payload == {"client_service_id": str(cs.id), "client_id": str(cs.client_id),
                          "from_status": old.value, "to_status": new.value}


def test_same_status_emits_nothing(db):
    cs = _service(db, S.ACTIVE)
    cs.status = S.ACTIVE
    db.flush()
    db.expire(cs)
    cs.status = "ACTIVE"  # old value loaded from the DB
    db.flush()
    assert _names(db) == []


def test_detached_instance_emits_nothing(db):
    cs = ClientService(company_id=CO_A, status=S.PENDING_INSTALL)
    cs.status = S.ACTIVE
    assert _names(db) == []


def test_lifecycle_names_are_known_events():
    for n in ("activated", "suspended", "reactivated", "cancelled", "plan_changed"):
        assert f"client_service.{n}" in EVENT_NAMES
