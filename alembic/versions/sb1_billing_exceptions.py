"""SaaS billing exceptions + pay-by-invoice checkout (doc 60).

Per-tenant billing exception columns on subscription (discount percent, its
end date, a superadmin note) and the latest Recurrente pay-this-invoice
checkout id on billing_invoice. Purely additive.

Revision ID: sb1_billing_exceptions
Revises: sv1_service_entity
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'sb1_billing_exceptions'
down_revision: Union[str, Sequence[str], None] = 'sv1_service_entity'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('subscription', sa.Column('discount_percent', sa.Integer(), nullable=False, server_default='0'))
    op.add_column('subscription', sa.Column('discount_until', sa.DateTime(timezone=True), nullable=True))
    op.add_column('subscription', sa.Column('billing_note', sa.Text(), nullable=True))
    op.create_check_constraint(
        'ck_subscription_discount_percent', 'subscription',
        'discount_percent >= 0 AND discount_percent <= 100',
    )
    op.add_column('billing_invoice', sa.Column('recurrente_checkout_id', sa.String(), nullable=True))
    op.create_index(
        'ix_billing_invoice_recurrente_checkout_id', 'billing_invoice', ['recurrente_checkout_id']
    )


def downgrade() -> None:
    op.drop_index('ix_billing_invoice_recurrente_checkout_id', table_name='billing_invoice')
    op.drop_column('billing_invoice', 'recurrente_checkout_id')
    op.drop_constraint('ck_subscription_discount_percent', 'subscription', type_='check')
    op.drop_column('subscription', 'billing_note')
    op.drop_column('subscription', 'discount_until')
    op.drop_column('subscription', 'discount_percent')
