"""Drop the lp1 free-text port labels (DESTRUCTIVE)

Revision ID: pt3_drop_port_labels
Revises: pt2_unmap_port_labels
Create Date: 2026-10-06

doc 40 §4.2 C8b. Drops `inventory_item.parent_port` and
`inventory_item.uplink_port` (and `uq_inventory_item_parent_port` IF EXISTS:
pt2 already dropped it). A device's ports are `network_link` +
`inventory_item_port`; the labels have been unmapped since pt2 (C8a).

ORDER: this must not reach a database until every backend deployed against
it runs C8a (models-utils >= 5.0.0, no PATCH /network/nodes/{id}/link). An
older backend still maps the columns and would 500 on every inventory_item
read (doc 40 DI-13).

Destructive: the label text is lost. The upgrade prints how many rows still
carried one, then asserts the columns and the index are gone. `downgrade()`
re-adds both columns (NULL, no data); the index is pt2's to recreate.
Idempotent (IF EXISTS / IF NOT EXISTS); lock_timeout so a busy table fails
fast instead of queueing every writer behind the ACCESS EXCLUSIVE lock.
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy.sql import text

revision: str = "pt3_drop_port_labels"
down_revision: Union[str, Sequence[str], None] = "pt2_unmap_port_labels"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

INDEX = "uq_inventory_item_parent_port"
LABEL_COLUMNS = (
    "SELECT column_name FROM information_schema.columns "
    "WHERE table_schema = current_schema() AND table_name = 'inventory_item' "
    "AND column_name IN ('parent_port', 'uplink_port')"
)


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))
    if connection.execute(text(LABEL_COLUMNS)).first():
        labelled = connection.execute(text(
            "SELECT count(*) FROM inventory_item "
            "WHERE parent_port IS NOT NULL OR uplink_port IS NOT NULL"
        )).scalar()
        print(f"[pt3_drop_port_labels] dropping labels on {labelled} inventory_item row(s)")
    op.execute(f"DROP INDEX IF EXISTS {INDEX}")
    op.execute("ALTER TABLE inventory_item DROP COLUMN IF EXISTS parent_port")
    op.execute("ALTER TABLE inventory_item DROP COLUMN IF EXISTS uplink_port")

    left = [r[0] for r in connection.execute(text(LABEL_COLUMNS))]
    if left:
        raise RuntimeError(f"[pt3] column(s) still present after upgrade: {left}")
    if connection.execute(text(
        "SELECT 1 FROM pg_indexes WHERE schemaname = current_schema() AND indexname = :i"
    ), {"i": INDEX}).first():
        raise RuntimeError(f"[pt3] index '{INDEX}' still present after upgrade")


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))
    op.execute("ALTER TABLE inventory_item ADD COLUMN IF NOT EXISTS parent_port VARCHAR(64)")
    op.execute("ALTER TABLE inventory_item ADD COLUMN IF NOT EXISTS uplink_port VARCHAR(64)")
