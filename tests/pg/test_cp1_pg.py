"""cp1_dunning on a real Postgres at head: the partial unique index allows one
OPEN promise per service (others free), enum/CHECK constraints hold, and the
CAPTIVE_PORTAL role holds exactly captive_portal.access. Each test runs in one
rolled-back transaction."""
import os
import uuid

import pytest
import sqlalchemy as sa

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


def _fixture(conn):
    co, cl, svc = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    _x(conn, "INSERT INTO company (id, created_at, name, tier_id) "
             "SELECT :id, now(), :n, id FROM tier LIMIT 1", id=co, n=f"cp1-{co}")
    _x(conn, "INSERT INTO client (id, created_at, name, company_id) VALUES (:id, now(), 'c', :co)",
       id=cl, co=co)
    plan = uuid.uuid4()
    _x(conn, "INSERT INTO service_plan (id, created_at, updated_at, name, plan_type, price, "
             "is_active, company_id) VALUES (:id, now(), now(), 'p', 'FIBER', 1, true, :co)",
       id=plan, co=co)
    _x(conn, "INSERT INTO client_service (id, created_at, updated_at, company_id, client_id, "
             "service_plan_id) VALUES (:id, now(), now(), :co, :cl, :pl)",
       id=svc, co=co, cl=cl, pl=plan)
    return co, cl, svc


def _promise(conn, co, cl, svc, status="OPEN", source="PORTAL"):
    _x(conn, "INSERT INTO payment_promise (id, created_at, promised_date, source, status, "
             "company_id, client_id, client_service_id) "
             "VALUES (:id, now(), current_date, :src, :st, :co, :cl, :svc)",
       id=uuid.uuid4(), src=source, st=status, co=co, cl=cl, svc=svc)


def test_one_open_promise_per_service(conn):
    co, cl, svc = _fixture(conn)
    _promise(conn, co, cl, svc)
    _promise(conn, co, cl, svc, status="BROKEN")
    _promise(conn, co, cl, svc, status="KEPT")
    with pytest.raises(sa.exc.IntegrityError):
        with conn.begin_nested():
            _promise(conn, co, cl, svc)
    with pytest.raises(sa.exc.DataError):
        with conn.begin_nested():
            _promise(conn, co, cl, svc, source="FAX")


def test_dunning_run_mode_check(conn):
    co, _, _ = _fixture(conn)
    ins = ("INSERT INTO dunning_run (id, ran_at, mode, outcome, candidates, suspended, skipped, "
           "company_id) VALUES (:id, now(), :m, 'OK', '[]', '[]', '[]', :co)")
    _x(conn, ins, id=uuid.uuid4(), m="REPORT", co=co)
    with pytest.raises(sa.exc.IntegrityError):
        with conn.begin_nested():
            _x(conn, ins, id=uuid.uuid4(), m="OFF", co=co)


def test_captive_portal_role_grants(conn):
    perms = _x(conn, "SELECT p.name FROM role r JOIN role_permission rp ON rp.role_id = r.id "
                     "JOIN permission p ON p.id = rp.permission_id "
                     "WHERE r.name = 'CAPTIVE_PORTAL' AND r.company_id IS NULL").scalars().all()
    assert perms == ["captive_portal.access"]
