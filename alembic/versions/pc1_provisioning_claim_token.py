"""provisioning_job.claim_token: the worker's per-claim fence token

Revision ID: pc1_provisioning_claim_token
Revises: vw1_viewer_no_credential_read
Create Date: 2026-10-06

Provisioning concurrency fix (release-v1.0.0/provisioning-concurrency). The
worker writes a fresh UUID here on every claim and makes every later write to
the row conditional on (id, status='RUNNING', claim_token), so a reaped or
superseded executor's settle writes nothing. NULL whenever the job is not
executing.

Additive and metadata-only (a nullable column with no default): no rewrite, no
backfill, no index. uq_provisioning_job_device_lock,
uq_provisioning_job_company_idem and uq_provisioning_run_company_idem are
untouched.
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy.sql import text

revision: str = "pc1_provisioning_claim_token"
down_revision: Union[str, Sequence[str], None] = "vw1_viewer_no_credential_read"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    connection = op.get_bind()
    # The live worker polls provisioning_job every second; never queue behind it.
    connection.execute(text("SET lock_timeout = '5s'"))
    connection.execute(text(
        "ALTER TABLE provisioning_job ADD COLUMN IF NOT EXISTS claim_token UUID NULL"
    ))


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))
    connection.execute(text(
        "ALTER TABLE provisioning_job DROP COLUMN IF EXISTS claim_token"
    ))
