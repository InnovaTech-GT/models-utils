"""Ajustes de la integración RECURRENTE y la vista de un link de pago."""
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

PaymentLinkMethod = Literal["bank_transfer", "card"]


class RecurrenteSettings(BaseModel):
    """Ajustes NO secretos de la integración, guardados en credentials junto
    a la llave. Por defecto: solo transferencia, links de 5 días."""
    model_config = ConfigDict(extra="forbid")

    payment_methods: list[PaymentLinkMethod] = Field(default_factory=lambda: ["bank_transfer"])
    link_hours: int = Field(120, ge=1, le=720)

    @field_validator("payment_methods")
    @classmethod
    def _no_vacio_sin_repetidos(cls, v):
        if not v:
            raise ValueError("payment_methods needs at least one method")
        if len(set(v)) != len(v):
            raise ValueError("payment_methods has duplicates")
        return v


class PaymentLinkOut(BaseModel):
    """Vista de oficina. No incluye nada secreto."""
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    company_id: UUID
    client_id: UUID
    order_id: UUID
    provider: str
    status: str
    amount_cents: int
    currency: str
    payment_methods: list[str]
    expires_at: datetime
    checkout_id: str | None = None
    checkout_url: str | None = None
    live_mode: bool | None = None
    last_checked_at: datetime | None = None
    checks: int
    intent_id: str | None = None
    payment_id: UUID | None = None
    paid_at: datetime | None = None
    review_reason: str | None = None
    created_at: datetime
