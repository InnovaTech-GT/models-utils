"""rf1 (doc 69 §5): referral / partner schema contract on the models
(SQLite create_all). The revision itself is guarded on Postgres by
tests/pg/test_rf1_pg.py."""
import uuid
from datetime import timedelta

import pytest
from _mi_helpers import load
from sqlalchemy.exc import IntegrityError

from database_utils.models import (
    BillingCard,
    BillingReversal,
    CompanyIdentity,
    Referral,
    ReferralIdentity,
    ReferralReward,
)
from database_utils.models.auth import BillingInvoice, Company, Subscription, Tier
from database_utils.utils.timezone_utils import now_gt


def test_rf1_revision_chain():
    m = load("versions/rf1_referral_programs.py", "rf1_test")
    assert m.revision == "rf1_referral_programs" and m.down_revision == "et1_edge_telnet"
    assert len(m.revision) <= 32


def _company(db, **kw):
    tier = Tier(name=f"T-{uuid.uuid4().hex[:8]}", price=800)
    db.add(tier)
    db.flush()
    co = Company(name=f"ISP-{uuid.uuid4().hex[:8]}", tier_id=tier.id, **kw)
    db.add(co)
    db.flush()
    return co


def _referral(db, referrer=None, referred=None, **kw):
    referrer = referrer or _company(db)
    referred = referred or _company(db)
    r = Referral(referrer_company_id=referrer.id, referred_company_id=referred.id, code="K7QX2M",
                 referred_nit="576937K", referred_email="a@example.com", referred_phone="50255551234",
                 nit_seen_before=False, **kw)
    db.add(r)
    db.flush()
    return r


def _reward(db, referral, **kw):
    row = {"referral_id": referral.id, "company_id": referral.referred_company_id,
           "kind": "FREE_MONTH", "status": "PENDING_APPROVAL"}
    row.update(kw)
    rw = ReferralReward(**row)
    db.add(rw)
    db.flush()
    return rw


def _refused(db, fn):
    with pytest.raises(IntegrityError), db.begin_nested():
        fn()
        db.flush()


def test_reward_status_and_kind_checks(db):
    r = _referral(db)
    _refused(db, lambda: _reward(db, r, status="BOGUS"))
    _refused(db, lambda: _reward(db, r, kind="X"))
    assert _reward(db, r).remaining_cents == 0


def test_reward_unique_per_kind(db):
    r = _referral(db)
    _reward(db, r)
    _refused(db, lambda: _reward(db, r))
    _reward(db, r, kind="REFERRER_CREDIT", company_id=r.referrer_company_id)


def test_remaining_not_above_amount(db):
    r = _referral(db)
    _refused(db, lambda: _reward(db, r, amount_cents=100, remaining_cents=101))
    _refused(db, lambda: _reward(db, r, amount_cents=None, remaining_cents=1))
    _refused(db, lambda: _reward(db, r, amount_cents=-1))
    assert _reward(db, r, amount_cents=100, remaining_cents=100).remaining_cents == 100


def test_referral_not_self(db):
    co = _company(db)
    _refused(db, lambda: _referral(db, referrer=co, referred=co))


def test_referred_company_unique(db):
    referred = _company(db)
    _referral(db, referred=referred)
    _refused(db, lambda: _referral(db, referred=referred))


def test_company_identity_unique(db):
    co = _company(db)

    def add(kind="NIT", value="576937K"):
        db.add(CompanyIdentity(company_id=co.id, kind=kind, value=value))
        db.flush()

    add()
    _refused(db, add)
    _refused(db, lambda: add(kind="CARD", value="x"))
    add(kind="EMAIL", value="576937K")


def test_referral_identity_unique_and_card_kind(db):
    r = _referral(db)

    def add(kind="CARD", value="visa:4242:12/30"):
        db.add(ReferralIdentity(referral_id=r.id, kind=kind, value=value))
        db.flush()

    add()
    _refused(db, add)
    _refused(db, lambda: add(kind="BOGUS"))
    add(kind="NIT", value="576937K")


def test_referral_code_unique(db):
    _company(db, referral_code="K7QX2M")
    _refused(db, lambda: _company(db, referral_code="K7QX2M"))
    _company(db)  # NULL codes never collide
    _company(db)


def test_money_columns_non_negative(db):
    co = _company(db)

    def sub(**kw):
        s = Subscription(company_id=kw.pop("company_id", co.id), tier_id=co.tier_id,
                         current_period_start=now_gt(), current_period_end=now_gt() + timedelta(days=30), **kw)
        db.add(s)
        db.flush()
        return s

    _refused(db, lambda: sub(balance_due_cents=-1))
    s = sub()
    assert s.balance_due_cents == 0 and s.partner_since is None

    def inv(**kw):
        db.add(BillingInvoice(invoice_number=f"U-{uuid.uuid4().hex[:8]}", invoice_date=now_gt(),
                              due_date=now_gt(), subtotal=80000, total=80000, subscription_id=s.id, **kw))
        db.flush()

    for col in ("credit_cents", "previous_balance_cents", "carried_out_cents", "refunded_cents"):
        _refused(db, lambda col=col: inv(**{col: -1}))
    inv(credit_cents=100, credit_detail=[{"reward_id": str(uuid.uuid4()), "cents": 100}],
        recurrente_payment_id="on_x")

    r = _referral(db)
    _refused(db, lambda: _reward(db, r, clawed_cents=-1))


def test_reversal_kind_and_gateway_unique(db):
    def add(kind="REFUND", gateway_id="re_1"):
        db.add(BillingReversal(kind=kind, gateway_id=gateway_id))
        db.flush()

    _refused(db, lambda: add(kind="X", gateway_id="re_x"))
    add()
    _refused(db, add)
    add(kind="DISPUTE", gateway_id="di_1")


def test_billing_card_row(db):
    co = _company(db)
    db.add(BillingCard(company_id=co.id, payment_method_id="pm_1", card_key="visa:4242:12/30"))
    db.flush()


def test_no_orm_relationship_to_company():
    for model in (Referral, ReferralReward, BillingReversal, BillingCard, CompanyIdentity, ReferralIdentity):
        assert not model.__mapper__.relationships, model.__name__
    names = {r.mapper.class_.__name__ for r in Company.__mapper__.relationships}
    assert not names & {"Referral", "ReferralReward", "BillingCard", "CompanyIdentity"}
