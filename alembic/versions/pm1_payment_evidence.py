"""Pagos: uploadedfileownertype += PAYMENT, ix_order_company_client

Revision ID: pm1_payment_evidence
Revises: inv1_general_inventory
Create Date: 2026-09-16

Figma redesign PR 5 (docs/design/plans/05-pagos.md §2, master plan §4 row 7).
No new tables and no new columns, on purpose:

1. Payment evidence ("Comprobante") reuses the existing polymorphic
   `uploaded_file` store with `owner_type='PAYMENT'`, `owner_id=payment.id`,
   `kind=PHOTO`. The existing `(owner_type, owner_id)` index already serves the
   lookup, so `payment.evidence_file_id` is a derived schema field, not a
   column. Only the enum needs a new label.

2. `ix_order_company_client` serves the per-client account aggregate
   (`GET /clients/{id}/account`, §3.2) and the `client_account` annotation on
   `GET /orders/`, which group the company's orders by `client_id`. Today
   `order` is indexed on `company_id` alone plus the partial overdue index, so
   an account page scans every order of the tenant.

IRREVERSIBLE by nature: Postgres cannot DROP an enum label, so `downgrade()`
drops the index and leaves 'PAYMENT' in `uploadedfileownertype` (precedents:
`c1e_install_actions`, `tj1_task_job_kinds`). An unreferenced label is
harmless — the Python enum is what decides what is writable.

No statement in this file may NAME the new label: Postgres forbids using an
enum value in the transaction that created it. Nothing needs to, there is no
backfill.

Hand-written (NOT autogenerate), ba1 house style: lock_timeout, IF NOT EXISTS,
post-upgrade assertions.
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy.sql import text


# revision identifiers, used by Alembic.
revision: str = 'pm1_payment_evidence'
down_revision: Union[str, Sequence[str], None] = 'inv1_general_inventory'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ENUM_NAME = "uploadedfileownertype"
_NEW_LABELS = ("PAYMENT",)
_NEW_INDEXES = ("ix_order_company_client",)


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))

    # Native-enum ADD VALUE always runs in an autocommit_block (c1e/tj1
    # precedent): with transaction_per_migration the new value must be
    # committed before any later statement or seed can reference it.
    with op.get_context().autocommit_block():
        op.execute(
            f"ALTER TYPE {_ENUM_NAME} ADD VALUE IF NOT EXISTS 'PAYMENT'"
        )

    # "order" is a reserved word — always quoted.
    op.execute(
        'CREATE INDEX IF NOT EXISTS ix_order_company_client '
        'ON "order" (company_id, client_id)'
    )

    # --- assertions ---------------------------------------------------------
    present = set(connection.execute(text(
        "SELECT e.enumlabel FROM pg_enum e "
        "JOIN pg_type t ON t.oid = e.enumtypid WHERE t.typname = :name"
    ), {"name": _ENUM_NAME}).scalars())
    missing = [label for label in _NEW_LABELS if label not in present]
    if missing:
        raise RuntimeError(
            f"[pm1] {_ENUM_NAME} label(s) missing after upgrade: {', '.join(missing)}"
        )

    for index_name in _NEW_INDEXES:
        if connection.execute(
            text("SELECT to_regclass(:name)"), {"name": index_name}
        ).scalar() is None:
            raise RuntimeError(
                f"[pm1] expected index '{index_name}' to exist after upgrade"
            )

    print("[pm1_payment_evidence] upgrade complete")


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))

    for index_name in _NEW_INDEXES:
        op.execute(f"DROP INDEX IF EXISTS {index_name}")

    # PostgreSQL cannot DROP an enum label: 'PAYMENT' stays in
    # uploadedfileownertype after a downgrade. Documented no-op, same shape as
    # c1e_install_actions / tj1_task_job_kinds.
    print("[pm1_payment_evidence] enum label kept (PG cannot drop enum labels)")
