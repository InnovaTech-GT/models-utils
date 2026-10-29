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
