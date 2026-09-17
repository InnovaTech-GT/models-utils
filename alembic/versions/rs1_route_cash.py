"""uplink-mobile: collection_route / route_stop / collection_visit / cash_session

Revision ID: rs1_route_cash
Revises: uf1_uploaded_file
Create Date: 2026-08-18

Doc: docs/superpowers/plans/2026-08-18-uplink-mobile-real-data-integration.md
"Backend & DB Changes" — migration group rs1_route_cash (apps/cobros).

Payment.user_id/collected_by check (per doc §rs1 note): Payment already has
`received_by` (FK -> user.id, nullable, added in c1c_payment_ledger) —
covers cash-cut aggregation by collector. No new column added here.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'rs1_route_cash'
down_revision: Union[str, Sequence[str], None] = 'uf1_uploaded_file'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'collection_route',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('route_date', sa.Date(), nullable=False),
        sa.Column(
            'status', sa.Enum('OPEN', 'CLOSED', name='collectionroutestatus'),
            server_default='OPEN', nullable=False,
        ),
        sa.Column('company_id', sa.Uuid(), nullable=False),
        sa.Column('collector_id', sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(['company_id'], ['company.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['collector_id'], ['user.id'], ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_collection_route_company_id', 'collection_route', ['company_id'])
    op.create_index('ix_collection_route_collector_id', 'collection_route', ['collector_id'])
    op.create_index('ix_collection_route_company_date', 'collection_route', ['company_id', 'route_date'])

    op.create_table(
        'route_stop',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('sequence', sa.Integer(), nullable=False),
        sa.Column(
            'status',
            sa.Enum('PENDING', 'VISITED', 'NO_CONTACT', name='routestopstatus'),
            server_default='PENDING', nullable=False,
        ),
        sa.Column('route_id', sa.Uuid(), nullable=False),
        sa.Column('client_id', sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(['route_id'], ['collection_route.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['client_id'], ['client.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_route_stop_route_id', 'route_stop', ['route_id'])
    op.create_index('ix_route_stop_client_id', 'route_stop', ['client_id'])

    op.create_table(
        'collection_visit',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            'outcome',
            sa.Enum('PAID', 'PARTIAL', 'NO_CONTACT', name='collectionvisitoutcome'),
            nullable=False,
        ),
        sa.Column(
            'visit_code',
            sa.Enum(
                'noOneHome', 'promise', 'refused', 'complaint', 'moved',
                name='collectionvisitcode',
            ),
            nullable=True,
        ),
        sa.Column('promise_date', sa.Date(), nullable=True),
        sa.Column('note', sa.String(), nullable=True),
        sa.Column('route_stop_id', sa.Uuid(), nullable=False),
        sa.Column('payment_id', sa.Uuid(), nullable=True),
        sa.Column('signature_file_id', sa.Uuid(), nullable=True),
        sa.Column('created_by', sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(['route_stop_id'], ['route_stop.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['payment_id'], ['payment.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['signature_file_id'], ['uploaded_file.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['created_by'], ['user.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_collection_visit_route_stop_id', 'collection_visit', ['route_stop_id'])

    op.create_table(
        'cash_session',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('opened_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('closed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('counted_cash_cents', sa.BigInteger(), nullable=True),
        sa.Column(
            'status', sa.Enum('OPEN', 'CLOSED', name='cashsessionstatus'),
            server_default='OPEN', nullable=False,
        ),
        sa.Column('company_id', sa.Uuid(), nullable=False),
        sa.Column('collector_id', sa.Uuid(), nullable=False),
        sa.Column('route_id', sa.Uuid(), nullable=True),
        sa.Column('deposit_slip_photo_id', sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(['company_id'], ['company.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['collector_id'], ['user.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['route_id'], ['collection_route.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['deposit_slip_photo_id'], ['uploaded_file.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_cash_session_company_id', 'cash_session', ['company_id'])
    op.create_index('ix_cash_session_collector_id', 'cash_session', ['collector_id'])
    op.create_index('ix_cash_session_company_collector', 'cash_session', ['company_id', 'collector_id'])


def downgrade() -> None:
    op.drop_index('ix_cash_session_company_collector', table_name='cash_session')
    op.drop_index('ix_cash_session_collector_id', table_name='cash_session')
    op.drop_index('ix_cash_session_company_id', table_name='cash_session')
    op.drop_table('cash_session')
    sa.Enum(name='cashsessionstatus').drop(op.get_bind(), checkfirst=True)

    op.drop_index('ix_collection_visit_route_stop_id', table_name='collection_visit')
    op.drop_table('collection_visit')
    sa.Enum(name='collectionvisitcode').drop(op.get_bind(), checkfirst=True)
    sa.Enum(name='collectionvisitoutcome').drop(op.get_bind(), checkfirst=True)

    op.drop_index('ix_route_stop_client_id', table_name='route_stop')
    op.drop_index('ix_route_stop_route_id', table_name='route_stop')
    op.drop_table('route_stop')
    sa.Enum(name='routestopstatus').drop(op.get_bind(), checkfirst=True)

    op.drop_index('ix_collection_route_company_date', table_name='collection_route')
    op.drop_index('ix_collection_route_collector_id', table_name='collection_route')
    op.drop_index('ix_collection_route_company_id', table_name='collection_route')
    op.drop_table('collection_route')
    sa.Enum(name='collectionroutestatus').drop(op.get_bind(), checkfirst=True)
