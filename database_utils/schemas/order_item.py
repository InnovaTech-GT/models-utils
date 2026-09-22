from pydantic import BaseModel, ConfigDict, model_validator
from typing import Optional
from uuid import UUID
from .product import ProductOut
from .service_plan import ServicePlanOut


class OrderItemBase(BaseModel):
    # Cycle 2 D1 (entity merge): service_plan_id is preferred; product_id is
    # DEPRECATED but still accepted during the rollback window — the server
    # resolves product_id -> service_plan via the uq_service_plan_product
    # bridge and always snapshots from the plan. Exactly one must be given.
    product_id: Optional[UUID] = None
    service_plan_id: Optional[UUID] = None
    quantity: int

    @model_validator(mode="after")
    def _exactly_one_catalog_ref(self) -> "OrderItemBase":
        if bool(self.product_id) == bool(self.service_plan_id):
            raise ValueError(
                "exactly one of product_id or service_plan_id is required"
            )
        return self


class OrderItemInput(OrderItemBase):
    pass


class OrderItemCreate(OrderItemBase):
    pass


class OrderItemUpdate(BaseModel):
    product_id: Optional[UUID] = None
    service_plan_id: Optional[UUID] = None
    quantity: Optional[int] = None


class OrderItemOut(BaseModel):
    id: UUID
    # Nullable since Cycle 1: product_id is SET NULL on product deletion; the
    # snapshot columns below keep the line meaningful (doc 16 §2.2).
    product_id: Optional[UUID] = None
    # Cycle 2 D1: dual-written alongside product_id through the rollback
    # window; SET NULL on plan deletion (mirrors product_id).
    service_plan_id: Optional[UUID] = None
    quantity: int
    product: Optional[ProductOut] = None
    service_plan: Optional[ServicePlanOut] = None
    # Order-time snapshots (nullable for pre-backfill history).
    unit_price_cents: Optional[int] = None
    product_name: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)
