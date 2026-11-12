"""collectionvisitcode gains 'noMoney' (doc 68 §2, collector reasons & promises)

Revision ID: vn1_visit_no_money
Revises: et1_edge_telnet
Create Date: 2026-11-12

Additive and irreversible: one enum label, added with ALTER TYPE ... ADD VALUE
in an autocommit block (the nc1a recipe). An older backend-erp that lacks
CollectionVisitCode.NO_MONEY raises LookupError when it loads a `noMoney`
visit row, so the consumers must be pinned to a models-utils that has it; the
founder-only remedy for a rollback is in doc 68 §2.4.

Downgrade is a documented no-op: PG cannot drop an enum label without
rebuilding the type, and nothing writes it once the code is gone.
Hand-written, house style: lock_timeout, IF [NOT] EXISTS.
"""
from collections.abc import Sequence

from sqlalchemy.sql import text

from alembic import op

revision: str = "vn1_visit_no_money"
down_revision: str | Sequence[str] | None = "et1_edge_telnet"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    with op.get_context().autocommit_block():
        op.get_bind().execute(text("ALTER TYPE collectionvisitcode ADD VALUE IF NOT EXISTS 'noMoney'"))


def downgrade() -> None:
    """No-op: the label stays (see the module docstring)."""
