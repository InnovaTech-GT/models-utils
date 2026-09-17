"""Ordenes de Trabajo (tj1_task_job_kinds + tk2_task_links) guardrails.

Same problem as cl1: the schema exists in two places that cannot import each
other — the hand-written revisions and the SQLAlchemy models — and `alembic
check` is what compares them. A column, CHECK or index added to only one half
passes every other test in this suite and lands as CI drift instead.

Three things here are load-bearing beyond "the column exists":

  - `task_state.kind` is a CHECK-constrained STRING, not a PG enum (master
    plan §2.1 explicitly dropped two competing enum designs). The CHECK set,
    the model constant and the Pydantic Literal must be the same four values.
  - `job_kind` stays NULLABLE on the model. "Required" is an API-boundary
    rule only; making the column NOT NULL would break every legacy row.
  - The workflow engine's CREATE_TASK fills the new FKs from the polymorphic
    link. If it stops doing that, automations write blank rows that look like
    a frontend bug (master plan §9.2).
"""
import importlib.util
import os

import pytest
from sqlalchemy import CheckConstraint

from database_utils.models.crm import (
    TASK_ASSIGNEE_ROLES,
    TASK_STATE_KINDS,
    Task,
    TaskJobKind,
    TaskState,
    task_assignee,
)
from database_utils.schemas.task_state import (
    TaskStateCreate,
    TaskStateOut,
    TaskStateUpdate,
)

_HERE = os.path.dirname(__file__)
_VERSIONS = os.path.join(_HERE, "..", "alembic", "versions")
_ISP_SEED_PATH = os.path.join(_HERE, "..", "alembic", "seeds", "isp_seed.py")

_NEW_TASK_COLUMNS = (
    "client_id",
    "client_service_id",
    "device_category_id",
    "inventory_item_id",
    "parent_item_id",
    "address",
)

# The seven grants of the new role (master plan §2.7). Pinned as a set: a
# collector must be able to see what is owed and record the payment, and must
# NOT gain client edits or order creation.
COLLECTOR_GRANTS = {
    "tasks.read",
    "task_states.read",
    "clients.read",
    "payments.read",
    "payments.record",
    "orders.read",
    "client_services.read",
}


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _tj1():
    return _load(os.path.join(_VERSIONS, "tj1_task_job_kinds.py"), "tj1_task_job_kinds")


def _tk2():
    return _load(os.path.join(_VERSIONS, "tk2_task_links.py"), "tk2_task_links")


def _isp_seed():
    return _load(_ISP_SEED_PATH, "isp_seed_task_links_test")


# --- migration chain ---

def test_migration_chain_position():
    tj1, tk2 = _tj1(), _tk2()
    assert tj1.revision == "tj1_task_job_kinds"
    assert tj1.down_revision == "cl1_client_dpi_deactivation"
    assert tk2.revision == "tk2_task_links"
    # tk2 MUST come after tj1: Postgres forbids referencing an enum label in
    # the transaction that created it, which is the whole reason tj1 exists.
    assert tk2.down_revision == "tj1_task_job_kinds"


def test_tj1_never_references_the_labels_it_adds():
    with open(os.path.join(_VERSIONS, "tj1_task_job_kinds.py")) as fh:
        body = fh.read().split("def upgrade")[1].split("def downgrade")[0]
    # The ADD VALUE statements themselves are the only mentions; any other use
    # (a backfill, a CHECK) would fail at runtime on a fresh database.
    assert body.count("SUSPEND") == 1
    assert body.count("MAINTENANCE") == 1


# --- revision <-> model agreement ---

def test_revision_and_model_agree_on_the_columns():
    tk2 = _tk2()
    by_table = {}
    for table, column in tk2._NEW_COLUMNS:
        by_table.setdefault(table, []).append(column)
    assert by_table["task_state"] == ["kind"]
    assert by_table["task"] == list(_NEW_TASK_COLUMNS)
    assert by_table["task_assignee"] == ["role"]
    for column in _NEW_TASK_COLUMNS:
        assert column in Task.__table__.columns
    assert "kind" in TaskState.__table__.columns
    assert "role" in task_assignee.columns


def test_revision_and_model_agree_on_the_indexes():
    tk2 = _tk2()
    model_indexes = {i.name for i in Task.__table__.indexes}
    assert set(tk2._NEW_INDEXES) == model_indexes - {
        "ix_task_company_scheduled_date", "ix_task_company_id"
    }


def test_revision_and_model_agree_on_the_kind_set():
    assert _tk2().TASK_STATE_KINDS == TASK_STATE_KINDS


# --- task_state.kind ---

def test_kind_is_a_checked_string_not_an_enum():
    column = TaskState.__table__.columns["kind"]
    # An enum here would make every new state semantic a migration; the plan
    # picked a CHECK precisely so the set stays a cheap ALTER.
    assert column.type.python_type is str
    assert column.nullable is False
    assert column.server_default.arg == "IN_PROGRESS"


def test_kind_check_pins_exactly_the_four_kinds():
    check = next(
        c for c in TaskState.__table__.constraints
        if isinstance(c, CheckConstraint) and c.name == "ck_task_state_kind"
    )
    text = str(check.sqltext)
    for kind in TASK_STATE_KINDS:
        assert f"'{kind}'" in text
    assert text.count("'") == 2 * len(TASK_STATE_KINDS)


def test_assignee_role_check_allows_null():
    check = next(
        c for c in task_assignee.constraints
        if isinstance(c, CheckConstraint) and c.name == "ck_task_assignee_role"
    )
    text = str(check.sqltext)
    # Legacy rows are NULL and read as technicians — a NOT NULL default would
    # silently relabel every pre-existing assignment.
    assert "role IS NULL" in text
    for role in TASK_ASSIGNEE_ROLES:
        assert f"'{role}'" in text
    assert task_assignee.columns["role"].nullable is True


