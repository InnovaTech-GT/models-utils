# schemas/insight.py
"""
Insights (Cycle 4, v2 since models-utils 1.33.0): tenant-defined dashboards of
charts over existing entities. Pure request/response shape.

`spec`, `viz` and `default_time_range` are OPAQUE dicts on purpose. backend-erp
owns the query-spec v2 schema (insights/spec.py), validates all three on write
and stores the normalized dump; reads never re-validate, so a stale spec can
never 500 a dashboard GET, and a spec change never needs a models-utils release.
"""
from pydantic import BaseModel, ConfigDict
from typing import Optional, List, Dict, Any
from uuid import UUID
from datetime import datetime

from database_utils.models.isp import InsightChartType


class InsightChartBase(BaseModel):
    title: str
    chart_type: InsightChartType
    spec: Dict[str, Any]                       # opaque; backend-erp validates on write
    viz: Optional[Dict[str, Any]] = None       # opaque; backend-erp validates on write


class InsightChartCreate(InsightChartBase):
    ordering: Optional[int] = None             # None -> backend assigns max+1 (inline: list index)


class InsightChartUpdate(BaseModel):
    title: Optional[str] = None
    chart_type: Optional[InsightChartType] = None
    spec: Optional[Dict[str, Any]] = None
    viz: Optional[Dict[str, Any]] = None
    ordering: Optional[int] = None


class InsightChartOut(InsightChartBase):
    id: UUID
    dashboard_id: UUID
    ordering: int
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class InsightDashboardBase(BaseModel):
    name: str
    ordering: int = 0
    default_time_range: Optional[Dict[str, Any]] = None   # opaque; backend-erp validates on write


class InsightDashboardCreate(InsightDashboardBase):
    charts: List[InsightChartCreate] = []


class InsightDashboardUpdate(BaseModel):
    name: Optional[str] = None
    ordering: Optional[int] = None
    default_time_range: Optional[Dict[str, Any]] = None


class InsightDashboardOut(InsightDashboardBase):
    id: UUID
    company_id: UUID
    created_at: datetime
    updated_at: datetime
    charts: List[InsightChartOut] = []

    model_config = ConfigDict(from_attributes=True)
