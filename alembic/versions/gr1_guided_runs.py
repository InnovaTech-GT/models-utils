"""provisioning_run.mode + guided_timeout_seconds (guided runs, doc 50)

Revision ID: gr1_guided_runs
Revises: pd2_payment_day_backfill
Create Date: 2026-10-14

Additive: two NOT NULL columns with server defaults, so every existing run
reads AUTOMATIC / 1800 and old code that never writes them keeps working.
`mode` is a plain VARCHAR(16) (ProvisioningRunMode in Python, no PG enum);
the 300..14400 timeout range is validated by create_run, not the DB.

Hand-written, house style: lock_timeout, idempotent, post-upgrade assert.
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy.sql import text

revision: str = "gr1_guided_runs"
down_revision: Union[str, Sequence[str], None] = "pd2_payment_day_backfill"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))
    op.execute("ALTER TABLE provisioning_run ADD COLUMN IF NOT EXISTS "
               "mode VARCHAR(16) NOT NULL DEFAULT 'AUTOMATIC'")
    op.execute("ALTER TABLE provisioning_run ADD COLUMN IF NOT EXISTS "
               "guided_timeout_seconds INTEGER NOT NULL DEFAULT 1800")
    found = connection.execute(text(
        "SELECT count(*) FROM information_schema.columns WHERE table_name = 'provisioning_run' "
        "AND column_name IN ('mode', 'guided_timeout_seconds')"
    )).scalar()
    if found != 2:
        raise RuntimeError("[gr1] provisioning_run.mode / guided_timeout_seconds missing after upgrade")


def downgrade() -> None:
    op.execute("ALTER TABLE provisioning_run DROP COLUMN IF EXISTS guided_timeout_seconds")
    op.execute("ALTER TABLE provisioning_run DROP COLUMN IF EXISTS mode")
