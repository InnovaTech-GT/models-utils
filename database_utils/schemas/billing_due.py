from pydantic import BaseModel, ConfigDict
from typing import Optional, List, TYPE_CHECKING
from datetime import datetime
from uuid import UUID
from database_utils.models.crm import RecurrenceEnum

if TYPE_CHECKING:
    from .order import OrderOut


class OrderGenerationResponse(BaseModel):
    """Response when manually generating an order from a recurring template."""
    model_config = ConfigDict(from_attributes=True)

    order: "OrderOut"  # Properly typed using forward reference
    generation_for_date: datetime
    generation_period: str  # Human-readable period (e.g., "February 2026", "Week 7 2026")
    next_generation_date: Optional[datetime] = None


# ===================== Gap Detection & Regeneration =====================
class MissingPeriod(BaseModel):
    """A period where an order should have been generated but wasn't."""
    period_date: datetime      # Start date of the missing period
    period_label: str          # Human-readable label (e.g., "March 2026")
    expected_due_date: datetime  # What the due_date would be for this period


class GeneratedOrdersWithGaps(BaseModel):
    """Response with orders and detected missing periods."""
    orders: List["OrderOut"]
    missing_periods: List[MissingPeriod]
    total_expected: int
    total_generated: int
    total_missing: int


class RegeneratePeriodRequest(BaseModel):
    """Request to regenerate orders for specific missing periods."""
    period_dates: List[datetime]


class RegeneratePeriodResponse(BaseModel):
    """Response after regenerating orders for missing periods."""
    generated_orders: List["OrderOut"]
    failed_periods: List[MissingPeriod]
    success_count: int
    failure_count: int


class DueBillingItemOut(BaseModel):
    """Cycle 2 (doc 18 amendment 5): the cron contract-frozen response_model
    for GET /recurring-orders/get-all-due. The client_service billing engine
    serializes into this shape — cron-erp reads only id/client.name/recurrence/
    next_generation_date via .get(), so this reshape is contract-safe.
    `source` is additive and cron-erp ignores it."""
    id: UUID
    client: Optional["_DueBillingClientOut"] = None
    recurrence: RecurrenceEnum
    next_generation_date: Optional[datetime] = None
    source: str  # "client_service" | "recurring_order"


class _DueBillingClientOut(BaseModel):
    name: str


DueBillingItemOut.model_rebuild()
