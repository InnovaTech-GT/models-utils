"""Plan network automation: service_plan.network_automation + allowed CPE
categories, taskjobkind += RECONNECT, permission client_services.cancel

Revision ID: pa1_plan_network_automation
Revises: po1_promise_office_source
Create Date: 2026-10-28

Doc 56. A plan says whether the network is driven by provisioning
(`network_automation = true`) or by technicians through field work orders
(false), and which CPE device categories may be installed on it. Additive:

- `service_plan.network_automation` BOOLEAN NOT NULL DEFAULT true: every
  existing plan keeps today's automated behaviour (new plans default to false
  at the API schema, not here).
- new table `service_plan_device_category` (plan CASCADE, category RESTRICT,
  composite PK; no company_id — device_category is platform-global),
  backfilled with ONU for every SERVICE plan.
- `taskjobkind` += RECONNECT (the technician reconnects a manual plan). Added
  in an autocommit_block first; no statement here names the label.
- permission `client_services.cancel`, granted to the global ADMIN role only
  (isp_seed carries the row; no ISP base role holds it).

Downgrade drops the table and the column. The enum label (PG cannot drop one)
and the permission row stay — harmless, the Python enum and the routers decide
what is used. Hand-written, house style: lock_timeout, IF [NOT] EXISTS,
post-upgrade asserts.
"""
from typing import Sequence, Union

from sqlalchemy.sql import text

from alembic import op

revision: str = "pa1_plan_network_automation"
down_revision: Union[str, Sequence[str], None] = "po1_promise_office_source"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Pinned against isp_seed.ISP_PERMISSIONS by tests/test_plan_network_automation.py.
CANCEL_PERMISSION = {
    "name": "client_services.cancel",
    "resource": "client_services",
    "action": "cancel",
    "description": "Cancel a subscriber service",
}


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))

    # tj1 precedent: ADD VALUE must commit before anything could use it.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE taskjobkind ADD VALUE IF NOT EXISTS 'RECONNECT'")

    op.execute(
        "ALTER TABLE service_plan ADD COLUMN IF NOT EXISTS "
        "network_automation BOOLEAN NOT NULL DEFAULT true"
    )
    op.execute("""
        CREATE TABLE IF NOT EXISTS service_plan_device_category (
            service_plan_id UUID NOT NULL REFERENCES service_plan (id) ON DELETE CASCADE,
            device_category_id UUID NOT NULL REFERENCES device_category (id) ON DELETE RESTRICT,
            PRIMARY KEY (service_plan_id, device_category_id)
        )
    """)
    op.execute("""
        INSERT INTO service_plan_device_category (service_plan_id, device_category_id)
        SELECT sp.id, dc.id FROM service_plan sp CROSS JOIN device_category dc
        WHERE sp.kind = 'SERVICE' AND dc.key = 'ONU'
        ON CONFLICT DO NOTHING
    """)

    connection.execute(
        text(
            "INSERT INTO permission (id, created_at, name, resource, action, description) "
            "VALUES (gen_random_uuid(), NOW(), :name, :resource, :action, :description) "
            "ON CONFLICT (name) DO NOTHING"
        ),
        CANCEL_PERMISSION,
    )
    connection.execute(
        text(
            "INSERT INTO role_permission (role_id, permission_id) "
            "SELECT r.id, p.id FROM role r, permission p "
            "WHERE r.name = 'ADMIN' AND r.company_id IS NULL AND p.name = :perm "
            "ON CONFLICT DO NOTHING"
        ),
        {"perm": CANCEL_PERMISSION["name"]},
    )

    if connection.execute(text(
        "SELECT 1 FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid "
        "WHERE t.typname = 'taskjobkind' AND e.enumlabel = 'RECONNECT'"
    )).scalar() is None:
        raise RuntimeError("[pa1] taskjobkind RECONNECT missing after upgrade")
    if connection.execute(text(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = 'service_plan' AND column_name = 'network_automation'"
    )).scalar() is None:
        raise RuntimeError("[pa1] service_plan.network_automation missing after upgrade")
    if connection.execute(text("SELECT to_regclass('service_plan_device_category')")).scalar() is None:
        raise RuntimeError("[pa1] service_plan_device_category missing after upgrade")


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))
    op.execute("DROP TABLE IF EXISTS service_plan_device_category")
    op.execute("ALTER TABLE service_plan DROP COLUMN IF EXISTS network_automation")
    # RECONNECT label and the client_services.cancel row stay (see docstring).
