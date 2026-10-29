"""Doc 61 schema contract: usage-pricing fields and their bounds."""
import pytest
from pydantic import ValidationError

from database_utils.models.auth import BillingInvoice, Subscription, Tier
from database_utils.schemas.subscription import SubscriptionExceptionUpdate
from database_utils.schemas.tier import TierUpdate


def test_columns_mapped():
    for model, cols in (
        (Tier, ("included_clients", "extra_client_price", "quote_above_clients")),
        (Subscription, ("period_peak_clients", "custom_monthly_price")),
        (BillingInvoice, ("period_start", "period_end", "billed_clients")),
    ):
        for col in cols:
            assert col in model.__table__.c, (model.__name__, col)
    assert Subscription.__table__.c.period_peak_clients.server_default.arg == "0"


def test_bounds():
    assert TierUpdate(included_clients=1000, extra_client_price=0.8).extra_client_price == 0.8
    with pytest.raises(ValidationError):
        TierUpdate(extra_client_price=-1)
    assert SubscriptionExceptionUpdate(custom_monthly_price=None).custom_monthly_price is None
    with pytest.raises(ValidationError):
        SubscriptionExceptionUpdate(custom_monthly_price=-5)


# --- doc 61 §7: billable count, client cap, quote stop, billing access ---------

import uuid as _uuid
from datetime import timedelta

from fastapi import HTTPException

from database_utils.models.crm import Client
from database_utils.models.isp import ClientService, ClientServiceStatus
from database_utils.utils import tier_limits as tl
from database_utils.utils.timezone_utils import now_gt

CO = _uuid.uuid4()


def _world(db, cap_min=3, ratio=1.1, quote=None, overrides=None):
    tier = Tier(name=f"U-{_uuid.uuid4().hex[:5]}", price=800, quote_above_clients=quote,
                features={"max_users": 2, "client_cap_min": cap_min, "client_cap_ratio": ratio})
    db.add(tier)
    db.flush()
    sub = Subscription(company_id=CO, tier_id=tier.id, status="ACTIVE", billing_type="MANUAL",
                       current_period_start=now_gt(), current_period_end=now_gt() + timedelta(days=30),
                       limit_overrides=overrides)
    db.add(sub)
    db.flush()
    return sub


def _client(db, status=None, deactivated=False):
    c = Client(id=_uuid.uuid4(), name="c", company_id=CO,
               deactivated_at=now_gt() if deactivated else None)
    db.add(c)
    db.flush()
    if status:
        db.add(ClientService(id=_uuid.uuid4(), company_id=CO, client_id=c.id,
                             service_plan_id=_uuid.uuid4(), status=status))
        db.flush()
    return c


def test_billable_counts_every_non_cancelled_service_of_active_clients(db):
    _world(db)
    _client(db, ClientServiceStatus.ACTIVE)
    _client(db, ClientServiceStatus.SUSPENDED)
    _client(db, ClientServiceStatus.PENDING_INSTALL)
    _client(db, ClientServiceStatus.CANCELLED)
    _client(db, ClientServiceStatus.ACTIVE, deactivated=True)
    _client(db)  # no service
    assert tl.billable_clients(db, CO) == 3


def test_client_cap_floor_ratio_and_override(db):
    sub = _world(db, cap_min=3, ratio=1.1)
    assert tl.client_cap(sub, 0) == 3 and tl.client_cap(sub, 100) == 110
    for _ in range(3):
        _client(db)
    _client(db, deactivated=True)  # never counts
    with pytest.raises(HTTPException) as e:
        tl.check_tier_limit(db, CO, "clients")
    assert "CLIENT_CAP_REACHED" in e.value.detail
    sub.limit_overrides = {"client_cap_min": 10}
    db.flush()
    tl.check_tier_limit(db, CO, "clients")  # raised per tenant


def test_quote_stop_blocks_clients_and_services_until_custom_price(db):
    sub = _world(db, cap_min=100, quote=2)
    _client(db, ClientServiceStatus.ACTIVE)
    _client(db, ClientServiceStatus.ACTIVE)
    for resource in ("clients", "services"):
        with pytest.raises(HTTPException) as e:
            tl.check_tier_limit(db, CO, resource)
        assert "QUOTE_REQUIRED" in e.value.detail
    sub.custom_monthly_price = 3000
    db.flush()
    tl.check_tier_limit(db, CO, "services")


def test_users_limit_override(db):
    sub = _world(db)
    assert tl.effective_limit(sub, "max_users") == 2
    sub.limit_overrides = {"max_users": -1}
    assert tl.effective_limit(sub, "max_users") is None


def test_billing_access_ladder(db):
    sub = _world(db)
    assert tl.billing_access(db, CO) == tl.ACCESS_FULL

    def pending(days_overdue):
        db.query(BillingInvoice).delete()
        db.add(BillingInvoice(invoice_number=f"I-{_uuid.uuid4().hex[:6]}", invoice_date=now_gt(),
                              due_date=now_gt() - timedelta(days=days_overdue), subtotal=1, total=1,
                              status="PENDING", payment_type="MANUAL", subscription_id=sub.id))
        db.flush()
        return tl.billing_access(db, CO)

    assert pending(14) == tl.ACCESS_FULL
    assert pending(15) == tl.ACCESS_READ_ONLY
    assert pending(30) == tl.ACCESS_LOCKED
    db.query(BillingInvoice).delete()
    sub.status = "INCOMPLETE"
    db.flush()
    assert tl.billing_access(db, CO) == tl.ACCESS_LOCKED


def test_partner_discount_keeps_full_access(db):
    sub = _world(db)
    db.add(BillingInvoice(invoice_number="I-OLD", invoice_date=now_gt(), due_date=now_gt() - timedelta(days=90),
                          subtotal=1, total=1, status="PENDING", payment_type="MANUAL", subscription_id=sub.id))
    sub.status = "INCOMPLETE"
    sub.discount_percent = 100
    db.flush()
    assert tl.billing_access(db, CO) == tl.ACCESS_FULL
    sub.discount_until = now_gt() - timedelta(days=1)  # discount over: debt counts again
    db.flush()
    assert tl.billing_access(db, CO) == tl.ACCESS_LOCKED
    sub.discount_until, sub.status = None, "CANCELED"
    db.flush()
    assert tl.billing_access(db, CO) == tl.ACCESS_LOCKED
