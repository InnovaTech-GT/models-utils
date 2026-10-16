"""company.provisioning_zone: per-tenant provisioning worker routing

Revision ID: pz1_provisioning_zone
Revises: pw1_provisioning_event
Create Date: 2026-10-15

Workers move to per-tenant DigitalOcean droplets (one worker per tenant inside
that tenant's ZeroTier network namespace); the Railway worker keeps every
other tenant. A worker started with PROVISIONING_ZONE=<z> claims only
companies whose provisioning_zone = <z>; one with it unset claims only
provisioning_zone IS NULL — every tenant today.

company  + provisioning_zone VARCHAR(64) NULL

Additive, metadata-only (nullable, no default). Downgrade drops the column.
Hand-written, house style: lock_timeout, IF [NOT] EXISTS.
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy.sql import text

revision: str = "pz1_provisioning_zone"
down_revision: Union[str, Sequence[str], None] = "pw1_provisioning_event"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    c = op.get_bind()
    c.execute(text("SET lock_timeout = '5s'"))
    c.execute(text("ALTER TABLE company ADD COLUMN IF NOT EXISTS provisioning_zone VARCHAR(64)"))


def downgrade() -> None:
    c = op.get_bind()
    c.execute(text("SET lock_timeout = '5s'"))
    c.execute(text("ALTER TABLE company DROP COLUMN IF EXISTS provisioning_zone"))
