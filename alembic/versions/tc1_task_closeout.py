"""uplink-mobile: task_closeout + Task.scheduled_date/job_kind

Revision ID: tc1_task_closeout
Revises: rs1_route_cash
Create Date: 2026-08-18

Doc: docs/superpowers/plans/2026-08-18-uplink-mobile-real-data-integration.md
"Backend & DB Changes" — migration group tc1_task_closeout (apps/tecnicos).
Extends Task (scheduled_date, job_kind) rather than forking a parallel "Job"
model; task_closeout is one-to-one with task via the unique FK.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'tc1_task_closeout'
down_revision: Union[str, Sequence[str], None] = 'rs1_route_cash'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    connection = op.get_bind()
    # ALTER TABLE ADD COLUMN does not implicitly create the PG enum type the
    # way CREATE TABLE does (c2a_catalog_merge precedent) — create it first,
    # then reference it with create_type=False so add_column doesn't try
    # (and fail on) a second CREATE TYPE.
    sa.Enum('INSTALL', 'FAULT', 'CHANGE', 'REMOVE', name='taskjobkind').create(
        connection, checkfirst=True
    )

    op.add_column('task', sa.Column('scheduled_date', sa.Date(), nullable=True))
    op.add_column(
        'task',
        sa.Column(
            'job_kind',
            sa.Enum('INSTALL', 'FAULT', 'CHANGE', 'REMOVE', name='taskjobkind', create_type=False),
            nullable=True,
        ),
    )
    op.create_index(
        'ix_task_company_scheduled_date', 'task', ['company_id', 'scheduled_date'],
        postgresql_where=sa.text('scheduled_date IS NOT NULL'),
    )

    op.create_table(
        'task_closeout',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('checklist_state', sa.JSON(), nullable=True),
        sa.Column('serial_number', sa.String(), nullable=True),
        sa.Column('device_match', sa.JSON(), nullable=True),
        sa.Column('gps_lat', sa.Float(), nullable=True),
        sa.Column('gps_lng', sa.Float(), nullable=True),
        sa.Column('gps_accuracy_m', sa.Float(), nullable=True),
        sa.Column('gps_captured_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('signer_name', sa.String(), nullable=True),
        sa.Column('signer_id_number', sa.String(), nullable=True),
        sa.Column('submitted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('task_id', sa.Uuid(), nullable=False),
        sa.Column('technician_id', sa.Uuid(), nullable=False),
        sa.Column('signature_file_id', sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(['task_id'], ['task.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['technician_id'], ['user.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['signature_file_id'], ['uploaded_file.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('task_id', name='uq_task_closeout_task_id'),
    )
    op.create_index('ix_task_closeout_technician_id', 'task_closeout', ['technician_id'])


def downgrade() -> None:
    op.drop_index('ix_task_closeout_technician_id', table_name='task_closeout')
    op.drop_table('task_closeout')

    op.drop_index('ix_task_company_scheduled_date', table_name='task')
    op.drop_column('task', 'job_kind')
    op.drop_column('task', 'scheduled_date')
    sa.Enum(name='taskjobkind').drop(op.get_bind(), checkfirst=True)
