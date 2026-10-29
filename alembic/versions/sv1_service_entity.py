"""Service entity: service, service_device_category, service_plan.service_id

Revision ID: sv1_service_entity
Revises: pa2_micronode_category
Create Date: 2026-10-29

Doc 58. Network automation and allowed CPEs belong to the *service* the tenant
sells ("Internet GPON", "Cable"), not to each plan. Additive:

- `service` (company CASCADE, unique (company_id, name), `service_type` reuses
  the existing PG enum `serviceplantype`, `network_automation` default false).
- `service_device_category` (service CASCADE, category RESTRICT, composite PK).
- `service_plan.service_id` NULL FK RESTRICT + index (NULL only for
  INSTALLATION/HARDWARE plans).
- Backfill: one service per (company, service_group) over SERVICE-kind plans
  (NULL/blank group -> label of the plan_type: Internet/Cable/Inalámbrico/DSL/
  Otro); service_type = most common plan_type; network_automation = bool_or of
  its plans (never turns an automated customer manual); allowed categories =
  union of the plans' `service_plan_device_category` rows; each plan gets
  service_id and service_group = service name. plan_type is NOT rewritten.
  Seeds nothing.

Deprecated, kept one release (rollback safety): service_plan.network_automation,
service_plan_device_category, service_plan.service_group. Downgrade drops only
what this revision added. Hand-written, house style: lock_timeout, IF [NOT]
EXISTS, idempotent backfill (only plans with service_id IS NULL), post-upgrade
asserts.
"""
from typing import Sequence, Union

from sqlalchemy.sql import text

from alembic import op

revision: str = "sv1_service_entity"
down_revision: Union[str, Sequence[str], None] = "pa2_micronode_category"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# The group a plan lands in: its trimmed service_group, else its type's label.
_GROUP = (
    "COALESCE(NULLIF(btrim(sp.service_group), ''), CASE sp.plan_type::text "
    "WHEN 'FIBER' THEN 'Internet' WHEN 'CABLE' THEN 'Cable' "
    "WHEN 'WIRELESS' THEN 'Inalámbrico' WHEN 'DSL' THEN 'DSL' ELSE 'Otro' END)"
)


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))

    op.execute("""
        CREATE TABLE IF NOT EXISTS service (
            id UUID PRIMARY KEY,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            company_id UUID NOT NULL REFERENCES company (id) ON DELETE CASCADE,
            name VARCHAR(100) NOT NULL,
            service_type serviceplantype NOT NULL DEFAULT 'FIBER',
            network_automation BOOLEAN NOT NULL DEFAULT false,
            CONSTRAINT uq_service_company_name UNIQUE (company_id, name)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_service_company_id ON service (company_id)")
    op.execute("""
        CREATE TABLE IF NOT EXISTS service_device_category (
            service_id UUID NOT NULL REFERENCES service (id) ON DELETE CASCADE,
            device_category_id UUID NOT NULL REFERENCES device_category (id) ON DELETE RESTRICT,
            PRIMARY KEY (service_id, device_category_id)
        )
    """)
    op.execute(
        "ALTER TABLE service_plan ADD COLUMN IF NOT EXISTS "
        "service_id UUID REFERENCES service (id) ON DELETE RESTRICT"
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_service_plan_service_id ON service_plan (service_id)")

    # Backfill. A re-run only touches plans still without a service.
    op.execute(f"""
        INSERT INTO service (id, company_id, name, service_type, network_automation)
        SELECT gen_random_uuid(), g.company_id, g.gname,
               mode() WITHIN GROUP (ORDER BY g.plan_type), bool_or(g.network_automation)
        FROM (
            SELECT sp.company_id, {_GROUP} AS gname, sp.plan_type, sp.network_automation
            FROM service_plan sp WHERE sp.kind = 'SERVICE' AND sp.service_id IS NULL
        ) g
        GROUP BY g.company_id, g.gname
        ON CONFLICT (company_id, name) DO NOTHING
    """)
    op.execute(f"""
        UPDATE service_plan sp SET service_id = s.id, service_group = s.name
        FROM service s
        WHERE sp.kind = 'SERVICE' AND sp.service_id IS NULL
          AND s.company_id = sp.company_id AND s.name = {_GROUP}
    """)
    op.execute("""
        INSERT INTO service_device_category (service_id, device_category_id)
        SELECT DISTINCT sp.service_id, spc.device_category_id
        FROM service_plan sp
        JOIN service_plan_device_category spc ON spc.service_plan_id = sp.id
        WHERE sp.service_id IS NOT NULL
        ON CONFLICT DO NOTHING
    """)

    for tbl in ("service", "service_device_category"):
        if connection.execute(text("SELECT to_regclass(:t)"), {"t": tbl}).scalar() is None:
            raise RuntimeError(f"[sv1] {tbl} missing after upgrade")
    orphans = connection.execute(text(
        "SELECT count(*) FROM service_plan WHERE kind = 'SERVICE' AND service_id IS NULL"
    )).scalar()
    if orphans:
        raise RuntimeError(f"[sv1] {orphans} SERVICE plan(s) without a service after backfill")


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))
    op.execute("DROP INDEX IF EXISTS ix_service_plan_service_id")
    op.execute("ALTER TABLE service_plan DROP COLUMN IF EXISTS service_id")
    op.execute("DROP TABLE IF EXISTS service_device_category")
    op.execute("DROP TABLE IF EXISTS service")
