"""Dispatch routes: task.route_sequence

Revision ID: dr1_task_route_sequence
Revises: ts1_task_status
Create Date: 2026-09-28

The dispatch ETL (dispatch-etls) orders each technician's day and writes the
result back through backend-erp's POST /dispatch/routes. `route_sequence` is
the stop's position in that route for the task's `scheduled_date`, and the
technician app sorts its day by it. NULL means the task is not routed.

It is deliberately not `task.position`: that is the board order, which the
move and reorder endpoints renumber. No index: the reads are
(company_id, scheduled_date), already covered by the partial index
ix_task_company_scheduled_date.

Additive and fully reversible.
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy.sql import text


revision: str = "dr1_task_route_sequence"
down_revision: Union[str, Sequence[str], None] = "ts1_task_status"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))
    op.execute("ALTER TABLE task ADD COLUMN IF NOT EXISTS route_sequence INTEGER")
    if connection.execute(text(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = 'task' AND column_name = 'route_sequence'"
    )).scalar() is None:
        raise RuntimeError("[dr1] task.route_sequence missing after upgrade")
    print("[dr1_task_route_sequence] upgrade complete")


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))
    op.execute("ALTER TABLE task DROP COLUMN IF EXISTS route_sequence")
