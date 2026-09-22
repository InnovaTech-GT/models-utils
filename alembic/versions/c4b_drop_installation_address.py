"""cycle 4: drop client.installation_address

Revision ID: c4b_drop_installation_address
Revises: c4a_insights_dashboards
Create Date: 2026-07-07

`client.installation_address` (added by revision cd2f0076c709, the
isp-platform-core migration) was intended to be distinct from the billing
`address` column but was never populated/used separately in practice —
entity merge Cycle 2/3 consolidated on a single `address` per client. Safe to
drop: no production data (the column carries no rows worth preserving; every
consuming reference — model, schema, workflow field allowlist, seeded
templates — is removed in this same release).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c4b_drop_installation_address'
down_revision: Union[str, Sequence[str], None] = 'c4a_insights_dashboards'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column('client', 'installation_address')


def downgrade() -> None:
    op.add_column('client', sa.Column('installation_address', sa.String(), nullable=True))
