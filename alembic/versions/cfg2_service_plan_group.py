"""Configuración/Planes de servicios: service_plan.service_group + installation_price_cents

Revision ID: cfg2_service_plan_group
Revises: cfg1_user_profile
Create Date: 2026-09-15

Figma redesign PR 2 (docs/design/plans/02-configuracion.md §2.2), second of
three. The Figma groups plans under a "Servicio" heading ("Fibra óptica",
"Cable HFC") and prices the installation separately from the monthly fee.

Decision (do not re-litigate): free-text `service_group`, NOT a
`service_offering` table. The heading is a grouping label whose status is
derived (`active = any(plan.is_active)`), so it owns no attributes of its own.
# ponytail: free-text grouping label — promote to a `service_offering` table
# the day a service needs its own attributes (price, description, contract
# terms).

`installation_price_cents` is integer cents like every other money column in
the platform (NULL or 0 renders "Gratis"). BIGINT mirrors
`service_plan.price_cents`.

Backfill: `service_group = initcap(plan_type::text)` for SERVICE-kind rows
only, so day-one tenants open the page onto "Fiber"/"Cable"/… groups they can
rename from the UI rather than one unnamed bucket. INSTALLATION/PRODUCT-kind
rows are deliberately left NULL — they are not services and must not appear
as a group.

Hand-written (NOT autogenerate), ba1 house style: lock_timeout, IF NOT
EXISTS, post-upgrade assertions, total downgrade.
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy.sql import text


# revision identifiers, used by Alembic.
revision: str = 'cfg2_service_plan_group'
down_revision: Union[str, Sequence[str], None] = 'cfg1_user_profile'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_NEW_COLUMNS = (
    ("service_plan", "service_group"),
    ("service_plan", "installation_price_cents"),
)


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))

    op.execute(
        "ALTER TABLE service_plan ADD COLUMN IF NOT EXISTS service_group VARCHAR(100)"
    )
    op.execute(
        "ALTER TABLE service_plan ADD COLUMN IF NOT EXISTS installation_price_cents BIGINT"
    )

    # Grouped listing + the `?service_group=` filter are always company-scoped.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_service_plan_company_group "
        "ON service_plan (company_id, service_group)"
    )

    # Idempotent (WHERE service_group IS NULL), so a re-run after a
    # lock_timeout abort never overwrites a group a tenant already renamed.
    op.execute(
        "UPDATE service_plan SET service_group = initcap(plan_type::text) "
        "WHERE service_group IS NULL AND kind = 'SERVICE'"
    )

    missing = connection.execute(text(
        "SELECT string_agg(t.table_name || '.' || t.column_name, ', ') "
        "FROM (VALUES "
        + ", ".join(f"('{t}','{c}')" for t, c in _NEW_COLUMNS)
        + ") AS t(table_name, column_name) "
        "WHERE NOT EXISTS ("
        "  SELECT 1 FROM information_schema.columns c "
        "  WHERE c.table_name = t.table_name AND c.column_name = t.column_name)"
    )).scalar()
    if missing:
        raise RuntimeError(f"[cfg2] expected column(s) missing after upgrade: {missing}")
    if connection.execute(
        text("SELECT to_regclass('ix_service_plan_company_group')")
    ).scalar() is None:
        raise RuntimeError(
            "[cfg2] expected index 'ix_service_plan_company_group' to exist after upgrade"
        )

    print("[cfg2_service_plan_group] upgrade complete")


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))

    op.execute("DROP INDEX IF EXISTS ix_service_plan_company_group")
    op.execute("ALTER TABLE service_plan DROP COLUMN IF EXISTS installation_price_cents")
    op.execute("ALTER TABLE service_plan DROP COLUMN IF EXISTS service_group")

    print("[cfg2_service_plan_group] downgrade complete")
