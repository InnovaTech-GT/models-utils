"""ZTP always on: drop provisioning_settings.ztp_enabled

Revision ID: zt3_ztp_always_on
Revises: zm1_manual_step
Create Date: 2026-10-09

Founder decision 2026-10-09: ZTP is always on for every tenant, so the zt1
tenant switch goes. The INSTALL closeout always starts the ACTIVATION run; the
safety mechanisms are the provisioning gates (DRY_RUN_REQUIRED, ...) and the
env PROVISIONING_KILL_SWITCH, not a column.

provisioning_settings  - ztp_enabled

Destructive, but safe: zt1 only ever reached Railway development (not prod),
and nothing reads the column once the consuming code drops it. Downgrade
re-adds it NOT NULL DEFAULT false (metadata-only), i.e. every tenant off —
the zt1 shape. Hand-written, house style: lock_timeout, IF [NOT] EXISTS.
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy.sql import text

revision: str = "zt3_ztp_always_on"
down_revision: Union[str, Sequence[str], None] = "zm1_manual_step"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    c = op.get_bind()
    c.execute(text("SET lock_timeout = '5s'"))
    c.execute(text("ALTER TABLE provisioning_settings DROP COLUMN IF EXISTS ztp_enabled"))


def downgrade() -> None:
    c = op.get_bind()
    c.execute(text("SET lock_timeout = '5s'"))
    c.execute(text(
        "ALTER TABLE provisioning_settings "
        "ADD COLUMN IF NOT EXISTS ztp_enabled BOOLEAN NOT NULL DEFAULT false"
    ))
