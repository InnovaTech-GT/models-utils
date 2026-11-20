"""SNMP traffic: drop client_service.pppoe_username (doc 74 §9)

Revision ID: tm2_drop_pppoe_username
Revises: tm1_traffic_match
Create Date: 2026-11-19

Destructive, step 2 of 2. tm1 (6.36.0) stopped mapping the column; it is
dropped once every deployed consumer runs a pin of 6.36.0 or later (backend-erp,
auth-erp, and the provisioning worker on Railway and on the relay droplet).
Traffic is matched by the tenant's match template, never by a stored PPPoE user.
Downgrade re-adds the empty column and its partial unique index (no data).
"""
from collections.abc import Sequence

from alembic import op

revision: str = "tm2_drop_pppoe_username"
down_revision: str | Sequence[str] | None = "tm1_traffic_match"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("DROP INDEX IF EXISTS uq_client_service_pppoe")
    op.execute("ALTER TABLE client_service DROP COLUMN IF EXISTS pppoe_username")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("ALTER TABLE client_service ADD COLUMN IF NOT EXISTS pppoe_username VARCHAR(64)")
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_client_service_pppoe ON client_service "
        "(company_id, pppoe_username) WHERE pppoe_username IS NOT NULL AND status <> 'CANCELLED'"
    )
