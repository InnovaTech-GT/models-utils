"""No-op: InventoryItem stops mapping the lp1 free-text port labels

Revision ID: pt2_unmap_port_labels
Revises: vw1_viewer_no_credential_read
Create Date: 2026-10-06

doc 40 §4.2 C8a. The model drops the `parent_port` / `uplink_port` Column
mappings and the `uq_inventory_item_parent_port` Index; the DB keeps all
three, so a backend still on the old models (it reads and writes them) keeps
working during the rollout. Nothing to do here: the revision exists so the CI
migration guard passes and the migrate workflow records the step.

The drop is pt3_drop_port_labels (C8b), which must not reach a database until
every backend deployed against it runs C8a (doc 40 DI-13).
"""
from typing import Sequence, Union

revision: str = "pt2_unmap_port_labels"
down_revision: Union[str, Sequence[str], None] = "vw1_viewer_no_credential_read"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
