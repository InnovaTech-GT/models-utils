"""client.payment_day: the client's usual payment day of month (1..31)

Revision ID: pd1_client_payment_day
Revises: mp1_technician_plan_read
Create Date: 2026-10-03

Additive: one nullable SMALLINT + a range CHECK. Feeds the cobros "Pendientes"
payment-day filter (backend-erp /collections/receivables). No backfill — an
unset value falls back to the billing day in the API.

Hand-written, house style: lock_timeout, idempotent, post-upgrade assert.
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy.sql import text

revision: str = "pd1_client_payment_day"
down_revision: Union[str, Sequence[str], None] = "mp1_technician_plan_read"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

CHECK = "ck_client_payment_day_range"


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))
    op.execute("ALTER TABLE client ADD COLUMN IF NOT EXISTS payment_day SMALLINT")
    if connection.execute(text("SELECT 1 FROM pg_constraint WHERE conname = :n"), {"n": CHECK}).scalar() is None:
        op.execute(
            f"ALTER TABLE client ADD CONSTRAINT {CHECK} "
            "CHECK (payment_day IS NULL OR (payment_day BETWEEN 1 AND 31))"
        )
    if connection.execute(text(
        "SELECT 1 FROM information_schema.columns WHERE table_name = 'client' AND column_name = 'payment_day'"
    )).scalar() is None:
        raise RuntimeError("[pd1] client.payment_day missing after upgrade")


def downgrade() -> None:
    op.execute(f"ALTER TABLE client DROP CONSTRAINT IF EXISTS {CHECK}")
    op.execute("ALTER TABLE client DROP COLUMN IF EXISTS payment_day")
