from typing import List, Optional
from datetime import datetime
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field

# Single source of truth — reuse the model enums (same pattern as task.py's
# TaskLinkedObjectType) so schema and DB never drift.
from database_utils.models.crm import PaymentKind, PaymentMethodType, PaymentStatus


class PaymentCreate(BaseModel):
    """Body of POST /orders/{order_id}/payments (doc 16 §3.2)."""
    amount_cents: int = Field(..., gt=0)
    method: PaymentMethodType
    reference: Optional[str] = None
    paid_at: Optional[datetime] = None
    notes: Optional[str] = None


class FullPaymentCreate(BaseModel):
    """Body of POST /orders/{order_id}/payments/full (doc 18 D3 / amendment 6).

    Deliberately has NO amount field — the outstanding balance is computed
    server-side under the same row lock that appends the payment, so a
    frontend-computed amount can never race a concurrent partial payment into
    a wrong charge. `PaymentCreate` above is intentionally UNTOUCHED (its
    amount_cents stays required, Field(..., gt=0)) — the balance-mode branch
    lives only in the service layer, keyed off `FullPaymentCreate` having no
    such field at all, never off a schema-level Optional[int] on the shared
    partial-payment body (that would let a client silently trigger a full
    charge by omitting amount_cents on the public partial route)."""
    method: PaymentMethodType
    reference: Optional[str] = None
    paid_at: Optional[datetime] = None
    notes: Optional[str] = None


class PaymentRefundCreate(BaseModel):
    """Body of POST /orders/{order_id}/payments/{payment_id}/refund.

    amount_cents defaults to the remaining refundable amount of the original
    payment when omitted."""
    amount_cents: Optional[int] = Field(None, gt=0)
    reason: str
    reference: Optional[str] = None


class PaymentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    company_id: UUID
    order_id: UUID
    invoice_id: Optional[UUID] = None
    kind: PaymentKind
    amount_cents: int
    method: PaymentMethodType
    reference: Optional[str] = None
    paid_at: datetime
    received_by: Optional[UUID] = None
    reverses_payment_id: Optional[UUID] = None
    notes: Optional[str] = None


class OrderPaymentsOut(BaseModel):
    """Response of GET /orders/{order_id}/payments (doc 16 §3.2)."""
    payments: List[PaymentOut]
    total_cents: int
    paid_cents: int
    refunded_cents: int
    balance_cents: int
    payment_status: PaymentStatus
    invoice_id: Optional[UUID] = None
