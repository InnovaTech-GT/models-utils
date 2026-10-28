"""Device category MICRONODE (Cable over GPON receiver at the customer)

Revision ID: pa2_micronode_category
Revises: pa1_plan_network_automation
Create Date: 2026-10-28

Doc 56 §2: a Cable GPON plan allows one CPE, the micronode, which is tracked in
inventory and recovered on cancel but never configured. New platform-global
category `MICRONODE` ("Micronodo"): EDGE (it sits at the customer), passive
(nothing is configured on it), active (tenants can create a product with it).
isp_seed.DEVICE_CATEGORIES carries the same row; this revision inserts it so
an existing database gets it at upgrade, not only a fresh seed. Additive,
ON CONFLICT DO NOTHING. Downgrade deletes the row only while no device type
or plan references it (RESTRICT FKs would refuse otherwise).
"""
from typing import Sequence, Union

from sqlalchemy.sql import text

from alembic import op

revision: str = "pa2_micronode_category"
down_revision: Union[str, Sequence[str], None] = "pa1_plan_network_automation"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Pinned against isp_seed.DEVICE_CATEGORIES by tests/test_plan_network_automation.py.
MICRONODE = ("MICRONODE", "Micronodo", 45, "EDGE", True, "box", True)


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    key, name, sort_order, tier, is_passive, icon, is_active = MICRONODE
    op.get_bind().execute(
        text(
            "INSERT INTO device_category (id, key, name, sort_order, tier, is_passive, icon, is_active, is_system, created_at, updated_at) "
            "VALUES (gen_random_uuid(), :key, :name, :sort_order, :tier, :is_passive, :icon, :is_active, TRUE, now(), now()) "
            "ON CONFLICT (key) DO NOTHING"
        ),
        {"key": key, "name": name, "sort_order": sort_order, "tier": tier,
         "is_passive": is_passive, "icon": icon, "is_active": is_active},
    )
    assert op.get_bind().execute(
        text("SELECT count(*) FROM device_category WHERE key = 'MICRONODE'")
    ).scalar() == 1


def downgrade() -> None:
    op.execute(
        "DELETE FROM device_category d WHERE d.key = 'MICRONODE' "
        "AND NOT EXISTS (SELECT 1 FROM device_type t WHERE t.category_id = d.id) "
        "AND NOT EXISTS (SELECT 1 FROM service_plan_device_category p WHERE p.device_category_id = d.id)"
    )
