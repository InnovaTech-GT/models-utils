"""task.onu_auto_assigned guardrails (revision `oa1_task_onu_auto_assigned`, doc 45 §4.3).
The reservation backfill is Postgres-only: tests/pg/test_onu_auto_assigned_pg.py."""
from _mi_helpers import load

from database_utils.models.crm import Task
from database_utils.schemas.task import TaskCreate, TaskOut, TaskUpdate


def test_oa1_follows_tl1_in_the_program_chain():
    oa1 = load("versions/oa1_task_onu_auto_assigned.py", "oa1_task_onu_auto_assigned")
    assert oa1.revision == "oa1_task_onu_auto_assigned"
    # doc 42a §4: pc1 -> ta1 -> ri1 -> tl1 -> oa1 -> pe1 -> zt1
    assert oa1.down_revision == "tl1_task_location"
    assert len(oa1.revision) <= 32


def test_onu_auto_assigned_is_a_non_null_false_flag(db):
    column = Task.__table__.columns["onu_auto_assigned"]
    assert column.nullable is False
    assert column.type.python_type is bool
    assert "false" in str(column.server_default.arg).lower()


def test_onu_auto_assigned_is_output_only_in_the_shared_schemas():
    assert TaskOut.model_fields["onu_auto_assigned"].default is False
    # Only the server sets it (backend-erp services/onu_assignment.py).
    assert "onu_auto_assigned" not in TaskCreate.model_fields
    assert "onu_auto_assigned" not in TaskUpdate.model_fields
