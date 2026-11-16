"""rf1 (doc 69 §5) schema contract: signup referral fields, invoice credit
fields + the derived `discounted`, partner flag, referral schemas."""
import uuid
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from database_utils.schemas.billing_invoice import BillingInvoiceOut, BillingInvoiceWithCompany
from database_utils.schemas.referral import (
    REFERRAL_STAGES,
    ReversalLink,
    RewardDecision,
    RewardOut,
    TenantReferralRow,
    TenantReferralsOut,
)
from database_utils.schemas.requests import SignupCompanyRequest
from database_utils.schemas.subscription import SubscriptionExceptionUpdate, SubscriptionWithTier

NOW = datetime(2026, 11, 12, tzinfo=timezone.utc)


def _inv(**kw):
    row = dict(id=uuid.uuid4(), invoice_number="U-1", invoice_date=NOW, due_date=NOW, paid_at=None,
               subtotal=80000, tax=0, total=80000, status="PENDING", payment_type="MANUAL",
               manual_payment_method=None, manual_payment_note=None, billing_reason=None)
    row.update(kw)
    return BillingInvoiceOut(**row)


def test_signup_referral_fields_optional_and_bounded():
    base = dict(company_name="ISP", name="Ana", email="a@example.com", password="x" * 8)
    req = SignupCompanyRequest(**base)
    assert req.referral_code is None and req.tax_id is None and req.phone is None
    with pytest.raises(ValidationError):
        SignupCompanyRequest(**base, referral_code="X" * 13)
    with pytest.raises(ValidationError):
        SignupCompanyRequest(**base, tax_id="1" * 33)
    with pytest.raises(ValidationError):
        SignupCompanyRequest(**base, phone="5" * 33)


def test_discounted_legacy_invoices():           # Review Focus 4
    assert _inv(subtotal=80000, total=40000).discounted is True        # pre-feature discount, new cols 0
    assert _inv(subtotal=80000, total=80000).discounted is False       # pre-feature full price


def test_discounted_ignores_credits_and_balance():
    assert _inv(subtotal=80000, total=0, credit_cents=80000).discounted is False
    assert _inv(subtotal=80000, total=80200, previous_balance_cents=200).discounted is False
    assert _inv(subtotal=80000, total=0, credit_cents=40000).discounted is True  # 50 % discount, credit paid the rest
    assert _inv(subtotal=80000, total=0, carried_out_cents=300, credit_cents=79700).discounted is False


def test_discounted_is_serialised_and_inherited():
    data = _inv(subtotal=80000, total=40000).model_dump()
    assert data["discounted"] is True and data["credit_cents"] == 0
    assert "discounted" in BillingInvoiceWithCompany.model_computed_fields


def test_tenant_referral_row_has_no_amount():
    assert not {"amount_cents", "remaining_cents", "ineligible_reasons"} & set(TenantReferralRow.model_fields)
    row = TenantReferralRow(company_name=None, created_at=NOW, stage="ACTIVE")
    assert row.company_name is None
    with pytest.raises(ValidationError):
        TenantReferralRow(company_name="x", created_at=NOW, stage="BOGUS")
    assert set(REFERRAL_STAGES) == {"REGISTERED", "ACTIVE", "CREDIT_PENDING", "CREDIT_AVAILABLE",
                                    "NOT_ELIGIBLE", "REVERSED", "EXPIRED"}


def test_exception_update_partner_and_reward_decision():
    assert SubscriptionExceptionUpdate(partner=False).model_dump(exclude_unset=True) == {"partner": False}
    assert SubscriptionExceptionUpdate().partner is None
    with pytest.raises(ValidationError):
        RewardDecision(note="x" * 501)
    assert RewardDecision().note is None


def test_subscription_with_tier_referral_fields_default():
    fields = SubscriptionWithTier.model_fields
    for name, default in (("credit_balance_cents", 0), ("balance_due_cents", 0), ("discount_active", False),
                          ("is_partner", False), ("partner_since", None)):
        assert fields[name].default == default, name


def test_reward_out_from_attributes_and_reversal_link():
    class Row:  # an ORM-like object
        id = uuid.uuid4(); referral_id = uuid.uuid4(); company_id = uuid.uuid4()  # noqa: E702
        kind = "REFERRER_CREDIT"; status = "PENDING_APPROVAL"; amount_cents = 80000  # noqa: E702
        remaining_cents = 0; source_invoice_id = None; unlock_invoice_id = None  # noqa: E702
        ineligible_reasons = None; decided_at = None; decision_note = None  # noqa: E702
        unlocked_at = None; expires_at = None; reversal_id = None; reversed_at = None  # noqa: E702
        forfeited_at = None; clawed_cents = 0  # noqa: E702
    out = RewardOut.model_validate(Row())
    assert out.holds == [] and out.amount_cents == 80000
    out2 = RewardOut.model_validate(Row())
    out2.holds.append("X")
    assert RewardOut.model_validate(Row()).holds == []  # no shared mutable default
    with pytest.raises(ValidationError):
        ReversalLink(invoice_number="")
    with pytest.raises(ValidationError):
        ReversalLink(invoice_number="9" * 65)
    t = TenantReferralsOut(enabled=False, code=None, credit_balance_cents=0, balance_due_cents=0,
                           next_expiry=None, referrals=[], referred_by=None)
    assert t.enabled is False
