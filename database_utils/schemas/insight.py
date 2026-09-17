# schemas/insight.py
"""
Insights (Cycle 4): tenant-defined dashboards of simple charts driven off
existing entities (clients, orders, client_services, ...). Follows
schemas/topology.py's structure — pure request/response shape; resolution of
`spec` (what data a chart actually renders) lives server-side in backend-erp's
insights service, not here.
"""
from pydantic import BaseModel, ConfigDict
from typing import Optional, List, Dict, Any
from uuid import UUID
from datetime import datetime

from database_utils.models.isp import InsightChartType


class InsightChartSpec(BaseModel):
    entity: str
    measure: str
    dimension: Optional[str] = None
    # A list of filter clauses ({column, op, value}) — the SAME shape the
    # /insights/query engine accepts, so a saved chart's spec replays verbatim.
    filters: Optional[List[Dict[str, Any]]] = None


class InsightChartBase(BaseModel):
    title: str
    chart_type: InsightChartType
    spec: InsightChartSpec
    ordering: int = 0


class InsightChartCreate(InsightChartBase):
    pass


class InsightChartUpdate(BaseModel):
    title: Optional[str] = None
    chart_type: Optional[InsightChartType] = None
    spec: Optional[InsightChartSpec] = None
    ordering: Optional[int] = None


class InsightChartOut(InsightChartBase):
    id: UUID
    dashboard_id: UUID
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class InsightDashboardBase(BaseModel):
    name: str
    ordering: int = 0


class InsightDashboardCreate(InsightDashboardBase):
    charts: List[InsightChartCreate] = []


class InsightDashboardUpdate(BaseModel):
    name: Optional[str] = None
    ordering: Optional[int] = None


class InsightDashboardOut(InsightDashboardBase):
    id: UUID
    company_id: UUID
    created_at: datetime
    updated_at: datetime
    charts: List[InsightChartOut] = []

    model_config = ConfigDict(from_attributes=True)
