"""Conversational workflows (doc 64 / plan 64a §2)

Revision ID: wf2_conversational
Revises: wf1_workflow_engine_v2
Create Date: 2026-11-04

Additive, no backfill (server defaults cover existing rows). workflow_execution
gains awaited_key (the chat a run waits on; partial UNIQUE while WAITING = C1,
one waiting run per chat), the cancel marker (cancel_requested_at,
cancel_code) and outbox_delivered; workflow_step_execution gains messages_sent
(C5 cap sum, partial index). No enum change: SUPERSEDED / CANCELLED_BY_CLIENT
are error_code values on a CANCELLED run. Downgrade drops the columns (and so
their indexes). Hand-written, house style: lock_timeout, IF [NOT] EXISTS.
"""
from collections.abc import Sequence

from sqlalchemy.sql import text

from alembic import op

revision: str = "wf2_conversational"
down_revision: str | Sequence[str] | None = "wf1_workflow_engine_v2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = (
    ("workflow_execution", "awaited_key", "VARCHAR(160)"),
    ("workflow_execution", "cancel_requested_at", "TIMESTAMPTZ"),
    ("workflow_execution", "cancel_code", "VARCHAR(32)"),
    ("workflow_execution", "outbox_delivered", "INTEGER NOT NULL DEFAULT 0"),
    ("workflow_step_execution", "messages_sent", "SMALLINT NOT NULL DEFAULT 0"),
)


def upgrade() -> None:
    c = op.get_bind()
    c.execute(text("SET lock_timeout = '5s'"))
    for table, col, ddl in _COLUMNS:
        c.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {ddl}"))
    c.execute(text(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_workflow_execution_awaited ON workflow_execution (awaited_key) "
        "WHERE status = 'WAITING' AND awaited_key IS NOT NULL"
    ))
    c.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_step_execution_sent ON workflow_step_execution (started_at) "
        "WHERE messages_sent > 0"
    ))


def downgrade() -> None:
    c = op.get_bind()
    c.execute(text("SET lock_timeout = '5s'"))
    # Dropping a column drops its indexes.
    for table, col, _ in reversed(_COLUMNS):
        c.execute(text(f"ALTER TABLE {table} DROP COLUMN IF EXISTS {col}"))
