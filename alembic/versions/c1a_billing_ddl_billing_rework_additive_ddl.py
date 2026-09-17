"""billing rework R1: additive DDL — cents columns, order billing state, snapshots

Revision ID: c1a_billing_ddl
Revises: cd2f0076c709
Create Date: 2026-07-05

Doc 16 §2.4 R1. Additive DDL only; metadata-only on PG11+ for the defaulted
enum columns. Creates the four new enum types (no ALTER TYPE hazards), adds
every column of §2.2, and swaps order_item.product_id's FK to SET NULL so
deleting a Product never destroys billed history.

All *_cents columns are nullable with NO server_default (doc 16 §1 —
server_default='0' would silently corrupt revenue and defeat the IS NULL
backfill guards in R2). NOT NULL tightening arrives in R4 (separate branch,
after the new backend is verified live in prod).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'c1a_billing_ddl'
down_revision: Union[str, Sequence[str], None] = 'cd2f0076c709'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema (additive only)."""
    # Fail fast instead of queueing behind long-running locks (doc 16 §6.h).
    op.execute("SET lock_timeout = '5s'")

    # --- New enum types (brand-new — no ALTER TYPE ADD VALUE hazards) ---
    # paymentkind/paymentmethodtype are consumed by the payment table in R3;
    # created here per §2.4 R1 so all billing types share one owner/downgrade.
    sa.Enum('PENDING', 'PARTIAL', 'PAID', 'REFUNDED',
            name='paymentstatus').create(op.get_bind(), checkfirst=True)
    sa.Enum('RECURRING', 'ONE_SHOT', 'INSTALLATION',
            name='ordertype').create(op.get_bind(), checkfirst=True)
    sa.Enum('PAYMENT', 'REFUND',
            name='paymentkind').create(op.get_bind(), checkfirst=True)
    sa.Enum('CASH', 'TRANSFER', 'CARD', 'DEPOSIT', 'OTHER', 'LEGACY',
            name='paymentmethodtype').create(op.get_bind(), checkfirst=True)

    # --- order ---
    op.add_column('order', sa.Column(
        'order_type',
        postgresql.ENUM(name='ordertype', create_type=False),
        server_default='ONE_SHOT', nullable=False,
    ))
    op.add_column('order', sa.Column(
        'payment_status',
        postgresql.ENUM(name='paymentstatus', create_type=False),
        server_default='PENDING', nullable=False,
    ))
    op.add_column('order', sa.Column('total_cents', sa.BigInteger(), nullable=True))
    op.add_column('order', sa.Column('client_service_id', sa.Uuid(), nullable=True))
    op.create_foreign_key(
        'fk_order_client_service', 'order', 'client_service',
        ['client_service_id'], ['id'], ondelete='SET NULL',
    )
    op.create_index(op.f('ix_order_client_service_id'), 'order', ['client_service_id'], unique=False)

    # --- order_item: snapshots + product FK swap (CASCADE/NOT NULL -> SET NULL/NULL) ---
    op.add_column('order_item', sa.Column('unit_price_cents', sa.BigInteger(), nullable=True))
    op.add_column('order_item', sa.Column('product_name', sa.String(), nullable=True))
    op.alter_column('order_item', 'product_id', existing_type=sa.Uuid(), nullable=True)
    op.drop_constraint('order_item_product_id_fkey', 'order_item', type_='foreignkey')
    op.create_foreign_key(
        'fk_order_item_product', 'order_item', 'product',
        ['product_id'], ['id'], ondelete='SET NULL',
    )

    # --- invoice ---
    op.add_column('invoice', sa.Column('subtotal_cents', sa.BigInteger(), nullable=True))
    op.add_column('invoice', sa.Column('tax_cents', sa.BigInteger(), nullable=True))
    op.add_column('invoice', sa.Column('total_cents', sa.BigInteger(), nullable=True))

    # --- catalog money shadows ---
    op.add_column('product', sa.Column('price_cents', sa.BigInteger(), nullable=True))
    op.add_column('service_plan', sa.Column('price_cents', sa.BigInteger(), nullable=True))
    op.add_column('inventory_item', sa.Column('cost_cents', sa.BigInteger(), nullable=True))


def downgrade() -> None:
    """Drop the added columns/types and restore the original product FK.

    NOTE: restoring NOT NULL + CASCADE on order_item.product_id fails if any
    row was already SET-NULLed by a product deletion — expected; those rows
    must be resolved manually before downgrading (Float originals untouched,
    doc 16 §7)."""
    op.execute("SET lock_timeout = '5s'")

    op.drop_column('inventory_item', 'cost_cents')
    op.drop_column('service_plan', 'price_cents')
    op.drop_column('product', 'price_cents')

    op.drop_column('invoice', 'total_cents')
    op.drop_column('invoice', 'tax_cents')
    op.drop_column('invoice', 'subtotal_cents')

    op.drop_constraint('fk_order_item_product', 'order_item', type_='foreignkey')
    op.alter_column('order_item', 'product_id', existing_type=sa.Uuid(), nullable=False)
    op.create_foreign_key(
        'order_item_product_id_fkey', 'order_item', 'product',
        ['product_id'], ['id'], ondelete='CASCADE',
    )
    op.drop_column('order_item', 'product_name')
    op.drop_column('order_item', 'unit_price_cents')

    op.drop_index(op.f('ix_order_client_service_id'), table_name='order')
    op.drop_constraint('fk_order_client_service', 'order', type_='foreignkey')
    op.drop_column('order', 'client_service_id')
    op.drop_column('order', 'total_cents')
    op.drop_column('order', 'payment_status')
    op.drop_column('order', 'order_type')

    for enum_name in ('paymentmethodtype', 'paymentkind', 'ordertype', 'paymentstatus'):
        sa.Enum(name=enum_name).drop(op.get_bind(), checkfirst=True)
