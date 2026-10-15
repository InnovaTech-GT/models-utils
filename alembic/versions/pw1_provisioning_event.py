"""provisioning_event outbox (provisioning worker split R2, design §3.2)

Revision ID: pw1_provisioning_event
Revises: gr1_guided_runs
Create Date: 2026-10-15

Additive: one new table plus its partial pending index. The worker and
models-utils state-machine code insert a row in the same transaction as the
state change; backend-erp drains them in id order. run_id / job_id are plain
UUIDs (no FK) so an event outlives its run or job. Downgrade drops the table
(pending events are lost; drain first).

Hand-written, house style: lock_timeout, idempotent, post-upgrade assert.
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy.sql import text

revision: str = "pw1_provisioning_event"
down_revision: Union[str, Sequence[str], None] = "gr1_guided_runs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))
    op.execute("""
        CREATE TABLE IF NOT EXISTS provisioning_event (
            id BIGSERIAL PRIMARY KEY,
            company_id UUID NOT NULL REFERENCES company (id) ON DELETE CASCADE,
            kind VARCHAR(32) NOT NULL,
            run_id UUID NULL,
            job_id UUID NULL,
            payload JSONB NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            processed_at TIMESTAMPTZ NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT NULL
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_provisioning_event_pending "
               "ON provisioning_event (id) WHERE processed_at IS NULL")
    found = connection.execute(text(
        "SELECT count(*) FROM pg_indexes WHERE tablename = 'provisioning_event' "
        "AND indexname = 'ix_provisioning_event_pending'"
    )).scalar()
    if found != 1:
        raise RuntimeError("[pw1] provisioning_event / ix_provisioning_event_pending missing after upgrade")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS provisioning_event")
