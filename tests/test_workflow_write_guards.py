"""Workflow engine write guards (security audit 2026-11-23, #18 #19 #35).

UPDATE_FIELD / CREATE_ENTITY never move a row to another tenant, never point a
foreign key at another tenant's row, never create a provisioning job, never
write invoices or order items, and never change the status of an order that
has money on it."""
import uuid

import pytest
from conftest import CO_A, CO_B

from database_utils.models import (
    Client,
    InventoryItem,
    Order,
    PaymentStatus,
    ServicePlan,
    Task,
)
from database_utils.models.isp import ClientService, DeviceType, ProvisioningJob
from database_utils.utils.workflow_engine import (
    WORKFLOW_PROTECTED_FIELDS,
    _execute_create_entity,
    _execute_update_field,
)
from database_utils.utils.workflow_fields import get_writable_fields


def _client(db, co=CO_A):
    c = Client(company_id=co, name="c")
    db.add(c)
    db.flush()
    return c


def _plan(db, co=CO_A):
    p = ServicePlan(company_id=co, name=f"p-{uuid.uuid4().hex[:6]}", download_mbps=10, upload_mbps=5)
    db.add(p)
    db.flush()
    return p


def _service(db, co=CO_A):
    s = ClientService(company_id=co, client_id=_client(db, co).id, service_plan_id=_plan(db, co).id)
    db.add(s)
    db.flush()
    return s


def _order(db, co=CO_A, payment_status=PaymentStatus.PENDING):
    o = Order(company_id=co, client_id=_client(db, co).id, total=10.0, paid=False,
              payment_status=payment_status)
    db.add(o)
    db.flush()
    return o


def _row(db, rtype):
    if rtype == "client":
        return _client(db)
    if rtype == "client_service":
        return _service(db)
    if rtype == "task":
        t = Task(company_id=CO_A, name="t")
    else:
        dt = DeviceType(company_id=CO_A, name="dt", category_id=uuid.uuid4())
        db.add(dt)
        db.flush()
        t = InventoryItem(company_id=CO_A, serial_number="SN1", device_type_id=dt.id)
    db.add(t)
    db.flush()
    return t


def _update(db, rtype, row_id, updates, **extra):
    return _execute_update_field(
        db, {"resource_type": rtype, "updates": updates, **extra},
        {"trigger": {"resource_id": row_id}}, CO_A)


def test_protected_fields_constant():
    assert WORKFLOW_PROTECTED_FIELDS == frozenset({"id", "company_id", "created_at"})


@pytest.mark.parametrize("rtype", ["client", "client_service", "task", "inventory_item"])
def test_update_field_refuses_company_id(db, rtype):
    row = _row(db, rtype)
    with pytest.raises(ValueError, match="PROTECTED_FIELD"):
        _update(db, rtype, row.id, {"company_id": str(CO_B)})
    db.refresh(row)
    assert row.company_id == CO_A


@pytest.mark.parametrize("field", ["id", "created_at"])
def test_update_field_refuses_id_and_created_at(db, field):
    c = _client(db)
    with pytest.raises(ValueError, match="PROTECTED_FIELD"):
        _update(db, "client", c.id, {field: str(uuid.uuid4())})


def test_update_field_refuses_foreign_tenant_fk(db):
    svc = _service(db)
    old_plan = svc.service_plan_id
    foreign = _plan(db, CO_B)
    with pytest.raises(ValueError, match="FOREIGN_REFERENCE: service_plan_id"):
        _update(db, "client_service", svc.id, {"service_plan_id": str(foreign.id)})
    db.refresh(svc)
    assert svc.service_plan_id == old_plan


def test_update_field_refuses_missing_fk_target(db):
    svc = _service(db)
    with pytest.raises(ValueError, match="FOREIGN_REFERENCE: service_plan_id"):
        _update(db, "client_service", svc.id, {"service_plan_id": str(uuid.uuid4())})
    with pytest.raises(ValueError, match="FOREIGN_REFERENCE: service_plan_id"):
        _update(db, "client_service", svc.id, {"service_plan_id": "not-a-uuid"})


def test_update_field_accepts_own_tenant_fk_and_null(db):
    svc = _service(db)
    own = _plan(db)
    out = _update(db, "client_service", svc.id, {"service_plan_id": own.id})
    assert out["updated_fields"] == ["service_plan_id"]
    out = _update(db, "client_service", svc.id, {"cpe_item_id": None})
    assert out["updated_fields"] == ["cpe_item_id"]
    db.refresh(svc)
    assert svc.service_plan_id == own.id and svc.cpe_item_id is None


def test_fk_to_a_table_without_company_id_is_refused(db):
    t = _row(db, "task")
    with pytest.raises(ValueError, match="FOREIGN_REFERENCE: device_category_id"):
        _update(db, "task", t.id, {"device_category_id": str(uuid.uuid4())})


