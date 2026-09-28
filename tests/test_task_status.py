"""Fixed task status guardrails (revision `ts1_task_status`).

The status set lives in four places that cannot import each other: the model
constant and CHECK, the hand-written revision, the Pydantic Literal and the
seed templates. These tests pin them together, and pin the two behaviours the
dispatch ETL relies on: PENDING/ASSIGNED follow the technician assignment, and
an automation-created task gets a status even when its workflow was installed
with a legacy board column.
"""
import importlib.util
import os
import uuid

import pytest
from pydantic import ValidationError
from sqlalchemy import CheckConstraint

from database_utils.models.auth import User
from database_utils.models.crm import (
    TASK_STATUSES,
    _TASK_STATUS_CHECK,
    Task,
    TaskState,
)
from database_utils.schemas.task import TaskCreate, TaskMove, TaskOut
from database_utils.utils.task_status import STATUS_FROM_STATE_KIND, derive_status
from database_utils.utils.workflow_engine import (
    _execute_create_task,
    _matches_field_conditions,
)

_HERE = os.path.dirname(__file__)
_VERSIONS = os.path.join(_HERE, "..", "alembic", "versions")
_ISP_SEED_PATH = os.path.join(_HERE, "..", "alembic", "seeds", "isp_seed.py")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _ts1():
    return _load("ts1_task_status", os.path.join(_VERSIONS, "ts1_task_status.py"))


# --- revision and model agree ---

def test_ts1_sits_on_tr1():
    ts1 = _ts1()
    assert ts1.revision == "ts1_task_status"
    assert ts1.down_revision == "tr1_transport_axis"
    assert len(ts1.revision) <= 32


def test_status_set_is_the_same_everywhere():
    ts1 = _ts1()
    assert ts1.TASK_STATUSES == TASK_STATUSES == ("PENDING", "ASSIGNED", "IN_PROGRESS", "DONE")
    assert ts1._TASK_STATUS_CHECK == _TASK_STATUS_CHECK
    for value in TASK_STATUSES:
        assert f"'{value}'" in _TASK_STATUS_CHECK
    assert ts1.STATUS_FROM_STATE_KIND == STATUS_FROM_STATE_KIND


def test_model_column_check_and_index():
    column = Task.__table__.columns["status"]
    assert column.nullable is False
    assert column.server_default.arg == "PENDING"
    checks = {c.name: str(c.sqltext) for c in Task.__table__.constraints if isinstance(c, CheckConstraint)}
    assert checks["ck_task_status"] == _TASK_STATUS_CHECK
    assert "ix_task_company_status" in {i.name for i in Task.__table__.indexes}
    assert Task.__table__.columns["task_state_id"].nullable is True


def test_every_state_kind_maps_to_a_status():
    ts1 = _ts1()
    assert set(ts1.STATUS_FROM_STATE_KIND) == {"ASSIGNED", "IN_PROGRESS", "DONE", "CANCELLED"}
    assert set(ts1.STATUS_FROM_STATE_KIND.values()) <= set(TASK_STATUSES)
    assert STATUS_FROM_STATE_KIND["CANCELLED"] == "DONE"


# --- schemas ---

def test_schemas_accept_the_four_values_and_reject_others():
    for value in TASK_STATUSES:
        assert TaskCreate(name="x", status=value).status == value
    assert TaskCreate(name="x").status is None
    assert TaskCreate(name="x").task_state_id is None
    with pytest.raises(ValidationError):
        TaskCreate(name="x", status="CANCELLED")
    with pytest.raises(ValidationError):
        TaskMove(status="TODO")
    assert TaskMove(status="DONE").position is None
    assert TaskOut.model_fields["status"].default == "PENDING"


# --- derive_status ---

@pytest.mark.parametrize("current,has_tech,expected", [
    (None, False, "PENDING"),
    (None, True, "ASSIGNED"),
    ("PENDING", True, "ASSIGNED"),
    ("ASSIGNED", False, "PENDING"),
    ("IN_PROGRESS", False, "IN_PROGRESS"),
    ("DONE", True, "DONE"),
])
def test_derive_status(current, has_tech, expected):
    assert derive_status(current, has_tech) == expected


def test_derive_status_rejects_unknown_values():
    with pytest.raises(ValueError):
        derive_status("CANCELLED", False)


