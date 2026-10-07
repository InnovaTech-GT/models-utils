"""pd1: client.payment_day is optional and range-checked (1..31)."""
import pytest
from pydantic import ValidationError

from database_utils.models.crm import Client
from database_utils.schemas.client import ClientUpdate


def test_model_has_nullable_payment_day_with_check():
    col = Client.__table__.c.payment_day
    assert col.nullable
    assert "ck_client_payment_day_range" in {c.name for c in Client.__table__.constraints}


@pytest.mark.parametrize("bad", [0, 32, -1])
def test_schema_rejects_out_of_range(bad):
    with pytest.raises(ValidationError):
        ClientUpdate(payment_day=bad)


def test_schema_accepts_bounds_and_none():
    assert ClientUpdate(payment_day=1).payment_day == 1
    assert ClientUpdate(payment_day=31).payment_day == 31
    assert ClientUpdate().payment_day is None
