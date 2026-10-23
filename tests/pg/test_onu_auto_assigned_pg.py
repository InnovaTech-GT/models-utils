"""oa1_task_onu_auto_assigned on a real Postgres (doc 45 §4.3, §8): the
column and the one-off reservation backfill.

Run against a database already at `alembic upgrade head`. Each test runs in
one rolled-back transaction; the backfill is exercised by downgrade + upgrade.
"""
import os
import uuid

import pytest
import sqlalchemy as sa
from _mi_helpers import load
from alembic.migration import MigrationContext
from alembic.operations import Operations

pytestmark = [
    pytest.mark.pg,
    pytest.mark.skipif(not os.getenv("PG_TEST_URL"), reason="PG_TEST_URL not set"),
]


@pytest.fixture()
def conn():
    engine = sa.create_engine(os.environ["PG_TEST_URL"])
    with engine.connect() as connection:
        tx = connection.begin()
        yield connection
        tx.rollback()
    engine.dispose()


def _x(conn, sql, **params):
    return conn.execute(sa.text(sql), params)


def _run(conn, fn):
    oa1 = load("versions/oa1_task_onu_auto_assigned.py", "oa1_task_onu_auto_assigned")
    with Operations.context(MigrationContext.configure(conn)):
        getattr(oa1, fn)()


@pytest.fixture()
def co(conn):
    cid = uuid.uuid4()
    _x(conn, "INSERT INTO company (id, created_at, name, tier_id) "
             "SELECT :id, now(), :name, id FROM tier LIMIT 1", id=cid, name=f"oa1-{cid}")
    types = {}
    for key in ("ONU", "ROUTER"):
        types[key] = uuid.uuid4()
        _x(conn, "INSERT INTO device_type (id, created_at, updated_at, name, company_id, category_id, "
                 "is_serialized) SELECT :id, now(), now(), :key, :co, id, true "
                 "FROM device_category WHERE key = :key", id=types[key], key=key, co=cid)
    return cid, types


def _item(conn, co, kind="ONU", status="IN_STOCK"):
    cid, types = co
    iid = uuid.uuid4()
    _x(conn, "INSERT INTO inventory_item (id, created_at, updated_at, company_id, device_type_id, "
             "serial_number, status) VALUES (:id, now(), now(), :co, :t, :sn, :st)",
             id=iid, co=cid, t=types[kind], sn=f"SN{iid.hex[:8]}", st=status)
    return iid


def _user(conn, co):
    uid = uuid.uuid4()
    _x(conn, 'INSERT INTO "user" (id, created_at, name, email, age, password_hash, active, '
             "email_verified, company_id, is_super_admin) "
             "VALUES (:id, now(), 'tech', :email, 30, 'x', true, true, :co, false)",
             id=uid, email=f"{uid}@oa1.test", co=co[0])
    return uid


def _task(conn, co, item, kind="INSTALL", status="PENDING", assignees=(), created="now()"):
    tid = uuid.uuid4()
    _x(conn, "INSERT INTO task (id, created_at, updated_at, name, position, company_id, job_kind, "
             f"status, inventory_item_id) VALUES (:id, {created}, now(), 't', 0, :co, :k, :st, :item)",
             id=tid, co=co[0], k=kind, st=status, item=item)
    for user_id, role in assignees:
        _x(conn, "INSERT INTO task_assignee (task_id, user_id, role) VALUES (:t, :u, :r)",
           t=tid, u=user_id, r=role)
    return tid


def _status(conn, item):
    return _x(conn, "SELECT status FROM inventory_item WHERE id = :i", i=item).scalar()


def _events(conn, item):
    return _x(conn, "SELECT event_type, technician_id, event_metadata, company_id FROM equipment_event "
                    "WHERE item_id = :i", i=item).mappings().all()


def test_column_is_not_null_false(conn, co):
    item = _item(conn, co)
    task = _task(conn, co, item)
    assert _x(conn, "SELECT onu_auto_assigned FROM task WHERE id = :t", t=task).scalar() is False


def test_backfill_reserves_an_in_stock_onu_of_an_open_install(conn, co):
    item = _item(conn, co)
    low, high = sorted([_user(conn, co), _user(conn, co)])
    collector = _user(conn, co)
    task = _task(conn, co, item, status="ASSIGNED",
                 assignees=[(high, "TECHNICIAN"), (low, None), (collector, "COLLECTOR")])
    _run(conn, "downgrade")
    _run(conn, "upgrade")
    assert _status(conn, item) == "RESERVED"
    [event] = _events(conn, item)
    assert event["event_type"] == "RESERVED"
    assert event["company_id"] == co[0]
    # first technician = lowest user_id among TECHNICIAN-or-NULL assignees
    # (collectors never count)
    assert event["technician_id"] == low
    assert event["event_metadata"] == {"task_id": str(task), "auto": False, "backfill": True}
    # links stay manual
    assert _x(conn, "SELECT onu_auto_assigned FROM task WHERE id = :t", t=task).scalar() is False


def test_backfill_reserves_a_doubly_held_unit_once_and_without_technician(conn, co):
    item = _item(conn, co)
    first = _task(conn, co, item, created="now() - interval '1 day'")
    _task(conn, co, item)
    _run(conn, "downgrade")
    _run(conn, "upgrade")
    [event] = _events(conn, item)
    assert event["technician_id"] is None
    assert event["event_metadata"]["task_id"] == str(first)


@pytest.mark.parametrize("item_kind,item_status,task_kind,task_status", [
    ("ONU", "IN_STOCK", "INSTALL", "DONE"),        # closed task
    ("ONU", "IN_STOCK", "FAULT", "PENDING"),       # affected-device link, by design
    ("ROUTER", "IN_STOCK", "INSTALL", "PENDING"),  # not an ONU
    ("ONU", "RESERVED", "INSTALL", "PENDING"),     # already reserved
    ("ONU", "INSTALLED", "INSTALL", "IN_PROGRESS"),
])
def test_backfill_leaves_other_units_alone(conn, co, item_kind, item_status, task_kind, task_status):
    item = _item(conn, co, kind=item_kind, status=item_status)
    _task(conn, co, item, kind=task_kind, status=task_status)
    _run(conn, "downgrade")
    _run(conn, "upgrade")
    assert _status(conn, item) == item_status
    assert _events(conn, item) == []


def test_down_then_up_is_idempotent(conn, co):
    item = _item(conn, co)
    _task(conn, co, item)
    _run(conn, "downgrade")
    assert _x(conn, "SELECT count(*) FROM information_schema.columns WHERE table_name = 'task' "
                    "AND column_name = 'onu_auto_assigned'").scalar() == 0
    _run(conn, "upgrade")
    _run(conn, "upgrade")
    assert len(_events(conn, item)) == 1
    # downgrade keeps the backfilled reservation (correct data)
    _run(conn, "downgrade")
    assert _status(conn, item) == "RESERVED"
