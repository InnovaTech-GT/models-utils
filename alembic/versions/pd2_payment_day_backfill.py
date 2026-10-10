"""Backfill client.payment_day from each client's payment history

Revision ID: pd2_payment_day_backfill
Revises: zm1_manual_step
Create Date: 2026-10-09

Data only, no DDL. pd1 added client.payment_day empty for every client; the
cobros route ETL (dispatch-etls) needs a value for all of them.

For every client whose payment_day is still NULL:
  1. Take the GT calendar dates of its PAYMENT ledger rows (through
     payment.order_id -> order.client_id), skipping payments that a REFUND
     reverses. Several payments on the same date count once, so a client who
     settles two months in one visit does not outweigh its other months.
  2. payment_day = the day of month that appears on the most dates. A tie goes
     to the day of the most recent of the tied dates.
  3. A client with no usable payment gets DEFAULT_DAY (15).

A value already set (a collector's PATCH /collections/clients/{id}) is never
touched, so the revision is idempotent: a second run finds no NULL row.
Downgrade is a no-op: the values are valid payment days and nothing records
which ones this revision wrote; pd1's downgrade drops the column anyway.

Hand-written, house style: lock_timeout, idempotent, post-upgrade assert.
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy.sql import text

revision: str = "pd2_payment_day_backfill"
down_revision: Union[str, Sequence[str], None] = "zm1_manual_step"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DEFAULT_DAY = 15
TIMEZONE = "America/Guatemala"

FROM_HISTORY = f"""
WITH paid_dates AS (
    SELECT o.client_id, (p.paid_at AT TIME ZONE '{TIMEZONE}')::date AS paid_on
    FROM payment p
    JOIN "order" o ON o.id = p.order_id
    JOIN client c ON c.id = o.client_id
    WHERE c.payment_day IS NULL
      AND p.kind = 'PAYMENT'
      AND NOT EXISTS (SELECT 1 FROM payment r WHERE r.reverses_payment_id = p.id)
    GROUP BY o.client_id, paid_on
), day_counts AS (
    SELECT client_id, EXTRACT(DAY FROM paid_on)::int AS day,
           count(*) AS dates, max(paid_on) AS last_paid_on
    FROM paid_dates
    GROUP BY client_id, day
), best AS (
    SELECT DISTINCT ON (client_id) client_id, day
    FROM day_counts
    ORDER BY client_id, dates DESC, last_paid_on DESC
)
UPDATE client c SET payment_day = best.day
FROM best
WHERE c.id = best.client_id AND c.payment_day IS NULL
"""


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(text("SET lock_timeout = '5s'"))
    from_history = conn.execute(text(FROM_HISTORY)).rowcount
    defaulted = conn.execute(
        text("UPDATE client SET payment_day = :d WHERE payment_day IS NULL"), {"d": DEFAULT_DAY}
    ).rowcount
    print(f"[pd2] payment_day from payment history: {from_history}, default {DEFAULT_DAY}: {defaulted}")
    if conn.execute(text("SELECT count(*) FROM client WHERE payment_day IS NULL")).scalar():
        raise RuntimeError("[pd2] clients without payment_day after backfill")


def downgrade() -> None:
    pass
