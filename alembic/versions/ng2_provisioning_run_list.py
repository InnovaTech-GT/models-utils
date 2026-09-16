"""Red/provisioning: ix_provisioning_run_company_created (company_id, created_at)

Revision ID: ng2_provisioning_run_list
Revises: al1_audit_log_created_idx
Create Date: 2026-09-16

Figma redesign PR 9 (docs/design/plans/09-red-provisioning.md §2.1, master plan
§4 row 10). One index, nothing else — no new tables, columns or backfill.

`provisioning_run` only has `ix_provisioning_run_service (client_service_id,
created_at)`, which is useless to the new company-wide run list: that page is
`WHERE company_id = ? ORDER BY created_at DESC` with skip/limit, i.e. today a
full scan + sort of every run in the table.

Not DESC here (unlike al1): a plain B-tree on (company_id, created_at) is
scannable backwards for free once company_id is an equality predicate, so the
ordering direction buys nothing.

`IF NOT EXISTS` lets an operator pre-create the index with CREATE INDEX
CONCURRENTLY on a large production table and have this revision no-op.

Fully reversible. Hand-written (NOT autogenerate), ba1 house style:
lock_timeout, IF NOT EXISTS, post-upgrade assertion.
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy.sql import text


# revision identifiers, used by Alembic.
revision: str = 'ng2_provisioning_run_list'
down_revision: Union[str, Sequence[str], None] = 'al1_audit_log_created_idx'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_NEW_INDEXES = ("ix_provisioning_run_company_created",)


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))

    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_provisioning_run_company_created "
        "ON provisioning_run (company_id, created_at)"
    )

    # --- assertions ---------------------------------------------------------
    for index_name in _NEW_INDEXES:
        if connection.execute(
            text("SELECT to_regclass(:name)"), {"name": index_name}
        ).scalar() is None:
            raise RuntimeError(
                f"[ng2] expected index '{index_name}' to exist after upgrade"
            )

    print("[ng2_provisioning_run_list] upgrade complete")


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))

    for index_name in _NEW_INDEXES:
        op.execute(f"DROP INDEX IF EXISTS {index_name}")
