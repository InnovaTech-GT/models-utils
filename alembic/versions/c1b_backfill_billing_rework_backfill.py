"""billing rework R2: batched cents/status backfill + payments.* permission rows

Revision ID: c1b_backfill
Revises: c1a_billing_ddl
Create Date: 2026-07-05

Doc 16 §2.4 R2 / §2.5 items 1-8. Data only:
- Batched backfills (10k rows per commit) run inside autocommit_block so each
  batch commits independently (requires env.py transaction_per_migration=True).
- Every block is idempotent (WHERE ... IS NULL / convergent-state guards) —
  the revision is safely re-runnable after a lock_timeout abort.
- Money conversion is ROUND(x::numeric * 100)::bigint (half-away-from-zero).
- Ends with in-migration assertions that RAISE on any mismatch.
- Inserts payments.record/read/refund permission rows and copies grants from
  orders.update / orders.read / orders.revert_payment (pattern: 9e6f5200dac4).

Downgrade: intentional no-op. The backfill only fills additive columns that
R1's downgrade drops entirely; un-invalidating duplicate invoices or deleting
copied grants would destroy information with no consumer benefit (doc 16 §7:
"forward re-runnable").

Concurrency note: prod migrates while the OLD backend serves traffic. A write
landing between the last backfill batch and the assertions (e.g. an order
INSERT with total_cents NULL, or a legacy pay-order flipping `paid`) can trip
an assertion. That is safe by design: every block here is idempotent, so the
documented remedy is simply re-running `alembic upgrade head` — the second
pass converges and the assertions pass (doc 16 §6.h retry-from-Rn).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.sql import text


# revision identifiers, used by Alembic.
revision: str = 'c1b_backfill'
down_revision: Union[str, Sequence[str], None] = 'c1a_billing_ddl'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

BATCH = 10_000


def _batched(connection, sql: str) -> int:
    """Run a self-limiting UPDATE until it converges. Each execution commits
    on its own (caller wraps us in autocommit_block). Returns rows touched."""
    total = 0
    while True:
        result = connection.execute(text(sql), {"batch": BATCH})
        total += result.rowcount
        if result.rowcount < BATCH:
            return total


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))

    # ------------------------------------------------------------------
    # Batched backfills — each batch commits individually.
    # ------------------------------------------------------------------
    with op.get_context().autocommit_block():
        # §2.5.1 order.total_cents — copied from the Float, never recomputed
        # from items (order.total is historical truth).
        # (total IS NOT NULL is redundant today — the column is NOT NULL — but
        # keeps the loop provably convergent: a NULL source would otherwise
        # re-select the same rows forever.)
        _batched(connection, """
            UPDATE "order" SET total_cents = ROUND(total::numeric * 100)::bigint
            WHERE id IN (
                SELECT id FROM "order"
                WHERE total_cents IS NULL AND total IS NOT NULL LIMIT :batch
            )
        """)

        # §2.5.2 invoice — identity-preserving: subtotal_cents derived as
        # total_cents - tax_cents so subtotal+tax=total holds exactly in cents
        # even where the float triple has sub-cent drift.
        _batched(connection, """
            UPDATE invoice SET
                tax_cents      = ROUND(tax::numeric * 100)::bigint,
                total_cents    = ROUND(total::numeric * 100)::bigint,
                subtotal_cents = ROUND(total::numeric * 100)::bigint
                                 - ROUND(tax::numeric * 100)::bigint
            WHERE id IN (
                SELECT id FROM invoice
                WHERE total_cents IS NULL
                  AND total IS NOT NULL AND tax IS NOT NULL LIMIT :batch
            )
        """)

        # §2.5.3 catalog money (NULL stays NULL).
        _batched(connection, """
            UPDATE product SET price_cents = ROUND(price::numeric * 100)::bigint
            WHERE id IN (
                SELECT id FROM product
                WHERE price_cents IS NULL AND price IS NOT NULL LIMIT :batch
            )
        """)
        _batched(connection, """
            UPDATE service_plan SET price_cents = ROUND(price::numeric * 100)::bigint
            WHERE id IN (
                SELECT id FROM service_plan
                WHERE price_cents IS NULL AND price IS NOT NULL LIMIT :batch
            )
        """)
        _batched(connection, """
            UPDATE inventory_item SET cost_cents = ROUND(cost::numeric * 100)::bigint
            WHERE id IN (
                SELECT id FROM inventory_item
                WHERE cost_cents IS NULL AND cost IS NOT NULL LIMIT :batch
            )
        """)

        # §2.5.4 order_item snapshots from CURRENT product price/name
        # (best-effort; historical amount truth stays in order.total_cents).
        _batched(connection, """
            UPDATE order_item oi SET
                unit_price_cents = ROUND(p.price::numeric * 100)::bigint,
                product_name     = p.name
            FROM product p
            WHERE oi.product_id = p.id
              AND oi.id IN (
                SELECT oi2.id FROM order_item oi2
                JOIN product p2 ON p2.id = oi2.product_id
                WHERE oi2.unit_price_cents IS NULL LIMIT :batch
              )
        """)

        # §2.5.5 payment_status / order_type from the boolean/FK world.
        # Convergent guards (columns are NOT NULL with defaults, so no IS NULL).
        _batched(connection, """
            UPDATE "order" SET payment_status = 'PAID'
            WHERE id IN (
                SELECT id FROM "order"
                WHERE paid AND payment_status <> 'PAID' LIMIT :batch
            )
        """)
        # Symmetric direction: the OLD backend can flip paid true->false
        # (revert-payment) between a committed batch and the assertions.
        # Without this block that row would fail the paid<>PAID assertion
        # forever — re-running would NOT converge. At this revision the
        # ledger does not exist yet, so PENDING is the only correct reverse
        # mapping (PARTIAL/REFUNDED cannot occur).
        _batched(connection, """
            UPDATE "order" SET payment_status = 'PENDING'
            WHERE id IN (
                SELECT id FROM "order"
                WHERE NOT paid AND payment_status = 'PAID' LIMIT :batch
            )
        """)
        _batched(connection, """
            UPDATE "order" SET order_type = 'RECURRING'
            WHERE id IN (
                SELECT id FROM "order"
                WHERE recurring_order_id IS NOT NULL AND order_type <> 'RECURRING'
                LIMIT :batch
            )
        """)

        # §2.5.6 order.client_service_id via the client_service.recurring_order_id
        # bridge — ONLY where the recurring order maps to exactly ONE service
        # (the bridge has no unique constraint). Ambiguous rows stay unlinked.
        ambiguous = connection.execute(text("""
            SELECT COUNT(*) FROM (
                SELECT recurring_order_id FROM client_service
                WHERE recurring_order_id IS NOT NULL
                GROUP BY recurring_order_id HAVING COUNT(*) > 1
            ) t
        """)).scalar()
        print(f"[c1b_backfill] ambiguous recurring_order bridges skipped: {ambiguous} (expected 0)")
        _batched(connection, """
            UPDATE "order" o SET client_service_id = b.cs_id
            FROM (
                SELECT recurring_order_id AS ro_id, MIN(id::text)::uuid AS cs_id
                FROM client_service
                WHERE recurring_order_id IS NOT NULL
                GROUP BY recurring_order_id HAVING COUNT(*) = 1
            ) b
            WHERE o.recurring_order_id = b.ro_id
              AND o.client_service_id IS NULL
              AND o.id IN (
                SELECT o2.id FROM "order" o2
                JOIN (
                    SELECT recurring_order_id AS ro_id FROM client_service
                    WHERE recurring_order_id IS NOT NULL
                    GROUP BY recurring_order_id HAVING COUNT(*) = 1
                ) b2 ON o2.recurring_order_id = b2.ro_id
                WHERE o2.client_service_id IS NULL LIMIT :batch
              )
        """)

        # §2.5.7 payment_date for paid orders (prerequisite for the revenue
        # chart's date-basis switch to Order.payment_date).
        _batched(connection, """
            UPDATE "order" o SET payment_date = COALESCE(
                (SELECT MAX(i.created_at) FROM invoice i
                 WHERE i.order_id = o.id AND i.is_valid),
                o.created_at
            )
            WHERE o.id IN (
                SELECT id FROM "order"
                WHERE paid AND payment_date IS NULL LIMIT :batch
            )
        """)

        # §2.5.8 duplicate valid invoices per order: keep newest, invalidate
        # the rest — enables uq_invoice_order_valid in R3.
        deduped = _batched(connection, """
            UPDATE invoice SET is_valid = false
            WHERE id IN (
                SELECT id FROM (
                    SELECT id, ROW_NUMBER() OVER (
                        PARTITION BY order_id
                        ORDER BY created_at DESC, id DESC
                    ) AS rn
                    FROM invoice WHERE is_valid
                ) t WHERE rn > 1 LIMIT :batch
            )
        """)
        print(f"[c1b_backfill] duplicate valid invoices invalidated: {deduped} (expected 0)")

    # ------------------------------------------------------------------
    # payments.* permission rows + grant copy (doc 16 §1; pattern 9e6f5200dac4).
    # Runs in the migration's own transaction.
    # ------------------------------------------------------------------
    for name, action, description in (
        ("payments.record", "record", "Record payments against orders"),
        ("payments.read", "read", "View order payment ledgers"),
        ("payments.refund", "refund", "Refund recorded payments (ADMIN only)"),
    ):
        connection.execute(
            text("""
                INSERT INTO permission (id, created_at, name, resource, action, description)
                VALUES (gen_random_uuid(), NOW(), :name, 'payments', :action, :description)
                ON CONFLICT (name) DO NOTHING
            """),
            {"name": name, "action": action, "description": description},
        )

    # Copy grants: every role holding the source key gets the new key.
    for source, target in (
        ("orders.update", "payments.record"),
        ("orders.read", "payments.read"),
        ("orders.revert_payment", "payments.refund"),
    ):
        connection.execute(
            text("""
                INSERT INTO role_permission (role_id, permission_id)
                SELECT rp.role_id, pnew.id
                FROM role_permission rp
                JOIN permission pold ON pold.id = rp.permission_id AND pold.name = :source
                JOIN permission pnew ON pnew.name = :target
                ON CONFLICT DO NOTHING
            """),
            {"source": source, "target": target},
        )

    # ------------------------------------------------------------------
    # Assertions — RAISE on mismatch (migration fails loudly, doc 16 §2.4 R2).
    # ------------------------------------------------------------------
    checks = {
        "orders with total_cents NULL": """
            SELECT COUNT(*) FROM "order" WHERE total_cents IS NULL
        """,
        "invoices with cents NULL": """
            SELECT COUNT(*) FROM invoice
            WHERE subtotal_cents IS NULL OR tax_cents IS NULL OR total_cents IS NULL
        """,
        "invoices violating subtotal+tax=total identity": """
            SELECT COUNT(*) FROM invoice
            WHERE subtotal_cents + tax_cents <> total_cents
        """,
        "products with price set but price_cents NULL": """
            SELECT COUNT(*) FROM product WHERE price IS NOT NULL AND price_cents IS NULL
        """,
        "service_plans with price set but price_cents NULL": """
            SELECT COUNT(*) FROM service_plan WHERE price IS NOT NULL AND price_cents IS NULL
        """,
        "inventory_items with cost set but cost_cents NULL": """
            SELECT COUNT(*) FROM inventory_item WHERE cost IS NOT NULL AND cost_cents IS NULL
        """,
        "order_items with existing product but no snapshot": """
            SELECT COUNT(*) FROM order_item oi
            JOIN product p ON p.id = oi.product_id
            WHERE oi.unit_price_cents IS NULL
        """,
        "orders where paid <> (payment_status='PAID')": """
            SELECT COUNT(*) FROM "order" WHERE paid <> (payment_status = 'PAID')
        """,
        "orders where recurring bridge <> order_type": """
            SELECT COUNT(*) FROM "order"
            WHERE (recurring_order_id IS NOT NULL) <> (order_type = 'RECURRING')
        """,
        "paid orders without payment_date": """
            SELECT COUNT(*) FROM "order" WHERE paid AND payment_date IS NULL
        """,
        "unambiguous-bridge orders left without client_service_id (§2.5.6)": """
            SELECT COUNT(*) FROM "order" o
            JOIN (
                SELECT recurring_order_id AS ro_id FROM client_service
                WHERE recurring_order_id IS NOT NULL
                GROUP BY recurring_order_id HAVING COUNT(*) = 1
            ) b ON o.recurring_order_id = b.ro_id
            WHERE o.client_service_id IS NULL
        """,
        "orders with >1 valid invoice": """
            SELECT COUNT(*) FROM (
                SELECT order_id FROM invoice WHERE is_valid
                GROUP BY order_id HAVING COUNT(*) > 1
            ) t
        """,
        "companies whose cents/float order totals diverge beyond tolerance": """
            SELECT COUNT(*) FROM (
                SELECT company_id FROM "order"
                GROUP BY company_id
                HAVING ABS(SUM(total_cents) / 100.0 - SUM(total::numeric)) > 0.01 * COUNT(*)
            ) t
        """,
    }
    failures = []
    for label, sql in checks.items():
        count = connection.execute(text(sql)).scalar()
        if count:
            failures.append(f"{label}: {count}")
    if failures:
        raise RuntimeError(
            "c1b_backfill post-conditions FAILED — " + "; ".join(failures)
        )
    print("[c1b_backfill] all post-condition assertions passed")


def downgrade() -> None:
    """Intentional no-op (doc 16 §2.4 R2).

    The columns this revision fills are dropped wholesale by R1's downgrade;
    reversing invoice de-duplication or deleting the copied permission grants
    would destroy information. Forward is re-runnable (IS NULL guards)."""
    pass
