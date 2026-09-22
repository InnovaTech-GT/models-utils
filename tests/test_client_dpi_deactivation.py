"""Clientes DPI + deactivation (cl1_client_dpi_deactivation) guardrails.

Three columns and two indexes exist in two places that cannot import each
other — the hand-written revision and the SQLAlchemy model — and the pair is
what `alembic check` compares. A column added to only one half passes every
other test in this suite and shows up as schema drift in CI instead, so it is
pinned here per column and per index (cf1 precedent).

The unique index MUST stay partial: `dpi` is nullable by decision (no
backfill, legacy rows and xlsx imports have none), so a plain UNIQUE would
reject the second NULL-dpi client of a tenant. That predicate is the one
detail a "clean up the __table_args__" edit would quietly drop.
"""
import importlib.util
import os

import pytest
from sqlalchemy import Column

from database_utils.models.crm import Client
from database_utils.schemas.client import (
    ClientAccountDetailOut,
    ClientAccountOut,
    ClientBase,
    ClientCreate,
    ClientDeactivateIn,
    ClientOut,
    ClientServiceBillingOut,
    ClientServiceSummaryOut,
    ClientUpdate,
)
from database_utils.schemas.client_service import (
    ClientServiceCreate,
    ClientServiceOut,
    ClientServiceUpdate,
)

_HERE = os.path.dirname(__file__)
_CL1_PATH = os.path.join(
    _HERE, "..", "alembic", "versions", "cl1_client_dpi_deactivation.py"
)

_NEW_COLUMNS = ("dpi", "deactivated_at", "deactivation_reason")


def _cl1():
    spec = importlib.util.spec_from_file_location(
        "cl1_client_dpi_deactivation", _CL1_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- migration chain ---

def test_migration_chain_position():
    cl1 = _cl1()
    assert cl1.revision == "cl1_client_dpi_deactivation"
    assert cl1.down_revision == "cfg3_matrix_permissions"


def test_revision_and_model_agree_on_the_columns():
    cl1 = _cl1()
    assert [c for _, c in cl1._NEW_COLUMNS] == list(_NEW_COLUMNS)
    assert all(t == "client" for t, _ in cl1._NEW_COLUMNS)


def test_revision_and_model_agree_on_the_indexes():
    cl1 = _cl1()
    assert set(cl1._NEW_INDEXES) == {i.name for i in Client.__table__.indexes} - {
        "ix_client_company_id"
    }


# --- model ---

@pytest.mark.parametrize("name", _NEW_COLUMNS)
def test_column_exists_and_is_nullable(name):
    column: Column = Client.__table__.columns[name]
    # All three are nullable with no backfill: NULL dpi = not collected yet,
    # NULL deactivated_at = active (the Actuales tab needs no data migration).
    assert column.nullable is True


def test_dpi_unique_index_is_partial_and_company_scoped():
    index = next(i for i in Client.__table__.indexes if i.name == "uq_client_company_dpi")
    assert index.unique is True
    assert [c.name for c in index.columns] == ["company_id", "dpi"]
    where = index.dialect_options["postgresql"]["where"]
    assert where is not None and "dpi IS NOT NULL" in str(where)


def test_active_index_covers_the_tab_predicate():
    index = next(i for i in Client.__table__.indexes if i.name == "ix_client_company_active")
    assert index.unique is False
    assert [c.name for c in index.columns] == ["company_id", "deactivated_at"]


# --- schemas ---

@pytest.mark.parametrize("schema", [ClientBase, ClientCreate, ClientUpdate, ClientOut])
def test_dpi_is_writable_and_optional(schema):
    field = schema.model_fields["dpi"]
    assert not field.is_required(), "dpi is nullable by decision — no default means a 422"


@pytest.mark.parametrize(
    "name", ("created_at", "deactivated_at", "deactivation_reason", "account", "services_summary")
)
def test_client_out_exposes_the_new_read_fields(name):
    assert name in ClientOut.model_fields


@pytest.mark.parametrize("name", ("deactivated_at", "deactivation_reason"))
def test_deactivation_is_never_client_writable(name):
    # Deactivation goes through POST /clients/{id}/{deactivate,reactivate}, so
    # a generic PATCH must not be able to stamp or clear the date.
    assert name not in ClientUpdate.model_fields
    assert name not in ClientCreate.model_fields


def test_account_rollup_shape():
    assert ClientAccountOut.model_fields["state"].is_required()
    for name in ("overdue_cents", "pending_orders", "oldest_due_date"):
        assert name in ClientAccountOut.model_fields
    # The detail endpoint extends the list rollup instead of declaring a
    # second one (master plan §2.2).
    assert issubclass(ClientAccountDetailOut, ClientAccountOut)
    assert "services" in ClientAccountDetailOut.model_fields
    for name in ("client_service_id", "plan_name", "recurrence",
                 "next_generation_date", "charge_cents"):
        assert name in ClientServiceBillingOut.model_fields


def test_service_summary_is_the_small_shape():
    assert set(ClientServiceSummaryOut.model_fields) == {
        "id", "plan_name", "plan_type", "status"
    }


def test_deactivate_body_defaults_to_no_cascade():
    body = ClientDeactivateIn()
    assert body.reason is None
    assert body.cascade is False


@pytest.mark.parametrize("name", ("cpe_serial_number", "cpe_online"))
def test_cpe_annotations_are_out_only(name):
    # Backend-COMPUTED from the CPE item + its ACS registration; accepting
    # them on a write schema would let a client invent its own "En linea".
    assert name in ClientServiceOut.model_fields
    assert name not in ClientServiceCreate.model_fields
    assert name not in ClientServiceUpdate.model_fields
