"""mi1/mi2: model surface, revision chain, and revision <-> model parity."""
import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from database_utils.models import (
    CashMovement, CashSession, CashSessionStatus, Company, EquipmentEventType,
    InventoryItem, Order, Payment, Task, TaskJobKind, TaskMaterial, UploadedFile,
    UserNotification, USER_NOTIFICATION_KINDS, Warehouse,
)
from database_utils.models.auth import _USER_NOTIFICATION_KIND_CHECK

from _mi_helpers import mi1, mi2


def test_chain_position():
    assert mi1().down_revision == ("dr1_task_route_sequence", "lp1_link_ports")
    assert mi2().down_revision == "mi1_mobile_enum_labels"


def test_mi1_labels_match_the_python_enums():
    labels = mi1().NEW_LABELS
    assert TaskJobKind(labels["taskjobkind"][0]) is TaskJobKind.RELOCATION
    assert CashSessionStatus(labels["cashsessionstatus"][0]) is CashSessionStatus.DEPOSITED
    assert {EquipmentEventType(v) for v in labels["equipmenteventtype"]} == {
        EquipmentEventType.CONSUMED, EquipmentEventType.RELEASED,
    }


def test_notification_kind_check_matches_model():
    # zt1 extended the CHECK; test_zt1_ztp_trigger pins mi2's literal as zt1's pre-image.
    for kind in USER_NOTIFICATION_KINDS:
        assert f"'{kind}'" in _USER_NOTIFICATION_KIND_CHECK


def test_receivables_index_where_matches_model():
    idx = next(i for i in Order.__table__.indexes if i.name == "ix_order_open_receivables")
    assert str(idx.dialect_options["postgresql"]["where"]) == mi2().OPEN_RECEIVABLES_WHERE


def test_every_mi2_column_is_on_the_model():
    tables = {
        "task": Task, "inventory_item": InventoryItem, "warehouse": Warehouse,
        "cash_session": CashSession, "payment": Payment,
        "uploaded_file": UploadedFile, "company": Company,
    }
    for table, column, ddl in mi2()._ADD_COLUMNS:
        col = tables[table].__table__.columns[column]
        # NOT NULL in the revision <=> NOT NULL on the model.
        assert col.nullable is ("NOT NULL" not in ddl), f"{table}.{column}"


def test_every_mi2_index_is_on_the_model():
    names = set()
    for model in (Payment, UploadedFile, Order, TaskMaterial, UserNotification, CashMovement):
        names |= {i.name for i in model.__table__.indexes}
    assert set(mi2()._INDEX_NAMES) <= names


def _company(db):
    from database_utils.models.auth import Tier
    tier = Tier(id=uuid.uuid4(), name=f"T{uuid.uuid4()}", price=1, billing_cycle="MONTHLY")
    db.add(tier)
    db.flush()
    co = Company(id=uuid.uuid4(), name=f"C{uuid.uuid4()}", tier_id=tier.id,
                 mobile_settings={"collector_daily_goal": 15})
    db.add(co)
    db.flush()
    return co


def _device_type(db, company_id):
    from database_utils.models import DeviceType
    dt = DeviceType(id=uuid.uuid4(), company_id=company_id, name="Fibra", category_id=uuid.uuid4())
    db.add(dt)
    db.flush()
    return dt


def test_create_all_defaults(db):
    co = _company(db)
    task = Task(name="t", company_id=co.id)
    db.add(task)
    db.flush()
    db.refresh(task)
    assert task.step_progress == {}
    assert task.started_at is None and task.completed_at is None
    assert co.mobile_settings == {"collector_daily_goal": 15}


def test_task_material_is_unique_per_task_and_type(db):
    co = _company(db)
    dt = _device_type(db, co.id)
    task = Task(name="t", company_id=co.id)
    db.add(task)
    db.flush()
    db.add(TaskMaterial(company_id=co.id, task_id=task.id, device_type_id=dt.id, quantity=30))
    db.flush()
    db.add(TaskMaterial(company_id=co.id, task_id=task.id, device_type_id=dt.id, quantity=5))
    with pytest.raises(IntegrityError):
        db.flush()


def test_task_material_quantity_must_be_positive(db):
    co = _company(db)
    dt = _device_type(db, co.id)
    task = Task(name="t", company_id=co.id)
    db.add(task)
    db.flush()
    db.add(TaskMaterial(company_id=co.id, task_id=task.id, device_type_id=dt.id, quantity=0))
    with pytest.raises(IntegrityError):
        db.flush()


def test_user_notification_dedupe(db):
    from database_utils.models import User
    co = _company(db)
    user = User(id=uuid.uuid4(), name="U", email=f"{uuid.uuid4()}@e.com", age=30,
                password_hash="x", company_id=co.id)
    db.add(user)
    db.flush()
    db.add(UserNotification(company_id=co.id, user_id=user.id, kind="TASK_ASSIGNED", dedupe_key="task:1"))
    db.flush()
    db.add(UserNotification(company_id=co.id, user_id=user.id, kind="TASK_ASSIGNED", dedupe_key="task:1"))
    with pytest.raises(IntegrityError):
        db.flush()


def test_cash_movement_hangs_off_the_session(db):
    from database_utils.models import User
    co = _company(db)
    user = User(id=uuid.uuid4(), name="U", email=f"{uuid.uuid4()}@e.com", age=30,
                password_hash="x", company_id=co.id)
    db.add(user)
    db.flush()
    cs = CashSession(company_id=co.id, collector_id=user.id)
    db.add(cs)
    db.flush()
    db.refresh(cs)
    assert cs.opening_cents == 0 and cs.reopen_count == 0
    mv_id = uuid.uuid4()
    db.add(CashMovement(id=mv_id, company_id=co.id, cash_session_id=cs.id, amount_cents=5000))
    db.flush()
    db.refresh(cs)
    assert [m.id for m in cs.movements] == [mv_id]
