"""grandfather email_verified for users predating login enforcement

Revision ID: c1f_verify_grandfather
Revises: c1e_install_actions
Create Date: 2026-07-05

auth-erp (develop) rejects login with 403 when user.email_verified is false.
Production's auth-erp (main) never enforced this, so every existing prod user
— including the internal system-cron@internal.erp account, which can never
receive a verification email — still has the column at its migration default
(false). Promoting auth-erp without this backfill locks out the entire live
tenant and silently kills the recurring-order cron.

Data-only, idempotent, re-runnable. Downgrade is an intentional no-op: the
pre-backfill per-user state is unknowable, and revoking trust from active
production users has no consumer benefit.
"""
from typing import Sequence, Union

from alembic import op


revision: str = 'c1f_verify_grandfather'
down_revision: Union[str, Sequence[str], None] = 'c1e_install_actions'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    result = op.get_bind().exec_driver_sql(
        'UPDATE "user" SET email_verified = TRUE WHERE NOT email_verified'
    )
    print(f"[c1f_verify_grandfather] users grandfathered: {result.rowcount}")


def downgrade() -> None:
    pass
