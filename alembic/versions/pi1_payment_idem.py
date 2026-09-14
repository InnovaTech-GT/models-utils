"""uplink-mobile: Payment.idempotency_key

Revision ID: pi1_payment_idem
Revises: tc1_task_closeout
Create Date: 2026-08-18

Doc: docs/superpowers/plans/2026-08-18-uplink-mobile-real-data-integration.md
"Backend & DB Changes" — migration group pi1_payment_idem. Mirrors the
existing pattern on ProvisioningJob.idempotency_key
(database_utils/models/isp.py, uq_provisioning_job_company_idem) — the
single highest-leverage change for offline-write safety from
apps/cobros. PaymentService.record_payment/record_full_payment
check-then-insert or catch the unique-violation IntegrityError and return
the existing row instead of double-recording money.

Partial unique index (not a plain UniqueConstraint) so any number of rows
with idempotency_key IS NULL (every payment recorded before this feature,
and every non-mobile payment) coexist without tripping the constraint.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'pi1_payment_idem'
down_revision: Union[str, Sequence[str], None] = 'tc1_task_closeout'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('payment', sa.Column('idempotency_key', sa.String(), nullable=True))
    op.create_index(
        'uq_payment_company_idem', 'payment', ['company_id', 'idempotency_key'],
        unique=True,
        postgresql_where=sa.text('idempotency_key IS NOT NULL'),
    )


def downgrade() -> None:
    op.drop_index('uq_payment_company_idem', table_name='payment')
    op.drop_column('payment', 'idempotency_key')
