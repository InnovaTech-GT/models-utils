"""Insights v2: insightcharttype += LINE, insight_dashboard.default_time_range, insight_chart.viz

Revision ID: iv1_insights_v2
Revises: dc1_category_trim
Create Date: 2026-09-18

Insights v2 (uplink-workspace docs/superpowers/specs/2026-09-18-insights-v2-design.md
§5.1). Purely additive:

1. `insightcharttype` gains 'LINE' (the fourth chart type).
2. `insight_dashboard.default_time_range` JSON NULL — the dashboard's default
   TimeRange ({"preset": ...} or {"from", "to"}).
3. `insight_chart.viz` JSON NULL — presentation settings ({"width", "stacked"}).

Both JSON columns are opaque here: backend-erp validates and normalizes them
on write. `insight_chart.spec` is unchanged at the DB level (still JSON NOT
NULL); only its contents move to query-spec v2, and no v1 chart exists
anywhere (verified), so there is no backfill.

No statement in this file may NAME the new label outside the ALTER TYPE:
Postgres forbids using an enum value in the transaction that created it.

IRREVERSIBLE in part: Postgres cannot DROP an enum label, so `downgrade()`
drops the two columns and leaves 'LINE' in `insightcharttype` (precedents:
`c1e_install_actions`, `nc1a_network_config_core`, `pm1_payment_evidence`,
`tj1_task_job_kinds`). An unreferenced label is harmless — the Python enum is
what decides what is writable. `downgrade()` refuses while any chart row still
uses the label, because the pre-v2 Python enum cannot load such a row.

Hand-written (NOT autogenerate — autogenerate does not see enum label
additions), ba1 house style: lock_timeout, IF NOT EXISTS, post-upgrade
assertions.
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy.sql import text


# revision identifiers, used by Alembic.
revision: str = 'iv1_insights_v2'
down_revision: Union[str, Sequence[str], None] = 'dc1_category_trim'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ENUM_NAME = "insightcharttype"
_NEW_LABELS = ("LINE",)
_NEW_COLUMNS = (
    ("insight_dashboard", "default_time_range"),
    ("insight_chart", "viz"),
)


def upgrade() -> None:
    connection = op.get_bind()
    op.execute("SET lock_timeout = '5s'")

    # Native-enum ADD VALUE always runs in an autocommit_block (c1e/tj1/pm1
    # precedent): with transaction_per_migration the new value must be
    # committed before any later statement or seed can reference it.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE insightcharttype ADD VALUE IF NOT EXISTS 'LINE'")

    op.execute("ALTER TABLE insight_dashboard ADD COLUMN IF NOT EXISTS default_time_range JSON")
    op.execute("ALTER TABLE insight_chart ADD COLUMN IF NOT EXISTS viz JSON")

    # --- assertions ---------------------------------------------------------
    present = set(connection.execute(text(
        "SELECT e.enumlabel FROM pg_enum e "
        "JOIN pg_type t ON t.oid = e.enumtypid WHERE t.typname = :name"
    ), {"name": _ENUM_NAME}).scalars())
    missing = [label for label in _NEW_LABELS if label not in present]
    if missing:
        raise RuntimeError(
            f"[iv1] {_ENUM_NAME} label(s) missing after upgrade: {', '.join(missing)}"
        )

    for table, column in _NEW_COLUMNS:
        data_type = connection.execute(text(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_schema = current_schema() "
            "AND table_name = :table AND column_name = :column"
        ), {"table": table, "column": column}).scalar()
        if data_type != "json":
            raise RuntimeError(
                f"[iv1] {table}.{column} expected type json after upgrade, found {data_type!r}"
            )

    print("[iv1_insights_v2] upgrade complete")


def downgrade() -> None:
    connection = op.get_bind()
    op.execute("SET lock_timeout = '5s'")

    # Refuse while any chart uses the new label: the pre-v2 InsightChartType
    # has no member for it, so every dashboard GET holding such a row would
    # fail to load (SQLAlchemy LookupError). Delete or retype those charts
    # first. Compared as text through a bound parameter so this file still
    # names the label only in the ALTER TYPE.
    in_use = connection.execute(text(
        "SELECT COUNT(*) FROM insight_chart WHERE chart_type::text = ANY(:labels)"
    ), {"labels": list(_NEW_LABELS)}).scalar()
    if in_use:
        raise RuntimeError(
            f"[iv1] refusing downgrade: {in_use} insight_chart row(s) use "
            f"{', '.join(_NEW_LABELS)}; delete or retype them first"
        )

    op.execute("ALTER TABLE insight_chart DROP COLUMN IF EXISTS viz")
    op.execute("ALTER TABLE insight_dashboard DROP COLUMN IF EXISTS default_time_range")
    # Documented no-op for the enum: PostgreSQL cannot drop an enum label, so
    # the new label stays in insightcharttype (precedents tj1, pm1, c1e, nc1a).
    # Harmless once no row uses it (guarded above) — the Python enum is the
    # only thing that decides what is writable.
    print("[iv1_insights_v2] downgrade complete (insightcharttype label kept)")
