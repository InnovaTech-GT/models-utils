"""Task reference point guardrails (revision `tl1_task_location`, doc 46 §4.2)."""
from _mi_helpers import load

from database_utils.models.crm import Task
from database_utils.schemas.task import TaskCreate, TaskOut, TaskUpdate


def test_tl1_follows_ri1_in_the_program_chain():
    tl1 = load("versions/tl1_task_location.py", "tl1_task_location")
    assert tl1.revision == "tl1_task_location"
    # doc 42a §4: pc1 -> ta1 -> ri1 -> tl1 -> oa1
    assert tl1.down_revision == "ri1_inventory_received_at"
    assert len(tl1.revision) <= 32


def test_task_location_columns_are_nullable_floats_without_index():
    for name in ("latitude", "longitude"):
        column = Task.__table__.columns[name]
        assert column.nullable is True
        assert column.type.python_type is float
    indexed = {c.name for i in Task.__table__.indexes for c in i.columns}
    assert not indexed & {"latitude", "longitude"}


def test_task_location_is_output_only_in_the_shared_schemas():
    for name in ("latitude", "longitude"):
        assert TaskOut.model_fields[name].default is None
        # Inputs live on backend-erp TaskCreateIn/TaskUpdateIn (doc 46 §4.3.3).
        assert name not in TaskCreate.model_fields
        assert name not in TaskUpdate.model_fields
