"""Usage-based pricing: one plan priced by active clients (doc 61).

Columns: tier.included_clients / extra_client_price / quote_above_clients,
subscription.period_peak_clients / custom_monthly_price, billing_invoice
period_start / period_end / billed_clients.

Data (one-way): insert tier "Uplink" (Q800 incl. 1,000 clients, Q0.80 per
extra client, quote above 5,000, monthly, every module, no resource caps),
deactivate every other tier, move every company and subscription onto it and
force subscriptions to MONTHLY. Downgrade drops the columns only; the tier
move is not reverted (old tiers stay, inactive).

Revision ID: up1_usage_pricing
Revises: sb1_billing_exceptions
"""
import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'up1_usage_pricing'
down_revision: Union[str, Sequence[str], None] = 'sb1_billing_exceptions'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TIER_NAME = 'Uplink'
ALL_MODULES = ["core", "admin", "management", "automations",
               "inventory", "topologies", "provisioning"]
FEATURES = {"max_users": -1, "max_products": -1, "max_clients": -1, "support": "Email"}


def upgrade() -> None:
    op.add_column('tier', sa.Column('included_clients', sa.Integer(), nullable=True))
    op.add_column('tier', sa.Column('extra_client_price', sa.Float(), nullable=True))
    op.add_column('tier', sa.Column('quote_above_clients', sa.Integer(), nullable=True))
    op.add_column('subscription', sa.Column(
        'period_peak_clients', sa.Integer(), nullable=False, server_default='0'))
    op.add_column('subscription', sa.Column('custom_monthly_price', sa.Float(), nullable=True))
    op.add_column('billing_invoice', sa.Column('period_start', sa.DateTime(timezone=True), nullable=True))
    op.add_column('billing_invoice', sa.Column('period_end', sa.DateTime(timezone=True), nullable=True))
    op.add_column('billing_invoice', sa.Column('billed_clients', sa.Integer(), nullable=True))

    conn = op.get_bind()
    conn.execute(sa.text(
        "INSERT INTO tier (id, created_at, name, price, price_yearly, billing_cycle, features, modules, "
        "                  is_active, included_clients, extra_client_price, quote_above_clients) "
        "SELECT gen_random_uuid(), now(), :name, 800, NULL, 'MONTHLY', CAST(:features AS json), "
        "       CAST(:modules AS json), true, 1000, 0.80, 5000 "
        "WHERE NOT EXISTS (SELECT 1 FROM tier WHERE name = :name)"
    ), {"name": TIER_NAME, "features": json.dumps(FEATURES), "modules": json.dumps(ALL_MODULES)})
    # Re-upgrade after a downgrade: the row survives, its pricing columns do not.
    conn.execute(sa.text(
        "UPDATE tier SET included_clients = 1000, extra_client_price = 0.80, quote_above_clients = 5000 "
        "WHERE name = :name AND included_clients IS NULL"
    ), {"name": TIER_NAME})
    conn.execute(sa.text("UPDATE tier SET is_active = false WHERE name <> :name"), {"name": TIER_NAME})
    conn.execute(sa.text(
        "UPDATE company SET tier_id = (SELECT id FROM tier WHERE name = :name)"
    ), {"name": TIER_NAME})
    conn.execute(sa.text(
        "UPDATE subscription SET tier_id = (SELECT id FROM tier WHERE name = :name), billing_cycle = 'MONTHLY'"
    ), {"name": TIER_NAME})


def downgrade() -> None:
    op.drop_column('billing_invoice', 'billed_clients')
    op.drop_column('billing_invoice', 'period_end')
    op.drop_column('billing_invoice', 'period_start')
    op.drop_column('subscription', 'custom_monthly_price')
    op.drop_column('subscription', 'period_peak_clients')
    op.drop_column('tier', 'quote_above_clients')
    op.drop_column('tier', 'extra_client_price')
    op.drop_column('tier', 'included_clients')
