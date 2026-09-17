"""uplink-mobile: uploaded_file (shared polymorphic photo/signature store)

Revision ID: uf1_uploaded_file
Revises: t2_paid_tier_network_modules
Create Date: 2026-08-18

Doc: docs/superpowers/plans/2026-08-18-uplink-mobile-real-data-integration.md
"Backend & DB Changes" — migration group uf1_uploaded_file.

Shared, polymorphic file store (photos/signatures) backing both
apps/cobros and apps/tecnicos closeout evidence. Reuses the
linked_object_type/linked_object_id pattern already established on Task
(routers/tasks.py:52-58) as owner_type/owner_id here. Created FIRST in this
feature's migration group because task_closeout/collection_visit/
cash_session all carry nullable FKs into it.

storage_key is a relative path under a Railway volume, not a URL —
`# ponytail: single-instance volume storage, not multi-replica-safe — move
storage_key's backing implementation to S3-compatible object storage the
moment backend-erp scales to >1 replica.`
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'uf1_uploaded_file'
down_revision: Union[str, Sequence[str], None] = 't2_paid_tier_network_modules'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'uploaded_file',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            'owner_type',
            sa.Enum('TASK_CLOSEOUT', 'COLLECTION_VISIT', 'CASH_SESSION', name='uploadedfileownertype'),
            nullable=False,
        ),
        sa.Column('owner_id', sa.Uuid(), nullable=False),
        sa.Column('kind', sa.Enum('PHOTO', 'SIGNATURE', name='uploadedfilekind'), nullable=False),
        sa.Column('storage_key', sa.String(), nullable=False),
        sa.Column('content_type', sa.String(), nullable=False),
        sa.Column('size_bytes', sa.Integer(), nullable=False),
        sa.Column('company_id', sa.Uuid(), nullable=False),
        sa.Column('uploaded_by', sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(['company_id'], ['company.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['uploaded_by'], ['user.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_uploaded_file_company_id', 'uploaded_file', ['company_id'])
    op.create_index('ix_uploaded_file_owner', 'uploaded_file', ['owner_type', 'owner_id'])


def downgrade() -> None:
    op.drop_index('ix_uploaded_file_owner', table_name='uploaded_file')
    op.drop_index('ix_uploaded_file_company_id', table_name='uploaded_file')
    op.drop_table('uploaded_file')
    sa.Enum(name='uploadedfilekind').drop(op.get_bind(), checkfirst=True)
    sa.Enum(name='uploadedfileownertype').drop(op.get_bind(), checkfirst=True)
