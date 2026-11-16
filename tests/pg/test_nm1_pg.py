"""nm1_network_managed on a real Postgres (doc 71 §9): re-running the upgrade
backfills one managed and one unmanaged service, ck_task_office_action holds,
and the downgrade drops every column (re-upgrade is idempotent). Run against a
database already at `alembic upgrade head`; each test runs in one rolled-back
transaction."""
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
    nm1 = load("versions/nm1_network_managed.py", "nm1_network_managed")
    with Operations.context(MigrationContext.configure(conn)):
        getattr(nm1, fn)()


def _tenant(conn):
    co, cl, plan = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    _x(conn, "INSERT INTO company (id, created_at, name, tier_id) "
             "SELECT :id, now(), :n, id FROM tier LIMIT 1", id=co, n=f"nm1-{co}")
    _x(conn, "INSERT INTO client (id, created_at, name, company_id) VALUES (:id, now(), 'c', :co)",
       id=cl, co=co)
    _x(conn, "INSERT INTO service_plan (id, created_at, updated_at, name, plan_type, price, "
             "is_active, company_id) VALUES (:id, now(), now(), 'p', 'FIBER', 1, true, :co)",
       id=plan, co=co)
    return co, cl, plan


def _service(conn, co, cl, plan):
    svc = uuid.uuid4()
    _x(conn, "INSERT INTO client_service (id, created_at, updated_at, company_id, client_id, "
             "service_plan_id) VALUES (:id, now(), now(), :co, :cl, :pl)",
       id=svc, co=co, cl=cl, pl=plan)
    return svc


def _run_row(conn, co, svc, purpose, minutes, status="SUCCEEDED", dry=False):
    _x(conn, "INSERT INTO provisioning_run (id, created_at, updated_at, purpose, dry_run, status, "
             "path, plan, frames, company_id, client_service_id) VALUES (:id, "
             "now() + make_interval(mins => :m), now(), :p, :dry, :st, '[]', '[]', '{}', :co, :svc)",
       id=uuid.uuid4(), m=minutes, p=purpose, dry=dry, st=status, co=co, svc=svc)


def _managed(conn, svc):
    return _x(conn, "SELECT network_managed FROM client_service WHERE id = :id", id=svc).scalar()


def _columns(conn):
    return set(_x(conn, "SELECT table_name || '.' || column_name FROM information_schema.columns "
                        "WHERE (table_name, column_name) IN (('client_service','network_managed'),"
                        "('task','office_action'),('task','provisioning_run_id'),"
                        "('task','completed_by'),('task','completion_note'))").scalars())


def test_backfill_check_and_downgrade(conn):
    co, cl, plan = _tenant(conn)
    managed, unmanaged = _service(conn, co, cl, plan), _service(conn, co, cl, plan)
    _run_row(conn, co, managed, "ACTIVATION", 0)
    _run_row(conn, co, unmanaged, "ACTIVATION", 0)
    _run_row(conn, co, unmanaged, "DEPROVISION", 5)
    _run_row(conn, co, unmanaged, "ACTIVATION", 10, dry=True)
    assert _managed(conn, managed) is False  # new rows default false

    _run(conn, "upgrade")  # idempotent on a head database; runs the backfill
    assert _managed(conn, managed) is True
    assert _managed(conn, unmanaged) is False

    ins = ("INSERT INTO task (id, created_at, updated_at, name, position, company_id, "
           "office_action, job_kind) VALUES (:id, now(), now(), 't', 0, :co, :oa, :jk)")
    _x(conn, ins, id=uuid.uuid4(), co=co, oa="NETWORK_CUT", jk=None)
    for oa, jk in (("NETWORK_CUT", "INSTALL"), ("NOPE", None)):
        with pytest.raises(sa.exc.IntegrityError):
            with conn.begin_nested():
                _x(conn, ins, id=uuid.uuid4(), co=co, oa=oa, jk=jk)

    _run(conn, "downgrade")
    assert _columns(conn) == set()
    assert _x(conn, "SELECT to_regclass('ix_task_provisioning_run_id')").scalar() is None
    _run(conn, "upgrade")
    assert len(_columns(conn)) == 5
    assert _managed(conn, managed) is True
