"""cycle 2 topology rework: topology + topology_device_type, client_service.topology_id, service_plan.default_topology_id

Revision ID: c2c_topology
Revises: c2b_service_billing
Create Date: 2026-07-06

Doc 18-cycle2-design.md D5 + 18a-cycle2-design-appendix.md topology-networking
§1. Purely additive — two new tables, two new nullable FK columns. No data to
backfill (this is new functionality, not an absorption of an existing table).

Topology = named ordered chain of device types (a join table, not JSONB —
appendix §1 rationale: FK integrity on delete, the resolution algorithm is a
plain join, chains are tiny/per-company). Bound to the playbook that
provisions it (RESTRICT, mirrors provisioning_job.playbook_id).
client_service.topology_id RESTRICT (deleting a topology in use is a 409,
never a silent unlink); service_plan.default_topology_id SET NULL (D5:
pre-fills a new service's topology; a plan must never block deleting one).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.sql import text


# revision identifiers, used by Alembic.
revision: str = 'c2c_topology'
down_revision: Union[str, Sequence[str], None] = 'c2b_service_billing'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))

    op.create_table(
        'topology',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('description', sa.String(), nullable=True),
        sa.Column('is_active', sa.Boolean(), server_default='true', nullable=False),
        sa.Column('company_id', sa.Uuid(), nullable=False),
        sa.Column('playbook_id', sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(['company_id'], ['company.id'], ondelete='CASCADE'),
        # RESTRICT: mirrors provisioning_job.playbook_id — the router DELETE
        # guard on playbooks.py extends to referencing topologies.
        sa.ForeignKeyConstraint(['playbook_id'], ['playbook.id'], ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('company_id', 'name', name='uq_topology_company_name'),
    )
    op.create_index(op.f('ix_topology_company_id'), 'topology', ['company_id'], unique=False)

    op.create_table(
        'topology_device_type',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('position', sa.Integer(), nullable=False),
        sa.Column('topology_id', sa.Uuid(), nullable=False),
        sa.Column('device_type_id', sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(['topology_id'], ['topology.id'], ondelete='CASCADE'),
        # RESTRICT: a device type referenced by a topology chain cannot be
        # deleted out from under it (matches inventory_item.device_type_id).
        sa.ForeignKeyConstraint(['device_type_id'], ['device_type.id'], ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('topology_id', 'position', name='uq_topology_position'),
        # One occurrence of a type per chain: D5 resolves concrete devices BY
        # TYPE, so a duplicated type in one chain would be ambiguous by
        # construction.
        sa.UniqueConstraint('topology_id', 'device_type_id', name='uq_topology_device_type'),
    )

    op.add_column('client_service', sa.Column('topology_id', sa.Uuid(), nullable=True))
    op.create_foreign_key(
        'fk_client_service_topology', 'client_service', 'topology',
        ['topology_id'], ['id'], ondelete='RESTRICT',
    )
    op.create_index('ix_client_service_topology_id', 'client_service', ['topology_id'])

    op.add_column('service_plan', sa.Column('default_topology_id', sa.Uuid(), nullable=True))
    op.create_foreign_key(
        'fk_service_plan_default_topology', 'service_plan', 'topology',
        ['default_topology_id'], ['id'], ondelete='SET NULL',
    )


def downgrade() -> None:
    """Drop cleanly — no data to preserve (new functionality this cycle)."""
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))

    op.drop_constraint('fk_service_plan_default_topology', 'service_plan', type_='foreignkey')
    op.drop_column('service_plan', 'default_topology_id')

    op.drop_index('ix_client_service_topology_id', table_name='client_service')
    op.drop_constraint('fk_client_service_topology', 'client_service', type_='foreignkey')
    op.drop_column('client_service', 'topology_id')

    op.drop_table('topology_device_type')
    op.drop_index(op.f('ix_topology_company_id'), table_name='topology')
    op.drop_table('topology')
