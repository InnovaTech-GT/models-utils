"""Insights v2 (iv1_insights_v2) guardrails.

Same shape as tests/test_payment_evidence.py. What is load-bearing:

  - The revision may NOT name the new enum label outside the autocommit
    ALTER TYPE: Postgres forbids using a value in the transaction that
    created it, and the failure only shows up against a real PG.
  - downgrade() drops the two JSON columns and nothing else. The 'LINE'
    label stays (PG cannot drop enum labels) - a DROP TYPE here would take
    insight_chart.chart_type with it.
  - The two new columns are declared in two places that cannot import each
    other (the hand-written revision and the models). If they drift,
    autogenerate proposes a phantom change.
"""
import importlib.util
import os
import re
import uuid

import sqlalchemy as sa

from database_utils.database import Base
from database_utils.models.isp import InsightChart, InsightChartType, InsightDashboard

_VERSIONS = os.path.join(os.path.dirname(__file__), "..", "alembic", "versions")
_IV1_PATH = os.path.join(_VERSIONS, "iv1_insights_v2.py")


def _iv1():
    spec = importlib.util.spec_from_file_location("iv1_insights_v2", _IV1_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _code():
    """The revision source without its module docstring."""
    with open(_IV1_PATH) as handle:
        return handle.read().split('"""', 2)[2]


# --- migration chain ---

def test_migration_chain_position():
    iv1 = _iv1()
    assert iv1.revision == "iv1_insights_v2"
    assert iv1.down_revision == "dc1_category_trim"
    # alembic_version.version_num is VARCHAR(32)
    assert len(iv1.revision) <= 32


def test_label_is_added_inside_an_autocommit_block():
    assert re.search(
        r"autocommit_block\(\):\s*\n\s*op\.execute\(\s*"
        r"\"ALTER TYPE insightcharttype ADD VALUE IF NOT EXISTS 'LINE'\"",
        _code(),
    )


def test_revision_never_names_the_new_label_outside_the_alter():
    """PG cannot use an enum value in the transaction that created it, so the
    only statement allowed to say 'LINE' is the ALTER TYPE itself."""
    for line in _code().splitlines():
        code = line.split("#", 1)[0]
        if "'LINE'" in code:
            assert "ADD VALUE IF NOT EXISTS" in line, line


def test_upgrade_adds_both_columns_idempotently():
    upgrade = _code().split("def upgrade()", 1)[1].split("def downgrade()", 1)[0]
    assert (
        "ALTER TABLE insight_dashboard ADD COLUMN IF NOT EXISTS default_time_range JSON"
        in upgrade
    )
    assert "ALTER TABLE insight_chart ADD COLUMN IF NOT EXISTS viz JSON" in upgrade
    assert "SET lock_timeout = '5s'" in upgrade
    assert "RuntimeError" in upgrade


def test_downgrade_only_drops_the_two_columns():
    downgrade = _code().split("def downgrade()", 1)[1]
    assert "ALTER TABLE insight_chart DROP COLUMN IF EXISTS viz" in downgrade
    assert (
        "ALTER TABLE insight_dashboard DROP COLUMN IF EXISTS default_time_range"
        in downgrade
    )
    assert downgrade.count("DROP COLUMN") == 2
    assert "DROP TYPE" not in downgrade
    assert "DROP TABLE" not in downgrade


def test_downgrade_refuses_while_a_chart_uses_the_new_label():
    downgrade = _code().split("def downgrade()", 1)[1]
    guard = downgrade.index("chart_type::text = ANY(:labels)")
    assert "RuntimeError" in downgrade
    # the refusal runs before anything is dropped
    assert guard < downgrade.index("DROP COLUMN")


# --- revision <-> model parity ---

def test_chart_type_gains_line():
    assert [member.value for member in InsightChartType] == ["NUMBER", "BAR", "PIE", "LINE"]
    iv1 = _iv1()
    assert iv1._NEW_LABELS == ("LINE",)
    assert iv1._ENUM_NAME == "insightcharttype"
    assert InsightChart.__table__.c.chart_type.type.name == iv1._ENUM_NAME


def test_new_columns_declared_in_both_the_revision_and_the_models():
    assert _iv1()._NEW_COLUMNS == (
        ("insight_dashboard", "default_time_range"),
        ("insight_chart", "viz"),
    )
    for table, column in _iv1()._NEW_COLUMNS:
        col = Base.metadata.tables[table].c[column]
        assert isinstance(col.type, sa.JSON), (table, column)
        assert col.nullable is True, (table, column)


def test_spec_column_is_unchanged():
    spec = InsightChart.__table__.c.spec
    assert isinstance(spec.type, sa.JSON)
    assert spec.nullable is False
    # insight_chart stays scoped through its dashboard (no company_id).
    assert "company_id" not in InsightChart.__table__.c


def test_orm_round_trip_of_the_new_columns(db):
    dashboard = InsightDashboard(
        name="Cobros",
        company_id=uuid.uuid4(),
        default_time_range={"preset": "last_30_days"},
    )
    dashboard.charts.append(
        InsightChart(
            title="Cobrado neto",
            chart_type=InsightChartType.LINE,
            spec={"version": 2, "entity": "payments"},
            viz={"width": 2, "stacked": False},
        )
    )
    dashboard.charts.append(
        InsightChart(title="Total", chart_type=InsightChartType.NUMBER, spec={"version": 2}, ordering=1)
    )
    db.add(dashboard)
    db.commit()
    db.expire_all()

    got = db.get(InsightDashboard, dashboard.id)
    assert got.default_time_range == {"preset": "last_30_days"}
    line, number = got.charts
    assert line.chart_type is InsightChartType.LINE
    assert line.viz == {"width": 2, "stacked": False}
    assert number.viz is None
