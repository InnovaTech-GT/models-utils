"""Drop the lp1 free-text port labels (DESTRUCTIVE)

Revision ID: pt3_drop_port_labels
Revises: pt2_unmap_port_labels
Create Date: 2026-10-06

doc 40 §4.2 C8b. Drops `uq_inventory_item_parent_port`, then
`inventory_item.parent_port` and `inventory_item.uplink_port`. A device's
ports are `network_link` + `inventory_item_port`; the labels have been
unmapped since pt2 (C8a).

ORDER: this must not reach a database until every backend deployed against
it runs C8a (models-utils >= 5.0.0, no PATCH /network/nodes/{id}/link). An
older backend still maps the columns and would 500 on every inventory_item
read (doc 40 DI-13).

Destructive: the label text is lost. The upgrade prints how many rows still
carried one. `downgrade()` re-adds both columns (NULL, no data) and the index.
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


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))
    has_column = connection.execute(text(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = 'inventory_item' "
        "AND column_name = 'parent_port'"
    )).scalar()
    if has_column:
        labelled = connection.execute(text(
            "SELECT count(*) FROM inventory_item "
            "WHERE parent_port IS NOT NULL OR uplink_port IS NOT NULL"
        )).scalar()
        print(f"[pt3_drop_port_labels] dropping labels on {labelled} inventory_item row(s)")
    op.execute(f"DROP INDEX IF EXISTS {INDEX}")
    op.execute("ALTER TABLE inventory_item DROP COLUMN IF EXISTS parent_port")
    op.execute("ALTER TABLE inventory_item DROP COLUMN IF EXISTS uplink_port")


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))
    op.execute("ALTER TABLE inventory_item ADD COLUMN IF NOT EXISTS parent_port VARCHAR(64)")
    op.execute("ALTER TABLE inventory_item ADD COLUMN IF NOT EXISTS uplink_port VARCHAR(64)")
    op.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS {INDEX} ON inventory_item (parent_id, parent_port) "
        "WHERE parent_port IS NOT NULL"
    )
