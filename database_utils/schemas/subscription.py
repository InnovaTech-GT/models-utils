from pydantic import BaseModel, ConfigDict
from typing import Optional
from datetime import datetime
from uuid import UUID


class SubscriptionOut(BaseModel):
    """Schema for returning subscription data"""
    id: UUID
    status: str
    billing_type: str
    billing_cycle: str  # Company's chosen cycle: MONTHLY or YEARLY
    current_period_start: datetime
    current_period_end: datetime
    cancel_at_period_end: bool
    canceled_at: Optional[datetime]
    trial_end: Optional[datetime]
    tier_id: UUID
    company_id: UUID
    stripe_subscription_id: Optional[str]
    # Recurrente integration — set by the subscription.create webhook.
    recurrente_subscription_id: Optional[str] = None
    card_last4: Optional[str] = None
    card_brand: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class SubscriptionUpdate(BaseModel):
    """Schema for updating subscription"""
    tier_id: Optional[UUID] = None
    cancel_at_period_end: Optional[bool] = None
    billing_cycle: Optional[str] = None  # Change between MONTHLY and YEARLY


class SubscriptionWithTier(SubscriptionOut):
    """Schema for subscription with tier details"""
    tier_name: str
    tier_price: float  # Monthly price in GTQ
    tier_price_yearly: Optional[float] = None  # Yearly price in GTQ
    tier_billing_cycle: str  # Tier's default billing cycle
    tier_modules: Optional[list] = None  # Enabled module groups for this tier
