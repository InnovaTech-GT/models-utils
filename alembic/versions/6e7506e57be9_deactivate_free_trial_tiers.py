"""deactivate Free and Trial tiers (Recurrente paywall)

Data-only migration. Uplink billing now requires Recurrente checkout for
every company — there is no free version or free trial. Deactivates the
"Free" and "Trial" tiers so they no longer surface as assignable options for
new companies (tiers.py's GET /public already filters on is_active). Existing
Tier rows are NOT deleted: companies/subscriptions may still reference them
by FK.

Revision ID: 6e7506e57be9
Revises: pi1_payment_idem
Create Date: 2026-08-20

"""
from alembic import op


# revision identifiers, used by Alembic.
revision = "6e7506e57be9"
down_revision = "pi1_payment_idem"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE tier SET is_active = false WHERE name IN ('Free', 'Trial')")


def downgrade() -> None:
    op.execute("UPDATE tier SET is_active = true WHERE name IN ('Free', 'Trial')")
