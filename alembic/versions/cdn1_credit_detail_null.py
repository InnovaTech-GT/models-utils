"""billing_invoice.credit_detail: JSON null becomes SQL NULL

Revision ID: cdn1_credit_detail_null
Revises: rtg2_run_trigger_backfill
Create Date: 2026-11-16

Data only, no DDL (6.34.1). The model column is now JSON(none_as_null=True), so
new invoices without referral credit store SQL NULL; this moves the rows written
before that (the JSON literal 'null') to SQL NULL too, so both look alike. The
only reader (`credit_detail or []`) treats both as empty, so it is safe at any
point of a deploy. Idempotent; downgrade is a documented no-op (a JSON 'null'
and SQL NULL mean the same thing).

Hand-written, house style: lock_timeout, idempotent.
"""
from collections.abc import Sequence

from sqlalchemy.sql import text

from alembic import op

revision: str = "cdn1_credit_detail_null"
down_revision: str | Sequence[str] | None = "rtg2_run_trigger_backfill"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATEMENT = "UPDATE billing_invoice SET credit_detail = NULL WHERE credit_detail::text = 'null'"


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))
    connection.execute(text(STATEMENT))
    print("[cdn1_credit_detail_null] upgrade complete")


def downgrade() -> None:
    # Documented no-op: JSON 'null' and SQL NULL mean the same thing to every reader.
    print("[cdn1_credit_detail_null] downgrade is a no-op")
