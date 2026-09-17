"""Clientes: client.dpi + deactivated_at + deactivation_reason

Revision ID: cl1_client_dpi_deactivation
Revises: cfg3_matrix_permissions
Create Date: 2026-09-16

Figma redesign PR 3 (docs/design/plans/03-clientes.md §2, master plan §2.3).
The Clientes table has a `DPI` column (the Guatemalan CUI) and splits into
two tabs, `Actuales` / `Histórico` — none of the three facts behind that
exist today.

  - `dpi`                 -> the DPI cell, and the exact-match lookup that
                             opens the "Agregar cliente" sheet.
  - `deactivated_at`      -> NULL = Actuales, NOT NULL = Histórico, and the
                             `Fecha de baja` column of the Histórico table.
  - `deactivation_reason` -> free text captured by the Suspender/baja dialog.

DPI is nullable (resolved default, master plan §8): every legacy row has
none, the xlsx import leaves it empty, and a tenant may legitimately never
collect it. The unique index is therefore PARTIAL — `WHERE dpi IS NOT NULL`
— so unlimited NULLs coexist while a real DPI is unique inside one company
(and the SAME DPI may exist in two different companies; it is national
identity, not a tenant-scoped sequence). Exact-match lookup 404s for legacy
clients until a human or an import fills the column in (§9.3).

No backfill of any kind: `deactivated_at IS NULL` already means "active", so
every pre-existing client shows up under Actuales on day one without a data
migration.

`ix_client_company_active` exists because BOTH tabs filter `company_id` +
`deactivated_at IS [NOT] NULL` on every page load — the tab split turns the
list endpoint's hot path into exactly that two-column predicate.

Nothing here touches a PG enum, so no autocommit block is needed and the
downgrade is total (2 indexes + 3 columns, all IF EXISTS).

Hand-written (NOT autogenerate), ba1 house style: lock_timeout, IF NOT
EXISTS, post-upgrade assertions, total downgrade.
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy.sql import text


# revision identifiers, used by Alembic.
revision: str = 'cl1_client_dpi_deactivation'
down_revision: Union[str, Sequence[str], None] = 'cfg3_matrix_permissions'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_NEW_COLUMNS = (
    ("client", "dpi"),
    ("client", "deactivated_at"),
    ("client", "deactivation_reason"),
)

_NEW_INDEXES = ("uq_client_company_dpi", "ix_client_company_active")


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))

    op.execute("ALTER TABLE client ADD COLUMN IF NOT EXISTS dpi VARCHAR")
    op.execute(
        "ALTER TABLE client ADD COLUMN IF NOT EXISTS "
        "deactivated_at TIMESTAMP WITH TIME ZONE"
    )
    op.execute(
        "ALTER TABLE client ADD COLUMN IF NOT EXISTS deactivation_reason VARCHAR"
    )

    # Partial: NULL dpi is the norm (no backfill), so a plain UNIQUE would be
    # fine in PG but would lie about intent — and the predicate is what keeps
    # the index small on tenants that never collect DPIs.
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_client_company_dpi "
        "ON client (company_id, dpi) WHERE dpi IS NOT NULL"
    )
    # Actuales/Histórico tab predicate.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_client_company_active "
        "ON client (company_id, deactivated_at)"
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
        raise RuntimeError(f"[cl1] expected column(s) missing after upgrade: {missing}")

    for index_name in _NEW_INDEXES:
        if connection.execute(
            text("SELECT to_regclass(:name)"), {"name": index_name}
        ).scalar() is None:
            raise RuntimeError(
                f"[cl1] expected index '{index_name}' to exist after upgrade"
            )

    print("[cl1_client_dpi_deactivation] upgrade complete")


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))

    op.execute("DROP INDEX IF EXISTS ix_client_company_active")
    op.execute("DROP INDEX IF EXISTS uq_client_company_dpi")
    op.execute("ALTER TABLE client DROP COLUMN IF EXISTS deactivation_reason")
    op.execute("ALTER TABLE client DROP COLUMN IF EXISTS deactivated_at")
    op.execute("ALTER TABLE client DROP COLUMN IF EXISTS dpi")

    print("[cl1_client_dpi_deactivation] downgrade complete")
