"""Unmap the lp1 free-text port labels; drop their unique index

Revision ID: pt2_unmap_port_labels
Revises: vw1_viewer_no_credential_read
Create Date: 2026-10-06

doc 40 §4.2 C8a. The model drops the `parent_port` / `uplink_port` Column
mappings and the `uq_inventory_item_parent_port` Index. The DB keeps both
columns, so a backend still on the old models (it reads and writes them) keeps
working during the rollout, but this revision drops the index: a C8a backend
no longer clears a moved item's label, so re-parenting an item whose legacy
label ("PON 1") a new sibling already carries would otherwise raise a unique
violation (a 500 on attach / reparent / the tecnicos connect step / the xlsx
re-parent). No data is lost; an old backend still checks label uniqueness in
code (`set_link_ports`, 409 PORT_IN_USE).

Downgrade recreates the index (IF NOT EXISTS). It fails if two siblings came to
share a label while at this revision; an old backend does not need the index
to run, so rolling the backend back does not require this downgrade.

The column drop is pt3_drop_port_labels (C8b), which must not reach a database
until every backend deployed against it runs C8a (doc 40 DI-13).
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy.sql import text

revision: str = "pt2_unmap_port_labels"
down_revision: Union[str, Sequence[str], None] = "vw1_viewer_no_credential_read"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

INDEX = "uq_inventory_item_parent_port"


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))
    op.execute(f"DROP INDEX IF EXISTS {INDEX}")
    if connection.execute(text(f"SELECT to_regclass('{INDEX}')")).scalar() is not None:
        raise RuntimeError(f"[pt2] index '{INDEX}' still exists after upgrade")


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))
    op.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS {INDEX} ON inventory_item (parent_id, parent_port) "
        "WHERE parent_port IS NOT NULL"
    )
