"""isp platform core: service plans, subscriber services, inventory, topology, provisioning

Revision ID: cd2f0076c709
Revises: a1f2b3c4d5e6
Create Date: 2026-07-04 15:32:46.415785

Hand-adjusted after autogenerate:
- Tables reordered so FK targets exist first; the inventory_item <-> network_node
  cycle is broken by adding network_node.inventory_item_id's FK afterwards.
- ALTER TYPE ... ADD VALUE for the pre-existing enums (tasklinkedobjecttype,
  stepactiontype) — autogenerate does not detect enum member additions.
- client.assigned_technician FK named explicitly (downgrade needs the name).
- Seeds (ISP permissions/roles, tier modules, default node types, workflow
  templates) run from alembic/env.py -> seeds/isp_seed.py after every upgrade.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'cd2f0076c709'
down_revision: Union[str, Sequence[str], None] = 'a1f2b3c4d5e6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # --- Extend existing enums (PG 12+: allowed in a transaction as long as the
    # new values aren't used by this same transaction) ---
    op.execute("ALTER TYPE tasklinkedobjecttype ADD VALUE IF NOT EXISTS 'CLIENT_SERVICE'")
    op.execute("ALTER TYPE tasklinkedobjecttype ADD VALUE IF NOT EXISTS 'INVENTORY_ITEM'")
    op.execute("ALTER TYPE tasklinkedobjecttype ADD VALUE IF NOT EXISTS 'NETWORK_NODE'")
    op.execute("ALTER TYPE stepactiontype ADD VALUE IF NOT EXISTS 'ENQUEUE_PROVISIONING'")

    # --- Independent tables first ---
    op.create_table('workflow_template',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('key', sa.String(), nullable=False),
    sa.Column('name', sa.String(), nullable=False),
    sa.Column('description', sa.String(), nullable=True),
    sa.Column('category', sa.String(), nullable=True),
    sa.Column('definition', sa.JSON(), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('key')
    )
    op.create_table('device_type',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('name', sa.String(), nullable=False),
    sa.Column('category', sa.Enum('ROUTER', 'SWITCH', 'OLT', 'ONU', 'SPLITTER', 'SPLICE_CLOSURE', 'PATCH_PANEL', 'ACCESS_POINT', 'CPE_ROUTER', 'UPS', 'ANTENNA', 'RADIO', 'OTHER', name='devicecategory'), nullable=False),
    sa.Column('vendor', sa.String(), nullable=True),
    sa.Column('model', sa.String(), nullable=True),
    sa.Column('description', sa.String(), nullable=True),
    sa.Column('attribute_schema', sa.JSON(), nullable=True),
    sa.Column('default_attributes', sa.JSON(), nullable=True),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_device_type_company_id'), 'device_type', ['company_id'], unique=False)
    op.create_table('warehouse',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('name', sa.String(), nullable=False),
    sa.Column('address', sa.String(), nullable=True),
    sa.Column('is_vehicle', sa.Boolean(), nullable=False),
    sa.Column('notes', sa.String(), nullable=True),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_warehouse_company_id'), 'warehouse', ['company_id'], unique=False)
    op.create_table('network_node_type',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('key', sa.String(), nullable=False),
    sa.Column('name', sa.String(), nullable=False),
    sa.Column('category', sa.Enum('ROUTER', 'SWITCH', 'OLT', 'ONU', 'SPLITTER', 'SPLICE_CLOSURE', 'PATCH_PANEL', 'ACCESS_POINT', 'CPE_ROUTER', 'UPS', 'ANTENNA', 'RADIO', 'OTHER', name='devicecategory'), nullable=True),
    sa.Column('icon', sa.String(), nullable=True),
    sa.Column('allowed_parent_keys', sa.JSON(), nullable=True),
    sa.Column('attribute_schema', sa.JSON(), nullable=True),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('company_id', 'key', name='uq_network_node_type_company_key')
    )
    op.create_index(op.f('ix_network_node_type_company_id'), 'network_node_type', ['company_id'], unique=False)
    op.create_table('service_plan',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('name', sa.String(), nullable=False),
    sa.Column('description', sa.String(), nullable=True),
    sa.Column('plan_type', sa.Enum('FIBER', 'CABLE', 'WIRELESS', 'DSL', 'OTHER', name='serviceplantype'), nullable=False),
    sa.Column('download_mbps', sa.Integer(), nullable=True),
    sa.Column('upload_mbps', sa.Integer(), nullable=True),
    sa.Column('data_cap_gb', sa.Integer(), nullable=True),
    sa.Column('price', sa.Float(), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('provisioning_params', sa.JSON(), nullable=True),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('product_id', sa.Uuid(), nullable=True),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['product_id'], ['product.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_service_plan_company_id'), 'service_plan', ['company_id'], unique=False)
    op.create_table('playbook',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('name', sa.String(), nullable=False),
    sa.Column('description', sa.String(), nullable=True),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('target_vendor', sa.String(), nullable=True),
    sa.Column('target_category', sa.Enum('ROUTER', 'SWITCH', 'OLT', 'ONU', 'SPLITTER', 'SPLICE_CLOSURE', 'PATCH_PANEL', 'ACCESS_POINT', 'CPE_ROUTER', 'UPS', 'ANTENNA', 'RADIO', 'OTHER', name='devicecategory'), nullable=True),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('definition', sa.JSON(), nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('created_by', sa.Uuid(), nullable=True),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['created_by'], ['user.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_playbook_company_id'), 'playbook', ['company_id'], unique=False)

    # --- network_node (without the inventory_item FK: cycle broken below) ---
    op.create_table('network_node',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('name', sa.String(), nullable=False),
    sa.Column('status', sa.Enum('PLANNED', 'ACTIVE', 'DEGRADED', 'DOWN', 'MAINTENANCE', name='networknodestatus'), server_default='ACTIVE', nullable=False),
    sa.Column('latitude', sa.Float(), nullable=True),
    sa.Column('longitude', sa.Float(), nullable=True),
    sa.Column('capacity', sa.Integer(), nullable=True),
    sa.Column('attributes', sa.JSON(), nullable=True),
    sa.Column('notes', sa.String(), nullable=True),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('node_type_id', sa.Uuid(), nullable=False),
    sa.Column('parent_id', sa.Uuid(), nullable=True),
    sa.Column('inventory_item_id', sa.Uuid(), nullable=True),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['node_type_id'], ['network_node_type.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['parent_id'], ['network_node.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_network_node_company_id'), 'network_node', ['company_id'], unique=False)
    op.create_index(op.f('ix_network_node_parent_id'), 'network_node', ['parent_id'], unique=False)

    op.create_table('network_link',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('link_type', sa.String(), nullable=False),
    sa.Column('attributes', sa.JSON(), nullable=True),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('from_node_id', sa.Uuid(), nullable=False),
    sa.Column('to_node_id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['from_node_id'], ['network_node.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['to_node_id'], ['network_node.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_network_link_company_id'), 'network_link', ['company_id'], unique=False)

    # --- client_service (needs service_plan, network_node, recurring_order) ---
    op.create_table('client_service',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('status', sa.Enum('PENDING_INSTALL', 'ACTIVE', 'SUSPENDED', 'CANCELLED', name='clientservicestatus'), server_default='PENDING_INSTALL', nullable=False),
    sa.Column('activation_date', sa.DateTime(timezone=True), nullable=True),
    sa.Column('cancelled_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('connection_params', sa.JSON(), nullable=True),
    sa.Column('notes', sa.String(), nullable=True),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('client_id', sa.Uuid(), nullable=False),
    sa.Column('service_plan_id', sa.Uuid(), nullable=False),
    sa.Column('network_node_id', sa.Uuid(), nullable=True),
    sa.Column('recurring_order_id', sa.Uuid(), nullable=True),
    sa.ForeignKeyConstraint(['client_id'], ['client.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['network_node_id'], ['network_node.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['recurring_order_id'], ['recurring_order.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['service_plan_id'], ['service_plan.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_client_service_client_id'), 'client_service', ['client_id'], unique=False)
    op.create_index(op.f('ix_client_service_company_id'), 'client_service', ['company_id'], unique=False)
    op.create_index('ix_client_service_company_status', 'client_service', ['company_id', 'status'], unique=False)

    # --- inventory_item (needs device_type, warehouse, client, client_service, network_node) ---
    op.create_table('inventory_item',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('serial_number', sa.String(), nullable=True),
    sa.Column('mac_address', sa.String(), nullable=True),
    sa.Column('status', sa.Enum('IN_STOCK', 'RESERVED', 'INSTALLED', 'IN_REPAIR', 'RETIRED', 'LOST', name='inventoryitemstatus'), server_default='IN_STOCK', nullable=False),
    sa.Column('condition', sa.Enum('NEW', 'USED', 'REFURBISHED', 'DAMAGED', name='inventoryitemcondition'), server_default='NEW', nullable=False),
    sa.Column('attributes', sa.JSON(), nullable=True),
    sa.Column('purchase_date', sa.DateTime(timezone=True), nullable=True),
    sa.Column('warranty_until', sa.DateTime(timezone=True), nullable=True),
    sa.Column('cost', sa.Float(), nullable=True),
    sa.Column('notes', sa.String(), nullable=True),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('device_type_id', sa.Uuid(), nullable=False),
    sa.Column('warehouse_id', sa.Uuid(), nullable=True),
    sa.Column('client_id', sa.Uuid(), nullable=True),
    sa.Column('client_service_id', sa.Uuid(), nullable=True),
    sa.Column('network_node_id', sa.Uuid(), nullable=True),
    sa.ForeignKeyConstraint(['client_id'], ['client.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['client_service_id'], ['client_service.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['device_type_id'], ['device_type.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['network_node_id'], ['network_node.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['warehouse_id'], ['warehouse.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_inventory_item_company_id'), 'inventory_item', ['company_id'], unique=False)
    op.create_index('ix_inventory_item_company_status', 'inventory_item', ['company_id', 'status'], unique=False)
    op.create_index('uq_inventory_item_company_serial', 'inventory_item', ['company_id', 'serial_number'], unique=True, postgresql_where=sa.text('serial_number IS NOT NULL'))

    # Close the inventory_item <-> network_node cycle.
    op.create_foreign_key(
        'fk_network_node_inventory_item', 'network_node', 'inventory_item',
        ['inventory_item_id'], ['id'], ondelete='SET NULL',
    )

    op.create_table('service_suspension',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('suspended_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('reactivated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('reason', sa.Enum('NON_PAYMENT', 'CUSTOMER_REQUEST', 'MAINTENANCE', 'FRAUD', 'OTHER', name='suspensionreason'), nullable=False),
    sa.Column('note', sa.String(), nullable=True),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('client_service_id', sa.Uuid(), nullable=False),
    sa.Column('created_by', sa.Uuid(), nullable=True),
    sa.ForeignKeyConstraint(['client_service_id'], ['client_service.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['created_by'], ['user.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_service_suspension_client_service_id'), 'service_suspension', ['client_service_id'], unique=False)
    op.create_index(op.f('ix_service_suspension_company_id'), 'service_suspension', ['company_id'], unique=False)

    op.create_table('equipment_event',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('event_type', sa.Enum('RECEIVED', 'TRANSFERRED', 'RESERVED', 'INSTALLED', 'REPLACED', 'REMOVED', 'REPAIRED', 'RETIRED', 'MAINTENANCE', name='equipmenteventtype'), nullable=False),
    sa.Column('notes', sa.String(), nullable=True),
    sa.Column('event_metadata', sa.JSON(), nullable=True),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('item_id', sa.Uuid(), nullable=False),
    sa.Column('related_item_id', sa.Uuid(), nullable=True),
    sa.Column('from_warehouse_id', sa.Uuid(), nullable=True),
    sa.Column('to_warehouse_id', sa.Uuid(), nullable=True),
    sa.Column('client_id', sa.Uuid(), nullable=True),
    sa.Column('client_service_id', sa.Uuid(), nullable=True),
    sa.Column('technician_id', sa.Uuid(), nullable=True),
    sa.ForeignKeyConstraint(['client_id'], ['client.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['client_service_id'], ['client_service.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['from_warehouse_id'], ['warehouse.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['item_id'], ['inventory_item.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['related_item_id'], ['inventory_item.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['technician_id'], ['user.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['to_warehouse_id'], ['warehouse.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_equipment_event_company_id'), 'equipment_event', ['company_id'], unique=False)
    op.create_index(op.f('ix_equipment_event_item_id'), 'equipment_event', ['item_id'], unique=False)

    op.create_table('provisioning_job',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('status', sa.Enum('QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'ROLLED_BACK', 'CANCELLED', name='provisioningjobstatus'), server_default='QUEUED', nullable=False),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('max_attempts', sa.Integer(), nullable=False),
    sa.Column('idempotency_key', sa.String(), nullable=True),
    sa.Column('scheduled_for', sa.DateTime(timezone=True), nullable=True),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('variables', sa.JSON(), nullable=True),
    sa.Column('log', sa.JSON(), nullable=True),
    sa.Column('error', sa.String(), nullable=True),
    sa.Column('triggered_by', sa.Enum('USER', 'WORKFLOW', 'API', name='provisioningtrigger'), server_default='USER', nullable=False),
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('playbook_id', sa.Uuid(), nullable=False),
    sa.Column('client_service_id', sa.Uuid(), nullable=True),
    sa.Column('network_node_id', sa.Uuid(), nullable=True),
    sa.Column('inventory_item_id', sa.Uuid(), nullable=True),
    sa.Column('integration_id', sa.Uuid(), nullable=True),
    sa.Column('triggered_by_user_id', sa.Uuid(), nullable=True),
    sa.ForeignKeyConstraint(['client_service_id'], ['client_service.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['company_id'], ['company.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['integration_id'], ['integration.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['inventory_item_id'], ['inventory_item.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['network_node_id'], ['network_node.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['playbook_id'], ['playbook.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['triggered_by_user_id'], ['user.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_provisioning_job_claim', 'provisioning_job', ['status', 'scheduled_for', 'created_at'], unique=False, postgresql_where=sa.text("status = 'QUEUED'"))
    op.create_index(op.f('ix_provisioning_job_client_service_id'), 'provisioning_job', ['client_service_id'], unique=False)
    op.create_index(op.f('ix_provisioning_job_company_id'), 'provisioning_job', ['company_id'], unique=False)
    op.create_index('uq_provisioning_job_company_idem', 'provisioning_job', ['company_id', 'idempotency_key'], unique=True, postgresql_where=sa.text("idempotency_key IS NOT NULL AND status IN ('QUEUED','RUNNING')"))

    # --- client ISP columns (ADR-004) ---
    # add_column does not auto-create enum types (create_table does).
    sa.Enum('UNKNOWN', 'SERVICEABLE', 'NOT_SERVICEABLE', 'SURVEY_REQUIRED',
            name='serviceavailability').create(op.get_bind(), checkfirst=True)
    sa.Enum('NOT_INSTALLED', 'SURVEY_SCHEDULED', 'INSTALL_SCHEDULED', 'INSTALLED', 'CANCELLED',
            name='installationstatus').create(op.get_bind(), checkfirst=True)
    op.add_column('client', sa.Column('latitude', sa.Float(), nullable=True))
    op.add_column('client', sa.Column('longitude', sa.Float(), nullable=True))
    op.add_column('client', sa.Column('gps_precision_m', sa.Float(), nullable=True))
    op.add_column('client', sa.Column('installation_address', sa.String(), nullable=True))
    op.add_column('client', sa.Column('service_availability', sa.Enum('UNKNOWN', 'SERVICEABLE', 'NOT_SERVICEABLE', 'SURVEY_REQUIRED', name='serviceavailability'), server_default='UNKNOWN', nullable=False))
    op.add_column('client', sa.Column('installation_status', sa.Enum('NOT_INSTALLED', 'SURVEY_SCHEDULED', 'INSTALL_SCHEDULED', 'INSTALLED', 'CANCELLED', name='installationstatus'), server_default='NOT_INSTALLED', nullable=False))
    op.add_column('client', sa.Column('installation_date', sa.DateTime(timezone=True), nullable=True))
    op.add_column('client', sa.Column('assigned_technician_id', sa.Uuid(), nullable=True))
    op.create_foreign_key('fk_client_assigned_technician', 'client', 'user', ['assigned_technician_id'], ['id'], ondelete='SET NULL')


def downgrade() -> None:
    """Downgrade schema. (Seed rows for permissions/roles/templates are removed;
    per-company node types drop with their table.)"""
    op.drop_constraint('fk_client_assigned_technician', 'client', type_='foreignkey')
    op.drop_column('client', 'assigned_technician_id')
    op.drop_column('client', 'installation_date')
    op.drop_column('client', 'installation_status')
    op.drop_column('client', 'service_availability')
    op.drop_column('client', 'installation_address')
    op.drop_column('client', 'gps_precision_m')
    op.drop_column('client', 'longitude')
    op.drop_column('client', 'latitude')

    op.drop_index('uq_provisioning_job_company_idem', table_name='provisioning_job', postgresql_where=sa.text("idempotency_key IS NOT NULL AND status IN ('QUEUED','RUNNING')"))
    op.drop_index(op.f('ix_provisioning_job_company_id'), table_name='provisioning_job')
    op.drop_index(op.f('ix_provisioning_job_client_service_id'), table_name='provisioning_job')
    op.drop_index('ix_provisioning_job_claim', table_name='provisioning_job', postgresql_where=sa.text("status = 'QUEUED'"))
    op.drop_table('provisioning_job')
    op.drop_index(op.f('ix_equipment_event_item_id'), table_name='equipment_event')
    op.drop_index(op.f('ix_equipment_event_company_id'), table_name='equipment_event')
    op.drop_table('equipment_event')
    op.drop_index(op.f('ix_service_suspension_company_id'), table_name='service_suspension')
    op.drop_index(op.f('ix_service_suspension_client_service_id'), table_name='service_suspension')
    op.drop_table('service_suspension')

    op.drop_constraint('fk_network_node_inventory_item', 'network_node', type_='foreignkey')
    op.drop_index('uq_inventory_item_company_serial', table_name='inventory_item', postgresql_where=sa.text('serial_number IS NOT NULL'))
    op.drop_index('ix_inventory_item_company_status', table_name='inventory_item')
    op.drop_index(op.f('ix_inventory_item_company_id'), table_name='inventory_item')
    op.drop_table('inventory_item')

    op.drop_index('ix_client_service_company_status', table_name='client_service')
    op.drop_index(op.f('ix_client_service_company_id'), table_name='client_service')
    op.drop_index(op.f('ix_client_service_client_id'), table_name='client_service')
    op.drop_table('client_service')

    op.drop_index(op.f('ix_network_link_company_id'), table_name='network_link')
    op.drop_table('network_link')
    op.drop_index(op.f('ix_network_node_parent_id'), table_name='network_node')
    op.drop_index(op.f('ix_network_node_company_id'), table_name='network_node')
    op.drop_table('network_node')

    op.drop_index(op.f('ix_playbook_company_id'), table_name='playbook')
    op.drop_table('playbook')
    op.drop_index(op.f('ix_service_plan_company_id'), table_name='service_plan')
    op.drop_table('service_plan')
    op.drop_index(op.f('ix_network_node_type_company_id'), table_name='network_node_type')
    op.drop_table('network_node_type')
    op.drop_index(op.f('ix_warehouse_company_id'), table_name='warehouse')
    op.drop_table('warehouse')
    op.drop_index(op.f('ix_device_type_company_id'), table_name='device_type')
    op.drop_table('device_type')
    op.drop_table('workflow_template')

    # Remove seeded RBAC rows (role_permission rows cascade via FKs).
    op.execute(
        "DELETE FROM role WHERE company_id IS NULL AND name IN "
        "('TECHNICIAN', 'NOC', 'WAREHOUSE', 'SUPPORT', 'BILLING')"
    )
    op.execute(
        "DELETE FROM permission WHERE resource IN "
        "('service_plans', 'client_services', 'device_types', 'warehouses', "
        "'inventory_items', 'equipment_events', 'network_node_types', "
        "'network_nodes', 'network_links', 'playbooks', 'provisioning', "
        "'workflow_templates')"
    )

    # Drop the enum types created by this revision.
    for enum_name in (
        'provisioningtrigger', 'provisioningjobstatus', 'equipmenteventtype',
        'inventoryitemcondition', 'inventoryitemstatus', 'networknodestatus',
        'suspensionreason', 'clientservicestatus', 'serviceplantype',
        'devicecategory', 'serviceavailability', 'installationstatus',
    ):
        sa.Enum(name=enum_name).drop(op.get_bind(), checkfirst=True)
    # Note: values added to tasklinkedobjecttype / stepactiontype are not
    # removable (PostgreSQL has no ALTER TYPE DROP VALUE) — harmless leftovers.
