from pydantic import BaseModel, ConfigDict
from typing import Optional
from uuid import UUID
from .service_plan import ServicePlanOut


class OrderItemBase(BaseModel):
    service_plan_id: UUID
    quantity: int


class OrderItemInput(OrderItemBase):
    pass


class OrderItemCreate(OrderItemBase):
    pass


class OrderItemUpdate(BaseModel):
    service_plan_id: Optional[UUID] = None
    quantity: Optional[int] = None


class OrderItemOut(BaseModel):
    id: UUID
    # SET NULL on plan deletion; the snapshot columns below keep the line
    # meaningful (doc 16 §2.2).
    service_plan_id: Optional[UUID] = None
    quantity: int
    service_plan: Optional[ServicePlanOut] = None
    # Order-time snapshots (nullable for pre-backfill history).
    unit_price_cents: Optional[int] = None
    product_name: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)
