"""Grandfather existing users as email-verified.

The auth overhaul hard-blocks login until email_verified is true. Users
created before the overhaul never went through the confirmation flow (all
production users carry email_verified = false), so without this backfill
the release would lock every existing user out.

One-shot data migration: verifies every user that exists when it runs.
Users created afterwards go through the normal confirmation flow.

Revision ID: t2_grandfather_email_verified
Revises: t1_free_trial_unlimited
"""
from typing import Sequence, Union

from alembic import op

revision: str = 't2_grandfather_email_verified'
down_revision: Union[str, Sequence[str], None] = 't1_free_trial_unlimited'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute('UPDATE "user" SET email_verified = TRUE WHERE email_verified = FALSE')


def downgrade() -> None:
    # Irreversible data backfill: which users were unverified beforehand is
    # not recorded. Leaving everyone verified is the safe direction.
    pass
