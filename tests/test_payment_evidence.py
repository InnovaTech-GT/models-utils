"""Pagos (pm1_payment_evidence) guardrails.

Same shape as tests/test_general_inventory.py. Three things are load-bearing
beyond "the field exists":

  - `ix_order_company_client` is declared in two places that cannot import each
    other (the hand-written revision and Order.__table_args__). If they drift,
    autogenerate proposes dropping the index nobody notices is gone.
  - The revision must NOT name the new enum label outside the autocommit
    ALTER TYPE: Postgres forbids using a value in the transaction that created
    it, and the failure only shows up against a real PG.
  - `credit_cents` is pinned to a 0 default on purpose — there is no credit
    ledger, and a field that silently starts returning a number would put a
    fabricated "Saldo a favor" in front of a cashier.
"""
import importlib.util
import os

from database_utils.models.crm import Order, UploadedFileOwnerType
from database_utils.schemas.client import ClientAccountDetailOut
from database_utils.schemas.order import OrderLastPaymentOut, OrderOut
from database_utils.schemas.payment import (
    FullPaymentCreate,
    PaymentCreate,
    PaymentMetadataUpdate,
    PaymentOut,
)

_HERE = os.path.dirname(__file__)
_VERSIONS = os.path.join(_HERE, "..", "alembic", "versions")


def _pm1():
    path = os.path.join(_VERSIONS, "pm1_payment_evidence.py")
    spec = importlib.util.spec_from_file_location("pm1_payment_evidence", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- migration chain ---

def test_migration_chain_position():
    pm1 = _pm1()
    assert pm1.revision == "pm1_payment_evidence"
    assert pm1.down_revision == "inv1_general_inventory"


def test_revision_never_names_the_new_label_outside_the_alter():
    """PG cannot use an enum value in the transaction that created it, so the
    only statement allowed to say 'PAYMENT' is the ALTER TYPE itself."""
    path = os.path.join(_VERSIONS, "pm1_payment_evidence.py")
    with open(path) as handle:
        body = handle.read()
    body = body.split('"""', 2)[2]  # drop the module docstring
    for line in body.splitlines():
        code = line.split("#", 1)[0]
        if "'PAYMENT'" in code:
            assert "ADD VALUE IF NOT EXISTS" in line, line


def test_downgrade_only_drops_the_index():
    """The enum label survives a downgrade (documented no-op); the index does
    not. Anything else in downgrade() would be a data-destroying surprise."""
    path = os.path.join(_VERSIONS, "pm1_payment_evidence.py")
    with open(path) as handle:
        downgrade = handle.read().split("def downgrade()", 1)[1]
    assert "DROP INDEX IF EXISTS" in downgrade
    assert "DROP TYPE" not in downgrade
    assert "DROP COLUMN" not in downgrade


# --- revision <-> model parity ---

def test_index_declared_in_both_the_revision_and_the_model():
    pm1 = _pm1()
    assert pm1._NEW_INDEXES == ("ix_order_company_client",)
    index = next(
        i for i in Order.__table__.indexes if i.name == "ix_order_company_client"
    )
    assert [c.name for c in index.columns] == ["company_id", "client_id"]
    assert index.unique is False


def test_payment_owner_type_label():
    assert UploadedFileOwnerType.PAYMENT.value == "PAYMENT"
    assert _pm1()._NEW_LABELS == ("PAYMENT",)
    assert _pm1()._ENUM_NAME == "uploadedfileownertype"


# --- schema pins ---

def test_payment_out_evidence_and_collector_name():
    assert PaymentOut.model_fields["received_by_name"].default is None
    # No payment.evidence_file_id COLUMN — it is derived from uploaded_file.
    assert PaymentOut.model_fields["evidence_file_id"].default is None
    assert "evidence_file_id" not in Order.__table__.columns
    from database_utils.models.crm import Payment
    assert "evidence_file_id" not in Payment.__table__.columns


def test_collector_is_optional_on_both_create_bodies():
    assert PaymentCreate.model_fields["received_by"].default is None
    assert FullPaymentCreate.model_fields["received_by"].default is None
    # FullPaymentCreate still has NO amount field (doc 18 D3).
    assert "amount_cents" not in FullPaymentCreate.model_fields


def test_payment_metadata_update_is_metadata_only():
    """Ledger rows are append-only: the PATCH body may never carry money."""
    assert set(PaymentMetadataUpdate.model_fields) == {"reference", "notes"}


def test_order_out_annotations_default_to_none():
    for field in ("last_payment", "period_label", "client_account"):
        assert OrderOut.model_fields[field].default is None
    assert set(OrderLastPaymentOut.model_fields) == {
        "id", "paid_at", "method", "received_by", "received_by_name",
    }


def test_client_account_detail_extensions():
    fields = ClientAccountDetailOut.model_fields
    assert fields["receivable_cents"].default == 0
    # Próximamente: no credit ledger exists, this must never be computed.
    assert fields["credit_cents"].default == 0
    assert fields["next_payment_day"].default is None
    assert fields["recurrence"].default is None
    assert fields["services_total"].default == 0
    # Extends PR 3's account shape, does not fork it.
    assert "state" in fields and "overdue_cents" in fields
