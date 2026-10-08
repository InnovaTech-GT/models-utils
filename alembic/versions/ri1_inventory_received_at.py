"""inventory_item.received_at: when the unit entered stock (FIFO key)

Revision ID: ri1_inventory_received_at
Revises: ta1_task_assignee_model
Create Date: 2026-10-08

Doc 47 (SP6 inventory intake). Additive: one TIMESTAMPTZ NOT NULL column with
a now() server default. Existing rows are backfilled from created_at (the
honest best guess; the office can correct it via xlsx) BEFORE the NOT NULL
alter. down_revision is the current develop head; the ZTP program chain
(doc 42a §4) re-points it at compose time if needed.

Hand-written, house style: lock_timeout, idempotent, post-upgrade assert.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.sql import text

revision: str = "ri1_inventory_received_at"
down_revision: Union[str, Sequence[str], None] = "ta1_task_assignee_model"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))
    op.execute("ALTER TABLE inventory_item ADD COLUMN IF NOT EXISTS received_at TIMESTAMP WITH TIME ZONE")
    op.execute("UPDATE inventory_item SET received_at = created_at WHERE received_at IS NULL")
    op.alter_column("inventory_item", "received_at", nullable=False, server_default=sa.text("now()"))

    missing = connection.execute(text("SELECT count(*) FROM inventory_item WHERE received_at IS NULL")).scalar()
    if missing:
        raise RuntimeError(f"[ri1] {missing} inventory items without received_at after upgrade")


def downgrade() -> None:
    op.execute("ALTER TABLE inventory_item DROP COLUMN IF EXISTS received_at")
