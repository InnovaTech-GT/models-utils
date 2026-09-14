# schemas/service_plan.py
"""
Cycle 2 D1/D2/D5: ServicePlan absorbs Product (kind/stock) and gains
`migration_source`
is a dedicated marker column (doc 18 amendment 1/2) and is NEVER exposed on
any schema here — it is internal audit state, not user-editable data.
"""
from pydantic import BaseModel, ConfigDict, field_validator
from typing import Any, List, Literal, Optional
from uuid import UUID
from datetime import datetime
import re

from database_utils.models.isp import ServicePlanType, CatalogKind

_PARAM_KEY = re.compile(r"^[a-z][a-z0-9_]*$")


class ProvisioningParam(BaseModel):
    """One row of a plan's provisioning intent (doc 33).

    Surfaced to playbooks as `{{service_plan.<key>}}`. `description` exists so
    the playbook editor can explain what a parameter is for — the raw JSON
    blob this replaced carried no such affordance.

    `scope` says WHERE the value lives:
      - 'plan'    — one shared value here, used by every service on this plan
      - 'service' — the plan only DECLARES the parameter; each ClientService
                    supplies its own value in `client_service.provisioning_params`
    Both resolve under `service_plan.<key>`, so a playbook never changes when a
    parameter's scope flips. A row with no scope is plan-scoped, which is what
    every row written before this feature is.
    """
    key: str
    value: Any = None
    description: Optional[str] = None
    scope: Literal["plan", "service"] = "plan"

    @field_validator("key")
    @classmethod
    def _snake_case(cls, v: str) -> str:
        if not _PARAM_KEY.match(v or ""):
            raise ValueError(
                "provisioning parameter key must be snake_case "
                "(lowercase letters, digits, underscores; not starting with a digit)"
            )
        return v


def _coerce_params(value):
    """Accept the legacy flat dict as well as the row list, so an xlsx import
    or a hand-written payload predating pv1 still validates."""
    if isinstance(value, dict):
        return [{"key": str(k), "value": v, "description": None} for k, v in value.items()]
    return value


class ServicePlanBase(BaseModel):
    name: str
    description: Optional[str] = None
    plan_type: ServicePlanType = ServicePlanType.FIBER
    download_mbps: Optional[int] = None
    upload_mbps: Optional[int] = None
    data_cap_gb: Optional[int] = None
    price: float = 0.0
    is_active: bool = True
    provisioning_params: Optional[List[ProvisioningParam]] = None
    product_id: Optional[UUID] = None
    # Cycle 2 D1/D2: what this plan bills for (drives Order.order_type
    # derivation). Absorbed-from-Product: stock (NULL = not stock-tracked).
    kind: CatalogKind = CatalogKind.SERVICE
    stock: Optional[int] = None

    @field_validator("provisioning_params", mode="before")
    @classmethod
    def _accept_legacy_dict(cls, v):
        return _coerce_params(v)


class ServicePlanCreate(ServicePlanBase):
    pass


class ServicePlanUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    plan_type: Optional[ServicePlanType] = None
    download_mbps: Optional[int] = None
    upload_mbps: Optional[int] = None
    data_cap_gb: Optional[int] = None
    price: Optional[float] = None
    is_active: Optional[bool] = None
    provisioning_params: Optional[List[ProvisioningParam]] = None
    product_id: Optional[UUID] = None
    kind: Optional[CatalogKind] = None
    stock: Optional[int] = None

    @field_validator("provisioning_params", mode="before")
    @classmethod
    def _accept_legacy_dict(cls, v):
        return _coerce_params(v)


class ServicePlanOut(ServicePlanBase):
    id: UUID
    company_id: UUID
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
