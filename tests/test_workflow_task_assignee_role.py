"""CREATE_TASK only assigns TECHNICIAN-role users (bug-fix task-assignee-role).

Tasks are for technicians. backend-erp rejects a non-technician assignee with
422 ASSIGNEE_NOT_TECHNICIAN; an automation cannot answer a 422, so the engine
creates the task unassigned (PENDING, for the dispatcher) and records the
rejected ids + a warning in the step result instead of failing the run.
"""
import uuid

from database_utils.models.auth import Role, User
from database_utils.models.crm import Client, Task
from database_utils.utils.workflow_engine import _execute_create_task


def _user(db, company_id, role=None):
    user = User(id=uuid.uuid4(), name="U", email=f"{uuid.uuid4()}@x.gt", age=30,
                password_hash="x", company_id=company_id)
    if role:
        user.roles = [Role(id=uuid.uuid4(), name=role)]
    db.add(user)
    db.flush()
    return user


def _task(db, result):
    return db.query(Task).filter(Task.id == uuid.UUID(result["resource_id"])).one()


def test_fixed_assigns_only_technicians(db, plant):
    tech = _user(db, plant.company_id, "TECHNICIAN")
    office = _user(db, plant.company_id, "ADMIN")
    result = _execute_create_task(
        db, {"name": "a", "assignee_source": "fixed",
             "assignee_ids": [str(tech.id), str(office.id)]},
        {}, plant.company_id,
    )
    assert result["assignee_ids"] == [str(tech.id)]
    assert result["skipped_assignee_ids"] == [str(office.id)]
    assert "ASSIGNEE_NOT_TECHNICIAN" in result["warning"]
    assert [u.id for u in _task(db, result).assignees] == [tech.id]


def test_fixed_non_technician_only_creates_unassigned_pending(db, plant):
    office = _user(db, plant.company_id)  # no role at all
    result = _execute_create_task(
        db, {"name": "a", "assignee_source": "fixed", "assignee_ids": [str(office.id)]},
        {}, plant.company_id,
    )
    task = _task(db, result)
    assert task.assignees == []
    assert task.status == "PENDING"
    assert result["skipped_assignee_ids"] == [str(office.id)]


def test_client_technician_not_technician_falls_back_to_fixed(db, plant):
    office = _user(db, plant.company_id, "SALES")
    tech = _user(db, plant.company_id, "TECHNICIAN")
    client = Client(id=uuid.uuid4(), name="C", company_id=plant.company_id,
                    assigned_technician_id=office.id)
    db.add(client)
    db.flush()
    result = _execute_create_task(
        db, {"name": "a", "assignee_source": "client_technician",
             "client_id": str(client.id), "assignee_ids": [str(tech.id)]},
        {}, plant.company_id,
    )
    assert result["assignee_ids"] == [str(tech.id)]
    assert result["skipped_assignee_ids"] == [str(office.id)]
    assert _task(db, result).status == "ASSIGNED"


def test_all_technicians_leaves_no_warning(db, plant):
    tech = _user(db, plant.company_id, "TECHNICIAN")
    result = _execute_create_task(
        db, {"name": "a", "assignee_source": "fixed", "assignee_ids": [str(tech.id)]},
        {}, plant.company_id,
    )
    assert "warning" not in result and "skipped_assignee_ids" not in result


def test_other_company_technician_is_skipped(db, plant):
    tech = _user(db, uuid.uuid4(), "TECHNICIAN")
    result = _execute_create_task(
        db, {"name": "a", "assignee_source": "fixed", "assignee_ids": [str(tech.id)]},
        {}, plant.company_id,
    )
    assert result["assignee_ids"] == []
    assert result["skipped_assignee_ids"] == [str(tech.id)]
