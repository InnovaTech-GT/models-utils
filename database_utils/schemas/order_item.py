from pydantic import BaseModel, ConfigDict, Field
from typing import Optional
from uuid import UUID
from .service_plan import ServicePlanOut


class OrderItemBase(BaseModel):
    service_plan_id: UUID
    # Security audit #68: a zero/negative line would credit the client. The DB
    # backstop is ck_order_item_quantity_positive (sec1_quantity_checks).
    quantity: int = Field(ge=1)


class OrderItemInput(OrderItemBase):
    pass


class OrderItemCreate(OrderItemBase):
    pass


class OrderItemUpdate(BaseModel):
    service_plan_id: Optional[UUID] = None
    quantity: Optional[int] = Field(default=None, ge=1)


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