def test_update_field_skips_relationship_attributes(db):
    c = _client(db)
    out = _update(db, "client", c.id, {"company": None, "orders": []})
    assert out["updated_fields"] == []
    db.refresh(c)
    assert c.company_id == CO_A


def test_create_entity_refuses_provisioning_job(db):
    with pytest.raises(ValueError, match="ENQUEUE_PROVISIONING"):
        _execute_create_entity(db, {"resource_type": "provisioning_job",
                                    "data": {"client_service_id": str(_service(db).id)}}, {}, CO_A)
    assert db.query(ProvisioningJob).count() == 0


def test_create_entity_refuses_foreign_tenant_fk(db):
    foreign_client = _client(db, CO_B)
    with pytest.raises(ValueError, match="FOREIGN_REFERENCE: client_id"):
        _execute_create_entity(db, {"resource_type": "client_service",
                                    "data": {"client_id": str(foreign_client.id),
                                             "service_plan_id": str(_plan(db).id)}}, {}, CO_A)


def test_create_entity_refuses_protected_fields(db):
    with pytest.raises(ValueError, match="PROTECTED_FIELD"):
        _execute_create_entity(db, {"resource_type": "client",
                                    "data": {"name": "x", "company_id": str(CO_B)}}, {}, CO_A)
    with pytest.raises(ValueError, match="PROTECTED_FIELD"):
        _execute_create_entity(db, {"resource_type": "client",
                                    "data": {"name": "x", "id": str(uuid.uuid4())}}, {}, CO_A)


def test_create_entity_own_tenant_fk_still_works(db):
    out = _execute_create_entity(db, {"resource_type": "task",
                                      "data": {"name": "x", "client_id": _client(db).id}}, {}, CO_A)
    task = db.get(Task, uuid.UUID(out["resource_id"]))
    assert task.company_id == CO_A


def test_invoice_and_order_item_are_read_only(db):
    for rtype, updates in (("invoice", {"details": {}}), ("invoice", {"issue_date": "2026-01-01"}),
                           ("order_item", {"quantity": 0})):
        with pytest.raises(ValueError, match="trigger-only"):
            _update(db, rtype, uuid.uuid4(), updates)
    for rtype in ("invoice", "order_item"):
        with pytest.raises(ValueError, match="trigger-only"):
            _execute_create_entity(db, {"resource_type": rtype, "data": {}}, {}, CO_A)
    assert get_writable_fields("invoice") == [] and get_writable_fields("order_item") == []
    assert get_writable_fields("provisioning_job") == []


def test_order_status_refused_on_paid_order(db):
    paid = _order(db, payment_status=PaymentStatus.PAID)
    with pytest.raises(ValueError, match="PAID_ORDER"):
        _update(db, "order", paid.id, {"status": "CANCELLED"})
    for ps in (PaymentStatus.PARTIAL, PaymentStatus.REFUNDED):
        with pytest.raises(ValueError, match="PAID_ORDER"):
            _update(db, "order", _order(db, payment_status=ps).id, {"status": "CANCELLED"})
    pending = _order(db)
    assert _update(db, "order", pending.id, {"status": "CANCELLED"})["updated_fields"] == ["status"]


def test_match_field_mode_applies_the_same_guards(db):
    svc = _service(db)
    with pytest.raises(ValueError, match="PROTECTED_FIELD"):
        _execute_update_field(
            db, {"resource_type": "client_service", "updates": {"company_id": str(CO_B)},
                 "resource_id_source": "match_field", "match_field": "client_id"},
            {"trigger": {"resource_id": svc.client_id}}, CO_A)
    with pytest.raises(ValueError, match="FOREIGN_REFERENCE"):
        _execute_update_field(
            db, {"resource_type": "client_service",
                 "updates": {"service_plan_id": str(_plan(db, CO_B).id)},
                 "resource_id_source": "match_field", "match_field": "client_id"},
            {"trigger": {"resource_id": svc.client_id}}, CO_A)


def test_match_field_paid_order_guard(db):
    paid = _order(db, payment_status=PaymentStatus.PAID)
    with pytest.raises(ValueError, match="PAID_ORDER"):
        _execute_update_field(
            db, {"resource_type": "order", "updates": {"status": "CANCELLED"},
                 "resource_id_source": "match_field", "match_field": "client_id"},
            {"trigger": {"resource_id": paid.client_id}}, CO_A)


def test_update_field_refuses_provisioning_job(db):
    with pytest.raises(ValueError, match="ENQUEUE_PROVISIONING"):
        _update(db, "provisioning_job", uuid.uuid4(), {"status": "SUCCEEDED"})
