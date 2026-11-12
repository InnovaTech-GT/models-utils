"""Client groups: client_group, client.client_group_id, dunning_run.mode OFF (doc 68 §1)

Revision ID: cg1_client_groups
Revises: et1_edge_telnet
Create Date: 2026-11-11

Additive, no backfill: every client starts ungrouped (= the tenant's dunning
settings). New table client_group (name, palette colour key, optional
auto_suspension_mode / min_overdue_orders overriding the tenant's), the nullable
client.client_group_id (FK SET NULL, indexed), and ck_dunning_run_mode widened
to allow 'OFF' (the tenant is OFF and a group's policy ran). One transactional
revision. Hand-written, house style: lock_timeout, IF [NOT] EXISTS.

Rollback caveat (doc 68 §1.4): additive, but NOT code-rollback-safe once used.
An older backend-erp reads dunning_run.mode as Literal["REPORT","ON"], so once
any run is stored with mode='OFF' its GET /dunning/runs answers 500. The remedy
is `UPDATE dunning_run SET mode='REPORT' WHERE mode='OFF';` (slightly lossy).
On production that SQL is founder-only. downgrade() runs the same mapping
before restoring the narrow CHECK.
"""
from collections.abc import Sequence

from sqlalchemy.sql import text

from alembic import op

revision: str = "cg1_client_groups"
down_revision: str | Sequence[str] | None = "et1_edge_telnet"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UPGRADE = (
    """
    CREATE TABLE IF NOT EXISTS client_group (
        id UUID PRIMARY KEY,
        created_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL,
        company_id UUID NOT NULL REFERENCES company(id) ON DELETE CASCADE,
        name VARCHAR(60) NOT NULL,
        color VARCHAR(10) NOT NULL DEFAULT 'grey',
        auto_suspension_mode VARCHAR(10),
        min_overdue_orders INTEGER,
        CONSTRAINT uq_client_group_company_name UNIQUE (company_id, name),
        CONSTRAINT ck_client_group_color CHECK (color IN ('grey', 'blue', 'green', 'yellow', 'red')),
        CONSTRAINT ck_client_group_mode CHECK (auto_suspension_mode IN ('OFF', 'REPORT', 'ON')),
        CONSTRAINT ck_client_group_min CHECK (min_overdue_orders >= 1)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_client_group_company_id ON client_group (company_id)",
    "ALTER TABLE client ADD COLUMN IF NOT EXISTS client_group_id UUID "
    "REFERENCES client_group(id) ON DELETE SET NULL",
    "CREATE INDEX IF NOT EXISTS ix_client_client_group ON client (client_group_id)",
    "ALTER TABLE dunning_run DROP CONSTRAINT IF EXISTS ck_dunning_run_mode",
    "ALTER TABLE dunning_run ADD CONSTRAINT ck_dunning_run_mode CHECK (mode IN ('OFF', 'REPORT', 'ON'))",
)

_DOWNGRADE = (
    "UPDATE dunning_run SET mode = 'REPORT' WHERE mode = 'OFF'",
    "ALTER TABLE dunning_run DROP CONSTRAINT IF EXISTS ck_dunning_run_mode",
    "ALTER TABLE dunning_run ADD CONSTRAINT ck_dunning_run_mode CHECK (mode IN ('REPORT', 'ON'))",
    "DROP INDEX IF EXISTS ix_client_client_group",
    "ALTER TABLE client DROP COLUMN IF EXISTS client_group_id",
    "DROP TABLE IF EXISTS client_group",
)


def _run(statements) -> None:
    c = op.get_bind()
    c.execute(text("SET lock_timeout = '5s'"))
    for sql in statements:
        c.execute(text(sql))


def upgrade() -> None:
    _run(_UPGRADE)


def downgrade() -> None:
    _run(_DOWNGRADE)
