"""Registro de actividad: ix_audit_log_created_at (created_at DESC)

Revision ID: al1_audit_log_created_idx
Revises: pm1_payment_evidence
Create Date: 2026-09-16

Figma redesign PR 7 (docs/design/plans/07-registro-actividad.md §2.1, master
plan §2.10 / §4 row 8). One index, nothing else.

`audit_log` already holds everything the activity timeline renders, so PR 7
adds no tables, no columns and no audit rows. What it does add is a page that
reads the table the way nothing did before: ORDER BY created_at DESC with
skip/limit, on every page load. Today that is a full scan + sort of the whole
audit history.

Deliberately NOT here:
- `audit_log.company_id`. Tenant scoping stays the `user_id IN (users of
  company)` join. The column would mean backfilling every historic row plus
  touching create_audit_log and ~120 call sites across two services, and it
  only buys visibility of `user_id IS NULL` system rows that the timeline does
  not show (07 §2.3).
- `ix_audit_log_user_created (user_id, created_at DESC)`. The tenant filter is
  an IN-subquery that Postgres applies on top of the created_at scan, and the
  page size is <= 50.
  # ponytail: single created_at index; add the (user_id, created_at) composite
  # if EXPLAIN shows a heap-scan blowup past ~1M rows.

`IF NOT EXISTS` means an operator may pre-create the index with CREATE INDEX
CONCURRENTLY on a large production table and have this revision no-op.

Fully reversible. Hand-written (NOT autogenerate), ba1 house style:
lock_timeout, IF NOT EXISTS, post-upgrade assertion.
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy.sql import text


# revision identifiers, used by Alembic.
revision: str = 'al1_audit_log_created_idx'
down_revision: Union[str, Sequence[str], None] = 'pm1_payment_evidence'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_NEW_INDEXES = ("ix_audit_log_created_at",)


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))

    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_audit_log_created_at "
        "ON audit_log (created_at DESC)"
    )

    # --- assertions ---------------------------------------------------------
    for index_name in _NEW_INDEXES:
        if connection.execute(
            text("SELECT to_regclass(:name)"), {"name": index_name}
        ).scalar() is None:
            raise RuntimeError(
                f"[al1] expected index '{index_name}' to exist after upgrade"
            )

    # DESC is the whole point (the timeline is newest-first); a plain ASC index
    # would still be usable but a pre-created CONCURRENTLY index built the
    # wrong way round would silently pass the existence check above.
    descending = connection.execute(text(
        "SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_audit_log_created_at'"
    )).scalar()
    if descending and "DESC" not in descending:
        raise RuntimeError(
            f"[al1] ix_audit_log_created_at is not DESC: {descending}"
        )

    print("[al1_audit_log_created_idx] upgrade complete")


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))

    for index_name in _NEW_INDEXES:
        op.execute(f"DROP INDEX IF EXISTS {index_name}")
