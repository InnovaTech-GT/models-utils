"""SNMP traffic: router last-poll status (doc 74)

Revision ID: tm1_traffic_match
Revises: sm1_snmp_traffic_metrics
Create Date: 2026-11-19

Additive. inventory_item.traffic_last_poll (JSON, NULL): the snmp-poller's
last post per router, written by backend-erp POST /traffic/samples and read by
the traffic settings preview. The match template and the source live in the
existing company.traffic_settings JSON (no DDL). client_service.pppoe_username
is no longer mapped by the ORM; tm2_drop_pppoe_username drops it once no
deployed code maps it. Hand-written, house style: lock_timeout, IF [NOT] EXISTS.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "tm1_traffic_match"
down_revision: str | Sequence[str] | None = "sm1_snmp_traffic_metrics"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("ALTER TABLE inventory_item ADD COLUMN IF NOT EXISTS traffic_last_poll JSON")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("ALTER TABLE inventory_item DROP COLUMN IF EXISTS traffic_last_poll")
