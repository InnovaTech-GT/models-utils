"""Recurrente tenant billing (prompt.md issue 4).

Adds the Recurrente gateway columns for tenant-pays-Uplink checkout:
tier product/price ids (NULL = not purchasable online), the company's lazy
customer id, subscription linkage + card display fields, an invoice-level
payment-intent idempotency key, and the billing_webhook_event svix-id
idempotency log. Purely additive.

Revision ID: rb1_recurrente_billing
Revises: cf1_drop_client_install_fields
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'rb1_recurrente_billing'
down_revision: Union[str, Sequence[str], None] = 'cf1_drop_client_install_fields'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('tier', sa.Column('recurrente_product_id', sa.String(), nullable=True))
    op.add_column('tier', sa.Column('recurrente_price_id', sa.String(), nullable=True))
    op.add_column('tier', sa.Column('recurrente_price_yearly_id', sa.String(), nullable=True))

    op.add_column('company', sa.Column('recurrente_customer_id', sa.String(), nullable=True))

    op.add_column('subscription', sa.Column('recurrente_subscription_id', sa.String(), nullable=True))
    op.create_unique_constraint(
        'uq_subscription_recurrente_subscription_id', 'subscription', ['recurrente_subscription_id']
    )
    op.add_column('subscription', sa.Column('recurrente_checkout_id', sa.String(), nullable=True))
    op.add_column('subscription', sa.Column('card_last4', sa.String(), nullable=True))
    op.add_column('subscription', sa.Column('card_brand', sa.String(), nullable=True))

    op.add_column('billing_invoice', sa.Column('recurrente_intent_id', sa.String(), nullable=True))
    op.create_unique_constraint(
        'uq_billing_invoice_recurrente_intent_id', 'billing_invoice', ['recurrente_intent_id']
    )

    op.create_table(
        'billing_webhook_event',
        sa.Column('svix_id', sa.String(), primary_key=True),
        sa.Column('event_type', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table('billing_webhook_event')
    op.drop_constraint('uq_billing_invoice_recurrente_intent_id', 'billing_invoice', type_='unique')
    op.drop_column('billing_invoice', 'recurrente_intent_id')
    op.drop_column('subscription', 'card_brand')
    op.drop_column('subscription', 'card_last4')
    op.drop_column('subscription', 'recurrente_checkout_id')
    op.drop_constraint('uq_subscription_recurrente_subscription_id', 'subscription', type_='unique')
    op.drop_column('subscription', 'recurrente_subscription_id')
    op.drop_column('company', 'recurrente_customer_id')
    op.drop_column('tier', 'recurrente_price_yearly_id')
    op.drop_column('tier', 'recurrente_price_id')
    op.drop_column('tier', 'recurrente_product_id')
