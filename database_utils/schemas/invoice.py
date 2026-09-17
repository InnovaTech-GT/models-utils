from typing import Optional, List
from pydantic import BaseModel, ConfigDict, Field
from datetime import datetime
from uuid import UUID

from database_utils.models.crm import PaymentStatus


class InvoiceBase(BaseModel):
    issue_date: datetime
    subtotal: float
    tax: float
    total: float
    details: dict


class InvoiceOut(InvoiceBase):
    id: UUID
    created_at: datetime
    company_id: UUID
    order_id: UUID
    is_valid: bool = True
    # --- Cycle 1 billing additions (doc 16 §3.5, additive) ---
    subtotal_cents: Optional[int] = None
    tax_cents: Optional[int] = None
    total_cents: Optional[int] = None
    # Order payment_status passthrough; populated by the backend.
    payment_status: Optional[PaymentStatus] = None

    model_config = ConfigDict(from_attributes=True)


class BulkOrderIdsRequest(BaseModel):
    order_ids: List[UUID] = Field(..., min_length=1, max_length=100)


class BulkOperationItemResult(BaseModel):
    order_id: UUID
    success: bool
    invoice_id: Optional[UUID] = None
    error: Optional[str] = None


class BulkOperationResponse(BaseModel):
    succeeded: int
    failed: int
    results: List[BulkOperationItemResult]
