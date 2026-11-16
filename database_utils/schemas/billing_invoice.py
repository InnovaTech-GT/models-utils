from pydantic import BaseModel, ConfigDict, computed_field
from typing import Optional
from datetime import datetime
from uuid import UUID


class BillingInvoiceOut(BaseModel):
    """Schema for returning billing invoice data"""
    id: UUID
    invoice_number: str
    invoice_date: datetime
    due_date: datetime
    paid_at: Optional[datetime]
    subtotal: int
    tax: int
    total: int
    status: str
    payment_type: str
    manual_payment_method: Optional[str]
    manual_payment_note: Optional[str]
    billing_reason: Optional[str]
    notes: Optional[str] = None
    # Doc 61 (NULL on pre-61 invoices).
    period_start: Optional[datetime] = None
    period_end: Optional[datetime] = None
    billed_clients: Optional[int] = None
    # Doc 69 §5 (all 0 on pre-rf1 invoices).
    credit_cents: int = 0
    previous_balance_cents: int = 0
    carried_out_cents: int = 0
    refunded_cents: int = 0

    model_config = ConfigDict(from_attributes=True)

    @computed_field
    @property
    def discounted(self) -> bool:
        """net < subtotal, net from the §5 invariant (same comparison as auth-erp price_note)."""
        net = self.total + self.credit_cents + self.carried_out_cents - self.previous_balance_cents
        return net < self.subtotal


class ManualPaymentMark(BaseModel):
    """Schema for marking invoice as paid manually (super admin)"""
    payment_method: str  # "Wire Transfer", "Check", "Cash"
    notes: Optional[str] = None


class BillingInvoiceWithCompany(BillingInvoiceOut):
    """Schema for invoice with company details (super admin view)"""
    company_id: UUID
    company_name: str
