"""CREATE_ENTITY task is capped per client per day (security audit #116,
template path; same number as the bot's per-sender cap)."""
import uuid
from datetime import timedelta

import pytest
from conftest import CO_A, CO_B

from database_utils.models import Client, Task
from database_utils.utils.timezone_utils import now_gt
from database_utils.utils.workflow_engine import (
    WORKFLOW_TASKS_PER_CLIENT_PER_DAY,
    _execute_create_entity,
)


def _client(db, co=CO_A):
    c = Client(company_id=co, name="c")
    db.add(c)
    db.flush()
    return c


def _create(db, client_id, **data):
    return _execute_create_entity(
        db, {"resource_type": "task", "data": {"name": "t", "client_id": client_id, **data}}, {}, CO_A)


def test_cap_constant():
    assert WORKFLOW_TASKS_PER_CLIENT_PER_DAY == 5


def test_sixth_workflow_task_for_client_in_24h_is_refused(db):
    c = _client(db)
    for _ in range(5):
        _create(db, c.id)
    with pytest.raises(ValueError, match="TASK_RATE_LIMITED"):
        _create(db, c.id)
    assert db.query(Task).filter(Task.client_id == c.id).count() == 5
    # another client is unaffected
    _create(db, _client(db).id)


def test_tasks_older_than_24h_do_not_count(db):
    c = _client(db)
    old = now_gt() - timedelta(hours=25)
    for _ in range(5):
        db.add(Task(company_id=CO_A, name="old", client_id=c.id, created_at=old))
    db.flush()
    _create(db, c.id)


def test_office_created_tasks_do_not_count_and_created_by_is_forced_null(db):
    c = _client(db)
    for _ in range(5):
        db.add(Task(company_id=CO_A, name="office", client_id=c.id, created_by=uuid.uuid4()))
    db.flush()
    out = _create(db, c.id)
    assert db.get(Task, uuid.UUID(out["resource_id"])).created_by is None
    # a template cannot dodge the count by claiming a user
    for _ in range(4):
        _create(db, c.id)
    with pytest.raises(ValueError, match="PROTECTED_FIELD|TASK_RATE_LIMITED"):
        _create(db, c.id, created_by=None)


def test_other_tenant_tasks_do_not_count(db):
    c = _client(db)
    for _ in range(5):
        db.add(Task(company_id=CO_B, name="x", client_id=c.id))
    db.flush()
    _create(db, c.id)


def test_task_without_client_is_not_capped(db):
    for _ in range(7):
        _execute_create_entity(db, {"resource_type": "task", "data": {"name": "t"}}, {}, CO_A)


def test_service_only_tasks_are_counted_against_the_service_client(db):
    from database_utils.models import ServicePlan
    from database_utils.models.isp import ClientService

    c = _client(db)
    plan = ServicePlan(company_id=CO_A, name="p-cap", download_mbps=10, upload_mbps=5)
    db.add(plan)
    db.flush()
    s = ClientService(company_id=CO_A, client_id=c.id, service_plan_id=plan.id)
    db.add(s)
    db.flush()

    def by_service():
        return _execute_create_entity(
            db, {"resource_type": "task", "data": {"name": "t", "client_service_id": str(s.id)}}, {}, CO_A)

    out = by_service()
    assert db.get(Task, uuid.UUID(out["resource_id"])).client_id == c.id
    for _ in range(4):
        by_service()
    with pytest.raises(ValueError, match="TASK_RATE_LIMITED"):
        by_service()
    with pytest.raises(ValueError, match="TASK_RATE_LIMITED"):
        _create(db, c.id)
