"""cycle 4: insight_dashboard + insight_chart tables

Revision ID: c4a_insights_dashboards
Revises: c3b_device_categories
Create Date: 2026-07-07

Insights (Cycle 4): tenant-defined dashboards of simple charts driven off
existing entities (clients, orders, client_services, ...). Purely additive —
two new tables, no changes to existing schema. Available to every tenant (no
tier module gate; permissions/roles are seeded, not gated by ISP_TIER_MODULES).

insight_chart intentionally has NO company_id — tenant scope derives via
insight_chart.dashboard_id -> insight_dashboard.company_id (same
scoping-through-parent shape as topology_device_type -> topology).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c4a_insights_dashboards'
down_revision: Union[str, Sequence[str], None] = 'c3b_device_categories'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'insight_dashboard',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('ordering', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('company_id', sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(['company_id'], ['company.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('company_id', 'name', name='uq_insight_dashboard_company_name'),
    )
    op.create_index('ix_insight_dashboard_company_id', 'insight_dashboard', ['company_id'])

    op.create_table(
        'insight_chart',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('title', sa.String(), nullable=False),
        sa.Column('chart_type', sa.Enum('NUMBER', 'BAR', 'PIE', name='insightcharttype'), nullable=False),
        sa.Column('spec', sa.JSON(), nullable=False),
        sa.Column('ordering', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('dashboard_id', sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(['dashboard_id'], ['insight_dashboard.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_insight_chart_dashboard_id', 'insight_chart', ['dashboard_id'])
    print("[c4a_insights_dashboards] upgrade complete")


def downgrade() -> None:
    op.drop_index('ix_insight_chart_dashboard_id', table_name='insight_chart')
    op.drop_table('insight_chart')
    op.execute("DROP TYPE IF EXISTS insightcharttype")

    op.drop_index('ix_insight_dashboard_company_id', table_name='insight_dashboard')
    op.drop_table('insight_dashboard')
    print("[c4a_insights_dashboards] downgrade complete")
