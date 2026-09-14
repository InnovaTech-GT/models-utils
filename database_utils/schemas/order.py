from typing import List, Optional, TYPE_CHECKING
from datetime import datetime
from uuid import UUID
from pydantic import BaseModel, computed_field, ConfigDict
from .order_item import OrderItemInput, OrderItemOut
from .client import ClientOut
import calendar

# Single source of truth — reuse the model enums (doc 16 §1 contract).
from database_utils.models.crm import OrderStatus, OrderType, PaymentStatus
from database_utils.utils.timezone_utils import make_aware_gt, today_gt

if TYPE_CHECKING:
    from .recurring_order import RecurringOrderOut


class OrderBase(BaseModel):
    client_id: Optional[UUID]

class OrderCreate(OrderBase):
    order_items: List[OrderItemInput]
    due_date: Optional[datetime] = None  # Optional due date for manually created orders

class OrderUpdate(BaseModel):
    # `paid` removed (doc 16 §3.2): payment state is written ONLY by the
    # payment ledger (PaymentService). extra='forbid' makes a payload that
    # still sends `paid` fail with 422 instead of being silently ignored.
    model_config = ConfigDict(extra="forbid")

    total: Optional[float] = None
    client_id: Optional[UUID] = None
    order_items: Optional[List[OrderItemInput]] = None
    due_date: Optional[datetime] = None
    status: Optional[OrderStatus] = None

class OrderOut(OrderBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    due_date: Optional[datetime] = None
    payment_date: Optional[datetime] = None
    total: float
    paid: bool  # kept through Cycle 1 (dual-write); drops in Cycle 2
    status: OrderStatus
    # --- Cycle 1 billing additions (doc 16 §3.3, additive) ---
    order_type: OrderType = OrderType.ONE_SHOT
    payment_status: PaymentStatus = PaymentStatus.PENDING
    total_cents: Optional[int] = None
    # Ledger-derived; populated by the backend (not ORM columns).
    amount_paid_cents: Optional[int] = None
    balance_cents: Optional[int] = None
    client_service_id: Optional[UUID] = None
    recurring_order_id: Optional[UUID] = None
    client_id: UUID
    client: Optional[ClientOut]
    company_id: UUID
    order_items: List[OrderItemOut]
    recurring_order: Optional["RecurringOrderOut"] = None

    @computed_field
    @property
    def is_overdue(self) -> bool:
        """OVERDUE is never stored (doc 16 §1): computed as payment_status in
        (PENDING, PARTIAL) AND due_date < today_gt() (Guatemala calendar date)
        AND status ACTIVE — matches the backend's server-side overdue filter
        (Order.due_date < today_gt()) so badges and tab membership agree."""
        if self.status != OrderStatus.ACTIVE:
            return False
        if self.payment_status not in (PaymentStatus.PENDING, PaymentStatus.PARTIAL):
            return False
        return bool(
            self.due_date is not None
            and make_aware_gt(self.due_date).date() < today_gt()
        )

    @computed_field
    @property
    def generation_period(self) -> Optional[str]:
        """
        Compute a human-readable period label for recurring orders.
        Returns None if this is not a recurring order.
        Uses the due_date to determine the period.
        """
        if not self.recurring_order or not self.due_date:
            return None

        recurrence = self.recurring_order.recurrence
        # For recurring orders, the due_date represents the end of the billing period
        period_date = self.due_date

        if recurrence == "MONTHLY":
            return f"{calendar.month_name[period_date.month]} {period_date.year}"
        elif recurrence == "WEEKLY":
            week_num = period_date.isocalendar()[1]
            return f"Week {week_num} {period_date.year}"
        elif recurrence == "YEARLY":
            return f"{period_date.year}"
        elif recurrence == "DAILY":
            return period_date.strftime("%B %d, %Y")
        else:
            return period_date.strftime("%B %d, %Y")
