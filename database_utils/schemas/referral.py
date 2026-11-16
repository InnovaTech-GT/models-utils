"""Doc 69: referral and partner program schemas. Stages, amounts, expiry and
holds are computed by auth-erp (services/referrals.py); the frontend renders them."""
from datetime import datetime
from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

REFERRAL_STAGES = ("REGISTERED", "ACTIVE", "CREDIT_PENDING", "CREDIT_AVAILABLE",
                   "NOT_ELIGIBLE", "REVERSED", "EXPIRED")
ReferralStage = Literal["REGISTERED", "ACTIVE", "CREDIT_PENDING", "CREDIT_AVAILABLE",
                        "NOT_ELIGIBLE", "REVERSED", "EXPIRED"]


class RewardOut(BaseModel):
    """Superadmin view of one referral_reward."""
    id: UUID
    referral_id: UUID
    company_id: UUID
    kind: str
    status: str
    amount_cents: Optional[int] = None
    remaining_cents: int = 0
    source_invoice_id: Optional[UUID] = None
    unlock_invoice_id: Optional[UUID] = None
    ineligible_reasons: Optional[list[str]] = None
    decided_at: Optional[datetime] = None
    decision_note: Optional[str] = None
    unlocked_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    reversal_id: Optional[UUID] = None
    reversed_at: Optional[datetime] = None
    forfeited_at: Optional[datetime] = None
    clawed_cents: int = 0
    holds: list[str] = []  # BENEFICIARY_NOT_MANUAL / REFERRER_NOT_PAYING, computed

    model_config = ConfigDict(from_attributes=True)


class PartyOut(BaseModel):
    """One side of a referral; company_id/name None once that company is deleted."""
    company_id: Optional[UUID] = None
    name: Optional[str] = None
    subscription_status: Optional[str] = None
    billing_type: Optional[str] = None
    paid_invoices: int = 0


class MatchOut(BaseModel):
    """An identity shared with another company (value redacted)."""
    code: str
    value_redacted: str
    other_party: str


class ReferralAdminOut(BaseModel):
    """Superadmin referrals queue row."""
    id: UUID
    created_at: datetime
    code: str
    nit_seen_before: bool
    referrer: PartyOut
    referred: PartyOut
    checks: dict[str, bool]
    matches: list[MatchOut]
    rewards: list[RewardOut]
    free_period_start: Optional[datetime] = None
    free_period_end: Optional[datetime] = None


class TenantReferralRow(BaseModel):
    """A referral as the referrer sees it: no amounts, no reasons."""
    company_name: Optional[str] = None  # None once the referred company is deleted
    created_at: datetime
    stage: ReferralStage


class ReferredByOut(BaseModel):
    company_name: Optional[str] = None
    free_month_status: Optional[str] = None
    free_period_start: Optional[datetime] = None
    free_period_end: Optional[datetime] = None


class TenantReferralsOut(BaseModel):
    """GET /billing/referrals (tenant card)."""
    enabled: bool
    code: Optional[str] = None
    credit_balance_cents: int = 0
    balance_due_cents: int = 0
    next_expiry: Optional[datetime] = None
    referrals: list[TenantReferralRow] = []
    referred_by: Optional[ReferredByOut] = None


class RewardDecision(BaseModel):
    """Body of approve / reject."""
    note: Optional[str] = Field(None, max_length=500)


class ReversalOut(BaseModel):
    """A refund / dispute as the superadmin sees it (payload omitted)."""
    id: UUID
    kind: str
    gateway_id: str
    status: Optional[str] = None
    reason: Optional[str] = None
    amount_cents: Optional[int] = None
    invoice_number: Optional[str] = None
    company_name: Optional[str] = None
    created_at: datetime
    applied_at: Optional[datetime] = None


class ReversalLink(BaseModel):
    """Link an unmatched reversal to an invoice by number."""
    invoice_number: str = Field(..., min_length=1, max_length=64)
