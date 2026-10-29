from pydantic import BaseModel, ConfigDict
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

    model_config = ConfigDict(from_attributes=True)


class ManualPaymentMark(BaseModel):
    """Schema for marking invoice as paid manually (super admin)"""
    payment_method: str  # "Wire Transfer", "Check", "Cash"
    notes: Optional[str] = None


class BillingInvoiceWithCompany(BillingInvoiceOut):
    """Schema for invoice with company details (super admin view)"""
    company_id: UUID
    company_name: str
