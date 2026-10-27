"""po1_payment_promise_office_source: PaymentPromiseSource gains OFFICE

Revision ID: po1_payment_promise_office_source
Revises: cp1_dunning
Create Date: 2026-10-27

Additive: backoffice (web) promises are recorded with source OFFICE.
"""
from alembic import op

revision = "po1_payment_promise_office_source"
down_revision = "cp1_dunning"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("SET lock_timeout = '5s'")
    # Native-enum ADD VALUE runs in an autocommit_block (doc 16 §2.0).
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE paymentpromisesource ADD VALUE IF NOT EXISTS 'OFFICE'")


def downgrade() -> None:
    """No-op: PostgreSQL cannot drop an enum value; an unused OFFICE value is harmless."""
