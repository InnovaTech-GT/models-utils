"""task.latitude/longitude: the task's own reference point (doc 46 §4.2.1)

Revision ID: tl1_task_location
Revises: ta1_task_assignee_model
Create Date: 2026-10-08

Additive: two nullable DOUBLE PRECISION columns + a pair/range CHECK. The
point is where the technician drives to; NULL = derived by backend-erp
utils/tasks.reference_point (client, then device for non-INSTALL, then the
planned parent). No index: reads go through ix_task_company_scheduled_date
or by id. No backfill.

ZTP program chain (doc 42a §4) puts this after ri1_inventory_received_at;
down_revision is the develop head at branch time and is re-pointed at
compose time if SP6 lands first.

Hand-written, house style: lock_timeout, idempotent, post-upgrade assert.
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy.sql import text

revision: str = "tl1_task_location"
down_revision: Union[str, Sequence[str], None] = "ta1_task_assignee_model"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

CHECK = "ck_task_location"


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))
    op.execute("ALTER TABLE task ADD COLUMN IF NOT EXISTS latitude DOUBLE PRECISION")
    op.execute("ALTER TABLE task ADD COLUMN IF NOT EXISTS longitude DOUBLE PRECISION")
    if connection.execute(text("SELECT 1 FROM pg_constraint WHERE conname = :n"), {"n": CHECK}).scalar() is None:
        op.execute(
            f"ALTER TABLE task ADD CONSTRAINT {CHECK} CHECK ("
            "(latitude IS NULL AND longitude IS NULL) "
            # IS NOT NULL is load-bearing: a half pair makes BETWEEN NULL, and
            # a CHECK that evaluates to NULL passes.
            "OR (latitude IS NOT NULL AND longitude IS NOT NULL "
            "AND latitude BETWEEN -90 AND 90 AND longitude BETWEEN -180 AND 180))"
        )
    columns = connection.execute(text(
        "SELECT count(*) FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = 'task' "
        "AND column_name IN ('latitude', 'longitude')"
    )).scalar()
    if columns != 2 or connection.execute(
        text("SELECT 1 FROM pg_constraint WHERE conname = :n"), {"n": CHECK}
    ).scalar() is None:
        raise RuntimeError("[tl1] task.latitude/longitude or ck_task_location missing after upgrade")


def downgrade() -> None:
    op.execute(f"ALTER TABLE task DROP CONSTRAINT IF EXISTS {CHECK}")
    op.execute("ALTER TABLE task DROP COLUMN IF EXISTS longitude")
    op.execute("ALTER TABLE task DROP COLUMN IF EXISTS latitude")
