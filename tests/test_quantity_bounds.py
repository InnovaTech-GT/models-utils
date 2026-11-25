"""Quantity and price bounds (security audit #68): quantity >= 1 on order items
and services (schemas + DB CHECK), never-negative plan prices on writes."""
import uuid
from datetime import datetime

import pytest
import sqlalchemy as sa
from _mi_helpers import load
from conftest import CO_A
from pydantic import ValidationError

from database_utils.models import Order, OrderItem
from database_utils.models.isp import ClientService
from database_utils.schemas.client_service import (
    ClientServiceBillingUpdate,
    ClientServiceCreate,
)
from database_utils.schemas.order_item import (
    OrderItemCreate,
    OrderItemInput,
    OrderItemUpdate,
)
from database_utils.schemas.service_plan import (
    ServicePlanCreate,
    ServicePlanOut,
    ServicePlanUpdate,
)


@pytest.mark.parametrize("q", [0, -1])
def test_order_item_quantity_must_be_positive(q):
    with pytest.raises(ValidationError):
        OrderItemCreate(service_plan_id=uuid.uuid4(), quantity=q)
    with pytest.raises(ValidationError):
        OrderItemInput(service_plan_id=uuid.uuid4(), quantity=q)
    with pytest.raises(ValidationError):
        OrderItemUpdate(quantity=q)
    assert OrderItemCreate(service_plan_id=uuid.uuid4(), quantity=1).quantity == 1


@pytest.mark.parametrize("q", [0, -3])
def test_service_quantity_must_be_positive(q):
    with pytest.raises(ValidationError):
        ClientServiceBillingUpdate(quantity=q)
    with pytest.raises(ValidationError):
        ClientServiceCreate(client_id=uuid.uuid4(), service_plan_id=uuid.uuid4(), quantity=q)
    assert ClientServiceBillingUpdate(quantity=None).quantity is None


@pytest.mark.parametrize("field", ["price", "price_cents", "installation_price_cents"])
def test_plan_price_cannot_be_negative(field):
    with pytest.raises(ValidationError, match="NEGATIVE_PRICE"):
        ServicePlanCreate(name="p", **{field: -1})
    with pytest.raises(ValidationError, match="NEGATIVE_PRICE"):
        ServicePlanUpdate(**{field: -1})
    assert ServicePlanCreate(name="p", **{field: 0})
    assert ServicePlanUpdate(**{field: 0})


def test_plan_out_still_reads_legacy_negative():
    out = ServicePlanOut.model_validate({
        "id": uuid.uuid4(), "company_id": uuid.uuid4(), "created_at": datetime(2026, 1, 1),
        "name": "legacy", "price": -5.0, "price_cents": -500,
    })
    assert out.price == -5.0


def test_model_checks_declared():
    for model, name in ((OrderItem, "ck_order_item_quantity_positive"),
                        (ClientService, "ck_client_service_quantity_positive")):
        ck = next(c for c in model.__table__.constraints if c.name == name)
        assert str(ck.sqltext) == "quantity >= 1"


def test_sqlite_check_backstops_a_bypassing_writer(db):
    order = Order(company_id=CO_A, client_id=uuid.uuid4(), total=1.0, paid=False)
    db.add(order)
    db.flush()
    db.add(OrderItem(order_id=order.id, quantity=0))
    with pytest.raises(sa.exc.IntegrityError, match="ck_order_item_quantity_positive|CHECK"):
        db.flush()


def test_sec1_revision_chain():
    m = load("versions/sec1_quantity_checks.py", "sec1_quantity_checks")
    assert (m.revision, m.down_revision) == ("sec1_quantity_checks", "dm1_device_metrics")
