"""Dispatch routes guardrails (revision `dr1_task_route_sequence`)."""
import importlib.util
import os

from database_utils.models.crm import Task
from database_utils.schemas.task import TaskOut, TaskUpdate

_VERSIONS = os.path.join(os.path.dirname(__file__), "..", "alembic", "versions")


def _dr1():
    path = os.path.join(_VERSIONS, "dr1_task_route_sequence.py")
    spec = importlib.util.spec_from_file_location("dr1_task_route_sequence", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_dr1_sits_on_ts1():
    dr1 = _dr1()
    assert dr1.down_revision == "ts1_task_status"
    assert len(dr1.revision) <= 32


def test_route_sequence_is_a_nullable_integer():
    column = Task.__table__.columns["route_sequence"]
    assert column.nullable is True
    assert column.type.python_type is int


def test_route_sequence_is_read_only_through_the_task_schemas():
    assert TaskOut.model_fields["route_sequence"].default is None
    assert "route_sequence" not in TaskUpdate.model_fields


def test_route_sequence_has_no_index_of_its_own():
    indexed = {c.name for i in Task.__table__.indexes for c in i.columns}
    assert "route_sequence" not in indexed
