"""Security audit #68: quantity >= 1 on order_item and client_service

Revision ID: sec1_quantity_checks
Revises: dm1_device_metrics
Create Date: 2026-11-24

Additive. Two CHECKs backstop the schemas (OrderItem* / ClientService* refuse
quantity < 1) for writers that bypass them (xlsx import, catalog_resolution).
The upgrade first counts offending rows in both tables and refuses with the
counts, so it never half-applies; fix the data, then re-run. Downgrade drops
both CHECKs.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "sec1_quantity_checks"
down_revision: str | Sequence[str] | None = "dm1_device_metrics"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CHECKS = (
    ("order_item", "ck_order_item_quantity_positive"),
    ("client_service", "ck_client_service_quantity_positive"),
)


def upgrade() -> None:
    bind = op.get_bind()
    bad = {
        table: bind.execute(sa.text(f"SELECT count(*) FROM {table} WHERE quantity < 1")).scalar()
        for table, _ in _CHECKS
    }
    if any(bad.values()):
        raise RuntimeError(
            "sec1_quantity_checks: rows with quantity < 1 must be fixed first: "
            + ", ".join(f"{t}={n}" for t, n in bad.items())
        )
    op.execute("SET LOCAL lock_timeout = '5s'")
    for table, name in _CHECKS:
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}")
        op.execute(f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK (quantity >= 1)")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    for table, name in _CHECKS:
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}")
