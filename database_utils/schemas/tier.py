# schemas/tier.py
from pydantic import BaseModel, ConfigDict
from typing import Optional
from datetime import datetime
from uuid import UUID


class TierBase(BaseModel):
    name: str
    price: float = 0.0  # Monthly price in GTQ
    price_yearly: Optional[float] = None  # Yearly price in GTQ (None = yearly not available)
    billing_cycle: str = "MONTHLY"  # MONTHLY, YEARLY (tier default)
    features: Optional[dict] = None
    modules: Optional[list] = None  # ["core", "admin", "management", "automations"]
    is_active: bool = True


class TierCreate(TierBase):
    pass


class TierUpdate(BaseModel):
    """Used for PATCH - all fields optional"""
    name: Optional[str] = None
    price: Optional[float] = None
    price_yearly: Optional[float] = None
    billing_cycle: Optional[str] = None
    features: Optional[dict] = None
    modules: Optional[list] = None
    is_active: Optional[bool] = None


class TierOut(TierBase):
    id: UUID
    created_at: datetime
    stripe_price_id: Optional[str] = None
    recurrente_product_id: Optional[str] = None
    recurrente_price_id: Optional[str] = None
    recurrente_price_yearly_id: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class TierPublic(BaseModel):
    """Public-facing tier info for the pricing page — no Stripe IDs or internal fields"""
    id: UUID
    name: str
    price: float  # Monthly price in GTQ
    price_yearly: Optional[float] = None  # Yearly price in GTQ
    billing_cycle: str
    modules: Optional[list] = None
    features: Optional[dict] = None
    is_active: bool
    # True when the tier can be bought via hosted Recurrente checkout
    # (recurrente_price_id set) — the endpoint stamps it; never the raw id.
    purchasable: bool = False

    model_config = ConfigDict(from_attributes=True)
