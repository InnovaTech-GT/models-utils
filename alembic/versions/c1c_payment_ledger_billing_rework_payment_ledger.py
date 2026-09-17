"""billing rework R3: payment ledger table + legacy synthesis + one-valid-invoice rule

Revision ID: c1c_payment_ledger
Revises: c1b_backfill
Create Date: 2026-07-05

Doc 16 §2.4 R3 / §2.5 item 9. DDL + data:
- Creates the append-only `payment` table (§2.1) using the enum types owned by
  R1 (create_type=False).
- Synthesizes one LEGACY ledger row per historically paid order WHERE paid AND
  total_cents > 0 (MAJOR fix: zero-total paid orders — e.g. free installs —
  are skipped; ck_payment_amount_positive would otherwise abort the
  migration). Makes payment_status ledger-derivable with zero legacy
  special-casing beyond the zero-total carve-out.
- Creates uq_invoice_order_valid (partial unique) — safe now that R2
  remediated any duplicate valid invoices.

Rollback (doc 16 §7): before cutover only LEGACY rows exist (re-synthesizable).
After the first real PARTIAL payment R3 is the point of no return —
`pg_dump -t payment` is mandatory before any post-cutover downgrade.

Re-runnability (doc 16 §6.h retry-from-Rn): ALL DDL here is guarded
(has_table / CREATE INDEX IF NOT EXISTS) because the autocommit_block used for
batched synthesis commits everything before it. A failure anywhere in this
revision (duplicate valid invoice slipping in post-R2, assertion tripped by a
concurrent old-backend write) leaves alembic_version at c1b — re-running
`alembic upgrade head` is the documented remedy and converges: the R2 §2.5.8
dedup is re-applied here right before uq_invoice_order_valid, and the LEGACY
synthesis re-runs once more immediately before the assertions.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql import text


# revision identifiers, used by Alembic.
revision: str = 'c1c_payment_ledger'
down_revision: Union[str, Sequence[str], None] = 'c1b_backfill'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

BATCH = 10_000


def _synthesize_legacy_rows(connection) -> int:
    """Batched, idempotent LEGACY ledger synthesis (§2.5.9). Converges: the
    NOT EXISTS guard makes every pass a no-op once each paid order has its
    LEGACY_BACKFILL row. Caller must be in autocommit mode for batching."""
    total = 0
    while True:
        result = connection.execute(text("""
            INSERT INTO payment (
                id, created_at, company_id, order_id, invoice_id, kind,
                amount_cents, method, reference, paid_at, received_by,
                reverses_payment_id, notes
            )
            SELECT gen_random_uuid(), NOW(), o.company_id, o.id,
                   NULL, 'PAYMENT', o.total_cents, 'LEGACY',
                   'LEGACY_BACKFILL',
                   COALESCE(o.payment_date, o.created_at), NULL, NULL, NULL
            FROM "order" o
            WHERE o.paid
              AND o.total_cents > 0
              AND NOT EXISTS (
                  -- ANY prior PAYMENT row disqualifies, not just a LEGACY one:
                  -- if this revision is ever re-executed after cutover (stamp
                  -- rollback, restored alembic_version), orders paid through
                  -- the live ledger must NOT receive an extra full-amount
                  -- LEGACY row (that would double their ledger sum).
                  SELECT 1 FROM payment p
                  WHERE p.order_id = o.id AND p.kind = 'PAYMENT'
              )
            LIMIT :batch
        """), {"batch": BATCH})
        total += result.rowcount
        if result.rowcount < BATCH:
            return total


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))

    # --- payment table (§2.1) — enum types owned by R1, do not re-create ---
    # has_table guard: a prior partially-failed run of this revision may have
    # already committed the table (autocommit_block below commits early DDL).
    inspector = sa.inspect(connection)
    if not inspector.has_table('payment'):
        _create_payment_table()

    # Secondary indexes: IF NOT EXISTS for the same re-run reason.
    connection.execute(text(
        'CREATE INDEX IF NOT EXISTS ix_payment_company_id ON payment (company_id)'))
    connection.execute(text(
        'CREATE INDEX IF NOT EXISTS ix_payment_order_id ON payment (order_id)'))
    connection.execute(text(
        'CREATE INDEX IF NOT EXISTS ix_payment_company_order ON payment (company_id, order_id)'))
    connection.execute(text(
        'CREATE INDEX IF NOT EXISTS ix_payment_company_paid_at ON payment (company_id, paid_at)'))

    # --- Legacy ledger synthesis (§2.5.9) — batched, idempotent ---
    with op.get_context().autocommit_block():
        total = _synthesize_legacy_rows(connection)
        print(f"[c1c_payment_ledger] LEGACY_BACKFILL rows synthesized: {total}")

        # --- one-valid-invoice rule, DB-enforced ---
        # Re-apply the R2 §2.5.8 dedup first: the old backend is still live
        # between R2 and R3 and can have created a duplicate valid invoice in
        # the window — without this, the unique index build aborts and (worse,
        # pre-fix) left the revision unretryable.
        deduped = connection.execute(text("""
            UPDATE invoice SET is_valid = false
            WHERE id IN (
                SELECT id FROM (
                    SELECT id, ROW_NUMBER() OVER (
                        PARTITION BY order_id
                        ORDER BY created_at DESC, id DESC
                    ) AS rn
                    FROM invoice WHERE is_valid
                ) t WHERE rn > 1
            )
        """)).rowcount
        if deduped:
            print(f"[c1c_payment_ledger] duplicate valid invoices invalidated pre-index: {deduped}")
        connection.execute(text(
            'CREATE UNIQUE INDEX IF NOT EXISTS uq_invoice_order_valid '
            'ON invoice (order_id) WHERE is_valid'))

        # Re-run synthesis once right before asserting: a legacy pay-order
        # call landing mid-revision would otherwise trip the assertion; the
        # loop converges in one cheap pass (NOT EXISTS guard).
        _synthesize_legacy_rows(connection)

    # --- Assertions — RAISE on mismatch (rehearsal check e5, doc 16 §6) ---
    failures = []
    missing = connection.execute(text("""
        SELECT COUNT(*) FROM "order" o
        WHERE o.paid AND o.total_cents > 0
          AND NOT EXISTS (SELECT 1 FROM payment p WHERE p.order_id = o.id)
    """)).scalar()
    if missing:
        failures.append(f"paid orders (total_cents>0) without a ledger row: {missing}")
    mismatched = connection.execute(text("""
        SELECT COUNT(*) FROM payment p
        JOIN "order" o ON o.id = p.order_id
        WHERE p.reference = 'LEGACY_BACKFILL' AND p.amount_cents <> o.total_cents
    """)).scalar()
    if mismatched:
        failures.append(f"LEGACY_BACKFILL rows with amount <> order.total_cents: {mismatched}")
    if failures:
        raise RuntimeError(
            "c1c_payment_ledger post-conditions FAILED — " + "; ".join(failures)
            + " (revision is idempotent: re-run `alembic upgrade head`)"
        )
    print("[c1c_payment_ledger] all post-condition assertions passed")


def _create_payment_table() -> None:
    op.create_table(
        'payment',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('company_id', sa.Uuid(), nullable=False),
        sa.Column('order_id', sa.Uuid(), nullable=False),
        sa.Column('invoice_id', sa.Uuid(), nullable=True),
        sa.Column('kind', postgresql.ENUM(name='paymentkind', create_type=False), nullable=False),
        sa.Column('amount_cents', sa.BigInteger(), nullable=False),
        sa.Column('method', postgresql.ENUM(name='paymentmethodtype', create_type=False),
                  server_default='OTHER', nullable=False),
        sa.Column('reference', sa.String(), nullable=True),
        sa.Column('paid_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('received_by', sa.Uuid(), nullable=True),
        sa.Column('reverses_payment_id', sa.Uuid(), nullable=True),
        sa.Column('notes', sa.String(), nullable=True),
        sa.ForeignKeyConstraint(['company_id'], ['company.id'], ondelete='CASCADE'),
        # RESTRICT: DB-level guard — orders with money history cannot be deleted.
        sa.ForeignKeyConstraint(['order_id'], ['order.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['invoice_id'], ['invoice.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['received_by'], ['user.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['reverses_payment_id'], ['payment.id'], ondelete='RESTRICT'),
        sa.CheckConstraint('amount_cents > 0', name='ck_payment_amount_positive'),
        sa.CheckConstraint("(kind = 'PAYMENT') = (reverses_payment_id IS NULL)",
                           name='ck_payment_refund_link'),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade() -> None:
    """Drop the one-valid-invoice index and the payment table.

    The paymentkind/paymentmethodtype enum types are owned by R1
    (c1a_billing_ddl) and are dropped there. After the first real (non-LEGACY)
    payment this downgrade destroys money history — pg_dump -t payment first
    (doc 16 §7)."""
    op.drop_index('uq_invoice_order_valid', table_name='invoice', postgresql_where=sa.text('is_valid'))
    op.drop_index('ix_payment_company_paid_at', table_name='payment')
    op.drop_index('ix_payment_company_order', table_name='payment')
    op.drop_index(op.f('ix_payment_order_id'), table_name='payment')
    op.drop_index(op.f('ix_payment_company_id'), table_name='payment')
    op.drop_table('payment')
