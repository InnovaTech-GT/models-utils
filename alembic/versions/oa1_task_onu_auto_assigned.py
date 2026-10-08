"""task.onu_auto_assigned + reservation backfill (doc 45 §4.3, ZTP SP4)

Revision ID: oa1_task_onu_auto_assigned
Revises: tl1_task_location
Create Date: 2026-10-08

1. `task.onu_auto_assigned` BOOLEAN NOT NULL DEFAULT false: true when the
   server chose `inventory_item_id` (custody-first / warehouse FIFO, backend-erp
   services/onu_assignment.py). Existing links are manual.
2. One-off data backfill: every IN_STOCK ONU referenced by an open (status <>
   'DONE') INSTALL task becomes RESERVED, with one RESERVED equipment_event
   (technician = that task's lowest-user_id TECHNICIAN-or-NULL assignee, or
   NULL; event_metadata {"task_id", "auto": false, "backfill": true}). A unit
   held by two open tasks is reserved once, for the oldest task; rollout step 5
   lists those duplicates for the office. Idempotent: a reserved unit is no
   longer IN_STOCK.

Downgrade drops the column only; the backfilled reservations are correct data
and stay.

ZTP program chain (doc 42a §4): pc1 -> ta1 -> ri1 -> tl1 -> oa1 -> pe1 -> zt1.

Hand-written, house style: lock_timeout, idempotent, post-upgrade assert.
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy.sql import text

revision: str = "oa1_task_onu_auto_assigned"
down_revision: Union[str, Sequence[str], None] = "tl1_task_location"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Open INSTALL tasks pointing at an IN_STOCK ONU, one row per unit (oldest task).
_HELD = """
    SELECT DISTINCT ON (i.id) i.id AS item_id, i.company_id, t.id AS task_id
    FROM inventory_item i
    JOIN task t ON t.inventory_item_id = i.id
    JOIN device_type dt ON dt.id = i.device_type_id
    JOIN device_category dc ON dc.id = dt.category_id
    WHERE i.status = 'IN_STOCK' AND dc.key = 'ONU'
      AND t.status <> 'DONE' AND t.job_kind = 'INSTALL'
    ORDER BY i.id, t.created_at, t.id
"""


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))
    op.execute(
        "ALTER TABLE task ADD COLUMN IF NOT EXISTS onu_auto_assigned BOOLEAN NOT NULL DEFAULT false"
    )
    op.execute(f"""
        WITH held AS ({_HELD}),
        ev AS (
            INSERT INTO equipment_event
                (id, created_at, event_type, company_id, item_id, technician_id, event_metadata)
            SELECT gen_random_uuid(), now(), 'RESERVED', h.company_id, h.item_id,
                   (SELECT a.user_id FROM task_assignee a
                     WHERE a.task_id = h.task_id AND (a.role IS NULL OR a.role = 'TECHNICIAN')
                     ORDER BY a.user_id LIMIT 1),
                   json_build_object('task_id', h.task_id, 'auto', false, 'backfill', true)
            FROM held h
            RETURNING item_id
        )
        UPDATE inventory_item SET status = 'RESERVED', updated_at = now()
        WHERE id IN (SELECT item_id FROM ev)
    """)
    if connection.execute(text(
        "SELECT count(*) FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = 'task' "
        "AND column_name = 'onu_auto_assigned'"
    )).scalar() != 1:
        raise RuntimeError("[oa1] task.onu_auto_assigned missing after upgrade")
    left = connection.execute(text(f"SELECT count(*) FROM ({_HELD}) h")).scalar()
    if left:
        raise RuntimeError(f"[oa1] {left} IN_STOCK ONU(s) still held by an open INSTALL task")


def downgrade() -> None:
    op.execute("ALTER TABLE task DROP COLUMN IF EXISTS onu_auto_assigned")
