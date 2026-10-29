"""payment_link in the workflow engine (pl1): a trigger resource, never a write
target. UPDATE_FIELD and CREATE_ENTITY refuse it whatever the field, so an
automation cannot mark a link PAID or forge one."""
import uuid
from datetime import timedelta

import pytest
from conftest import CO_A

from database_utils.models import PaymentLink, PaymentLinkStatus
from database_utils.utils.timezone_utils import now_gt
from database_utils.utils.workflow_engine import (
    KNOWN_RESOURCE_TYPES,
    READ_ONLY_RESOURCE_TYPES,
    _execute_create_entity,
    _execute_update_field,
    _get_model_map,
)
from database_utils.utils.workflow_fields import (
    get_resource_fields,
    get_writable_fields,
)


def _pending(db):
    link = PaymentLink(
        company_id=CO_A, client_id=uuid.uuid4(), order_id=uuid.uuid4(),
        status=PaymentLinkStatus.PENDING, amount_cents=15000,
        expires_at=now_gt() + timedelta(hours=120),
        checkout_id="ch_wf", checkout_url="https://app.recurrente.com/checkout-session/ch_wf",
    )
    db.add(link)
    db.commit()
    return link


def test_payment_link_is_a_known_trigger_resource():
    assert "payment_link" in KNOWN_RESOURCE_TYPES
    assert _get_model_map()["payment_link"] is PaymentLink
    assert "payment_link" in READ_ONLY_RESOURCE_TYPES


def test_fields_are_for_trigger_conditions_only():
    names = {f["name"] for f in get_resource_fields("payment_link")}
    assert {"status", "amount_cents", "review_reason", "order_id", "client_id"} <= names
    assert get_writable_fields("payment_link") == []


@pytest.mark.parametrize("updates", [
    {"status": "PAID"},
    {"payment_id": str(uuid.uuid4()), "paid_at": "2026-10-28T10:00:00-06:00"},
    {"review_reason": None},
    {"no_such_column": 1},
    {},
])
def test_update_field_refuses_a_payment_link(db, updates):
    link = _pending(db)
    context = {"trigger": {"resource_id": link.id}}
    with pytest.raises(ValueError, match="trigger-only"):
        _execute_update_field(db, {"resource_type": "payment_link", "updates": updates}, context, CO_A)
    db.refresh(link)
    assert link.status == PaymentLinkStatus.PENDING
    assert link.payment_id is None


def test_update_field_refuses_it_by_match_field_too(db):
    link = _pending(db)
    context = {"trigger": {"resource_id": link.order_id}}
    config = {"resource_type": "payment_link", "updates": {"status": "PAID"},
              "resource_id_source": "match_field", "match_field": "order_id"}
    with pytest.raises(ValueError, match="trigger-only"):
        _execute_update_field(db, config, context, CO_A)
    db.refresh(link)
    assert link.status == PaymentLinkStatus.PENDING


def test_create_entity_refuses_a_payment_link(db):
    data = {"client_id": str(uuid.uuid4()), "order_id": str(uuid.uuid4()), "status": "PAID",
            "amount_cents": 500, "expires_at": "2026-11-01T00:00:00-06:00"}
    with pytest.raises(ValueError, match="trigger-only"):
        _execute_create_entity(db, {"resource_type": "payment_link", "data": data}, {}, CO_A)
    assert db.query(PaymentLink).count() == 0


def test_other_resources_still_writable(db):
    with pytest.raises(ValueError, match="not found"):
        _execute_update_field(db, {"resource_type": "task", "updates": {"name": "x"}},
                              {"trigger": {"resource_id": uuid.uuid4()}}, CO_A)