# --- workflow engine ---

def _user(db, company_id):
    user = User(id=uuid.uuid4(), name="Tec", email=f"{uuid.uuid4()}@x.gt", age=30,
                password_hash="x", company_id=company_id)
    db.add(user)
    db.flush()
    return user


def _created(db, result):
    return db.query(Task).filter(Task.id == uuid.UUID(result["resource_id"])).one()


def test_create_task_without_state_is_pending_or_assigned(db, plant):
    pending = _execute_create_task(db, {"name": "a"}, {}, plant.company_id)
    assert _created(db, pending).status == "PENDING"
    assert pending["status"] == "PENDING"
    assert _created(db, pending).task_state_id is None

    tech = _user(db, plant.company_id)
    assigned = _execute_create_task(
        db, {"name": "b", "assignee_source": "fixed", "assignee_ids": [str(tech.id)]},
        {}, plant.company_id,
    )
    assert _created(db, assigned).status == "ASSIGNED"


def test_create_task_keeps_an_explicit_in_progress(db, plant):
    result = _execute_create_task(db, {"name": "a", "status": "IN_PROGRESS"}, {}, plant.company_id)
    assert _created(db, result).status == "IN_PROGRESS"
    with pytest.raises(ValueError, match="invalid status"):
        _execute_create_task(db, {"name": "a", "status": "CANCELLED"}, {}, plant.company_id)


def test_create_task_maps_a_legacy_task_state_id(db, plant):
    done = TaskState(id=uuid.uuid4(), company_id=plant.company_id, name="Hechas", kind="DONE")
    db.add(done)
    db.flush()
    result = _execute_create_task(db, {"name": "a", "task_state_id": str(done.id)}, {}, plant.company_id)
    task = _created(db, result)
    assert task.status == "DONE"
    assert task.task_state_id == done.id


def test_create_task_ignores_an_unresolved_state_param(db, plant):
    result = _execute_create_task(
        db, {"name": "a", "task_state_id": "{{param:install_state_id}}"}, {}, plant.company_id,
    )
    assert _created(db, result).status == "PENDING"


def test_status_changed_to_trigger_fires_once():
    conditions = {"field": "status", "operator": "changed_to", "value": "DONE"}
    assert _matches_field_conditions(conditions, {"status": "IN_PROGRESS"}, {"status": "DONE"})
    assert not _matches_field_conditions(conditions, {"status": "DONE"}, {"status": "DONE"})


# --- seed templates ---

def _templates():
    seed = _load("isp_seed_ts1", _ISP_SEED_PATH)
    return seed, {t["key"]: t for t in seed.WORKFLOW_TEMPLATES}


def test_templates_no_longer_ask_for_a_board_column():
    _, templates = _templates()
    for key in ("new-installation", "installation-provisioning"):
        params = templates[key]["definition"]["parameters"]
        assert not [p for p in params if p["type"] == "task_state"], key


def test_installation_provisioning_fires_on_done():
    _, templates = _templates()
    trigger = templates["installation-provisioning"]["definition"]["triggers"][0]
    assert trigger["field_conditions"] == {"field": "status", "operator": "changed_to", "value": "DONE"}


def test_new_installation_task_has_no_board_column():
    _, templates = _templates()
    step = next(s for s in templates["new-installation"]["definition"]["steps"]
                if s["action_type"] == "CREATE_TASK")
    assert "task_state_id" not in step["action_config"]


def test_templates_wait_for_the_status_column():
    seed, _ = _templates()
    for key in ("new-installation", "installation-provisioning"):
        assert ("task", "status") in seed.TEMPLATE_REQUIRED_COLUMNS[key]


# --- workflow rewrite helpers ---

def test_rewrite_maps_known_states_and_leaves_unknown_ones():
    ts1 = _ts1()
    mapping = {"s-done": "DONE"}
    config = {"name": "x", "task_state_id": "s-done", "data": {"task_state_id": "s-done"}}
    assert ts1._rewrite_config(config, mapping, "task_state_id", "status")
    assert config == {"name": "x", "status": "DONE", "data": {"status": "DONE"}}
    untouched = {"task_state_id": "other"}
    assert not ts1._rewrite_config(untouched, mapping, "task_state_id", "status")
    assert untouched == {"task_state_id": "other"}