# --- job_kind ---

@pytest.mark.parametrize("label", ("SUSPEND", "MAINTENANCE"))
def test_new_job_kinds_exist(label):
    assert TaskJobKind(label).value == label


def test_job_kind_column_stays_nullable():
    # Required-ness is an API rule (TaskCreateIn / zod / xlsx), never a DB
    # constraint: every legacy task row has NULL and there is no backfill.
    assert Task.__table__.columns["job_kind"].nullable is True


# --- new task FKs ---

@pytest.mark.parametrize("column", _NEW_TASK_COLUMNS)
def test_new_task_columns_are_nullable(column):
    assert Task.__table__.columns[column].nullable is True


def test_device_category_fk_restricts_and_the_rest_set_null():
    expected = {
        "client_id": "SET NULL",
        "client_service_id": "SET NULL",
        # Platform-global, admin-curated table: deleting a category out from
        # under open work orders is a bug to block, not a cascade to absorb.
        "device_category_id": "RESTRICT",
        "inventory_item_id": "SET NULL",
        "parent_item_id": "SET NULL",
    }
    for column, ondelete in expected.items():
        fk = next(iter(Task.__table__.columns[column].foreign_keys))
        assert fk.ondelete == ondelete, column


def test_both_inventory_relationships_are_disambiguated():
    # Two FKs into inventory_item: without foreign_keys= SQLAlchemy cannot
    # pick a join condition and mapper configuration blows up at import.
    for name in ("inventory_item", "parent_item"):
        rel = Task.__mapper__.relationships[name]
        assert len(rel._user_defined_foreign_keys) == 1


# --- schemas ---

def test_task_state_schemas_carry_kind():
    assert TaskStateCreate(name="x").kind == "IN_PROGRESS"
    assert "kind" in TaskStateOut.model_fields
    # PATCH-shaped: absent means untouched, so it must be optional.
    assert not TaskStateUpdate.model_fields["kind"].is_required()


def test_unknown_kind_is_rejected_at_the_schema_not_the_check():
    with pytest.raises(Exception):
        TaskStateCreate(name="x", kind="TODO")


# --- seed invariant ---

def test_collector_role_is_seeded_with_exactly_its_grants():
    roles = _isp_seed().ISP_ROLES
    assert "COLLECTOR" in roles, "master plan §2.7 — the Cobrador picker is empty without it"
    assert set(roles["COLLECTOR"]["permissions"]) == COLLECTOR_GRANTS


def test_install_template_stamps_job_kind():
    # Without it every automation-created installation is an untyped row in
    # the redesigned table (master plan §9.2).
    templates = {t["key"]: t for t in _isp_seed().WORKFLOW_TEMPLATES}
    step = next(
        s for s in templates["new-installation"]["definition"]["steps"]
        if s["action_type"] == "CREATE_TASK"
    )
    assert step["action_config"]["job_kind"] == TaskJobKind.INSTALL.value


# --- workflow engine: automation-created tasks must not be blank ---

def _create_task(db, plant, **config):
    import uuid as _uuid

    from database_utils.models.crm import TaskState
    from database_utils.utils.workflow_engine import _execute_create_task

    state = db.query(TaskState).filter(TaskState.company_id == plant.company_id).first()
    if state is None:
        state = TaskState(id=_uuid.uuid4(), company_id=plant.company_id, name="Nuevas")
        db.add(state)
        db.flush()
    result = _execute_create_task(
        db,
        {"name": "auto", "task_state_id": str(state.id), **config},
        {},
        plant.company_id,
    )
    return db.query(Task).filter(Task.id == _uuid.UUID(result["resource_id"])).one()


def test_create_task_resolves_client_service_and_its_client(db, plant):
    from database_utils.models.crm import Client

    # The plant fixture only stores a client_id; the engine verifies the row
    # exists and is ours before writing fk_task_client_id, so it needs one.
    db.add(Client(id=plant.service.client_id, company_id=plant.company_id,
                  name="Probe"))
    db.flush()
    task = _create_task(
        db, plant,
        linked_object_type="CLIENT_SERVICE",
        linked_object_id=str(plant.service.id),
    )
    assert task.client_service_id == plant.service.id
    # The client comes THROUGH the service — the automation never names it.
    assert task.client_id == plant.service.client_id


def test_create_task_resolves_inventory_item_and_its_category(db, plant):
    task = _create_task(
        db, plant,
        linked_object_type="INVENTORY_ITEM",
        linked_object_id=str(plant.cpe.id),
    )
    assert task.inventory_item_id == plant.cpe.id
    assert task.device_category_id == plant.categories["ONU"].id


def test_create_task_skips_a_dangling_link(db, plant):
    import uuid as _uuid
    # linked_object_id has no FK and never guaranteed the row exists; writing
    # the new FK anyway would trip fk_task_client_service_id and fail the step.
    task = _create_task(
        db, plant,
        linked_object_type="CLIENT_SERVICE",
        linked_object_id=str(_uuid.uuid4()),
    )
    assert task.client_service_id is None
    assert task.client_id is None


def test_create_task_job_kind_is_optional_and_validated(db, plant):
    assert _create_task(db, plant).job_kind is None
    assert _create_task(db, plant, job_kind="INSTALL").job_kind is TaskJobKind.INSTALL
    with pytest.raises(ValueError, match="invalid job_kind"):
        _create_task(db, plant, job_kind="COBRO")
