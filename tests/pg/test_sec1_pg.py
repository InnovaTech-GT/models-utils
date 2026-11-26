"""sec1_quantity_checks on a real Postgres: both CHECKs at head, the refusing
pre-check, and downgrade/upgrade round-trips."""
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


def _company(conn):
    cid = uuid.uuid4()
    tier = conn.execute(sa.text("SELECT id FROM tier LIMIT 1")).scalar()
    conn.execute(sa.text("INSERT INTO company (id, created_at, name, tier_id) "
                         "VALUES (:id, now(), :n, :t)"),
                 {"id": cid, "n": f"sec1-{cid}", "t": tier})
    return cid


def _client(conn, co):
    cid = uuid.uuid4()
    conn.execute(sa.text("INSERT INTO client (id, created_at, name, company_id, code) "
                         "VALUES (:id, now(), 'c', :co, :code)"),
                 {"id": cid, "co": co, "code": cid.hex[:8].upper()})
    return cid


def _plan(conn, co):
    pid = uuid.uuid4()
    conn.execute(sa.text("INSERT INTO service_plan (id, created_at, updated_at, name, company_id, price, plan_type, is_active) "
                         "VALUES (:id, now(), now(), :n, :co, 0, 'FIBER', true)"), {"id": pid, "co": co, "n": f"p-{pid}"})
    return pid


def _order(conn, co):
    oid = uuid.uuid4()
    conn.execute(sa.text("INSERT INTO \"order\" (id, created_at, total, paid, company_id, client_id) "
                         "VALUES (:id, now(), 0, false, :co, :cl)"),
                 {"id": oid, "co": co, "cl": _client(conn, co)})
    return oid


def _insert_service(conn, co, quantity):
    conn.execute(sa.text("INSERT INTO client_service (id, created_at, updated_at, company_id, client_id, "
                         "service_plan_id, quantity) VALUES (:id, now(), now(), :co, :cl, :pl, :q)"),
                 {"id": uuid.uuid4(), "co": co, "cl": _client(conn, co), "pl": _plan(conn, co), "q": quantity})


def _insert_item(conn, co, quantity):
    conn.execute(sa.text("INSERT INTO order_item (id, created_at, order_id, quantity) "
                         "VALUES (:id, now(), :o, :q)"),
                 {"id": uuid.uuid4(), "o": _order(conn, co), "q": quantity})


def test_order_item_quantity_check(conn):
    co = _company(conn)
    _insert_item(conn, co, 1)
    with conn.begin_nested() as sp:
        with pytest.raises(sa.exc.IntegrityError, match="ck_order_item_quantity_positive"):
            _insert_item(conn, co, 0)
        sp.rollback()


def test_client_service_quantity_check(conn):
    co = _company(conn)
    _insert_service(conn, co, 1)
    with conn.begin_nested() as sp:
        with pytest.raises(sa.exc.IntegrityError, match="ck_client_service_quantity_positive"):
            _insert_service(conn, co, -1)
        sp.rollback()


def test_round_trip_and_precheck(conn):
    sec1 = load("versions/sec1_quantity_checks.py", "sec1_quantity_checks")
    co = _company(conn)
    with Operations.context(MigrationContext.configure(conn)):
        sec1.downgrade()
        sec1.downgrade()
        _insert_item(conn, co, 0)
        with conn.begin_nested() as sp:
            with pytest.raises(RuntimeError, match="order_item=1, client_service=0"):
                sec1.upgrade()
            sp.rollback()
        conn.execute(sa.text("UPDATE order_item SET quantity = 1 WHERE quantity < 1"))
        sec1.upgrade()
        sec1.upgrade()
    names = set(conn.execute(sa.text(
        "SELECT conname FROM pg_constraint WHERE conname LIKE 'ck_%_quantity_positive'")).scalars())
    assert {"ck_order_item_quantity_positive", "ck_client_service_quantity_positive"} <= names
