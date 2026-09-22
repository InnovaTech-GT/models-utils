"""Insights v2 schema pins (database_utils/schemas/insight.py).

The chart `spec`, chart `viz` and dashboard `default_time_range` are OPAQUE
dicts on purpose: backend-erp owns query-spec v2 and validates all three on
write; reads never re-validate (a stale spec must not 500 a dashboard GET).
Anything that starts parsing them here re-creates the 4-repo release
coupling the v2 design removed.
"""
import uuid

import pytest
from pydantic import ValidationError

import database_utils.schemas as schemas
import database_utils.schemas.insight as insight
from database_utils.models.isp import InsightChart, InsightChartType, InsightDashboard
from database_utils.schemas.insight import (
    InsightChartCreate,
    InsightChartOut,
    InsightChartUpdate,
    InsightDashboardCreate,
    InsightDashboardOut,
    InsightDashboardUpdate,
)

_SPEC = {
    "version": 2,
    "entity": "payments",
    "measures": [{"id": "net", "agg": "sum", "field": "signed_amount_cents"}],
    "time": {"field": "paid_at", "granularity": "week", "range": {"preset": "last_12_months"}},
}


def test_v1_spec_model_is_gone():
    assert not hasattr(insight, "InsightChartSpec")
    assert not hasattr(schemas, "InsightChartSpec")


def test_spec_and_viz_pass_through_unparsed():
    body = {**_SPEC, "future_key": {"nested": [1, 2]}}
    chart = InsightChartCreate(
        title="Cobrado neto", chart_type="LINE", spec=body, viz={"width": 9, "future": True}
    )
    assert chart.spec == body
    assert chart.viz == {"width": 9, "future": True}
    assert chart.chart_type is InsightChartType.LINE


def test_create_defaults_ordering_and_viz_to_none():
    chart = InsightChartCreate(title="t", chart_type="BAR", spec=_SPEC)
    assert chart.ordering is None
    assert chart.viz is None


def test_unknown_chart_type_is_rejected():
    with pytest.raises(ValidationError):
        InsightChartCreate(title="t", chart_type="AREA", spec=_SPEC)


def test_chart_update_is_all_optional_and_tracks_explicit_null():
    assert set(InsightChartUpdate.model_fields) == {
        "title", "chart_type", "spec", "viz", "ordering",
    }
    assert InsightChartUpdate().model_dump(exclude_unset=True) == {}
    # PATCH `viz: null` must be distinguishable from "not sent".
    assert InsightChartUpdate(viz=None).model_dump(exclude_unset=True) == {"viz": None}


def test_dashboard_update_tracks_explicit_null_range():
    assert set(InsightDashboardUpdate.model_fields) == {"name", "ordering", "default_time_range"}
    assert InsightDashboardUpdate(default_time_range=None).model_dump(exclude_unset=True) == {
        "default_time_range": None
    }


def test_dashboard_create_carries_range_and_inline_charts():
    dashboard = InsightDashboardCreate(
        name="Cobros",
        default_time_range={"from": "2026-01-01", "to": "2026-03-31"},
        charts=[{"title": "Total", "chart_type": "NUMBER", "spec": _SPEC}],
    )
    assert dashboard.ordering == 0
    assert dashboard.default_time_range == {"from": "2026-01-01", "to": "2026-03-31"}
    assert dashboard.charts[0].ordering is None
    assert InsightDashboardCreate(name="x").default_time_range is None


def test_chart_out_requires_ordering():
    assert InsightChartOut.model_fields["ordering"].is_required()
    assert InsightChartOut.model_fields["viz"].default is None


def test_out_models_read_from_orm(db):
    dashboard = InsightDashboard(
        name="Cobros", company_id=uuid.uuid4(), default_time_range={"preset": "last_30_days"}
    )
    dashboard.charts.append(
        InsightChart(
            title="Cobrado neto", chart_type=InsightChartType.LINE, spec=_SPEC,
            viz={"width": 2, "stacked": False}, ordering=3,
        )
    )
    db.add(dashboard)
    db.commit()

    out = InsightDashboardOut.model_validate(dashboard)
    assert out.default_time_range == {"preset": "last_30_days"}
    chart = out.charts[0]
    assert chart.chart_type is InsightChartType.LINE
    assert chart.spec == _SPEC
    assert chart.viz == {"width": 2, "stacked": False}
    assert chart.ordering == 3
    assert chart.dashboard_id == dashboard.id
