"""zm1_manual_step on a real Postgres (doc 42d §7): the PENDING_MANUAL enum
value, a parked manual job still holding its device lock and dedupe key, the
ZTP_MANUAL_STEP kind, and a downgrade that refuses while a job is parked.
Run against a database already at `alembic upgrade head`; each test runs in
one rolled-back transaction."""
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
    zm1 = load("versions/zm1_manual_step.py", "zm1_manual_step")
    with Operations.context(MigrationContext.configure(conn)):
        getattr(zm1, fn)()


def _raises(conn, fn):
    sp = conn.begin_nested()
    with pytest.raises(sa.exc.DBAPIError):
        fn()
    sp.rollback()


def _tenant(conn):
    co, user, pb = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    _x(conn, "INSERT INTO company (id, created_at, name, tier_id) "
             "SELECT :id, now(), :n, id FROM tier LIMIT 1", id=co, n=f"zm1-{co}")
    _x(conn, 'INSERT INTO "user" (id, created_at, name, email, age, password_hash, active, '
             "is_super_admin, company_id) VALUES (:id, now(), 'u', :e, 30, 'x', true, false, :co)",
       id=user, e=f"{user}@zm1.test", co=co)
    _x(conn, "INSERT INTO playbook (id, created_at, updated_at, company_id, name, definition, "
             "is_active, version) VALUES (:id, now(), now(), :co, 'zm1', '{}', true, 1)",
       id=pb, co=co)
    return co, user, pb


def _job(conn, co, pb, status, lock=None, key=None):
    _x(conn, "INSERT INTO provisioning_job (id, created_at, company_id, playbook_id, "
             "status, attempts, max_attempts, triggered_by, dry_run, device_lock_key, "
             "idempotency_key) VALUES (:id, now(), :co, :pb, :st, 0, 3, 'USER', false, "
             ":lock, :key)",
       id=uuid.uuid4(), co=co, pb=pb, st=status, lock=lock, key=key)


def test_parked_manual_job_holds_lock_and_key(conn):
    co, _, pb = _tenant(conn)
    lock = f"{co}:item:{uuid.uuid4()}"
    _job(conn, co, pb, "PENDING_MANUAL", lock=lock, key="k1")
    _raises(conn, lambda: _job(conn, co, pb, "QUEUED", lock=lock))
    _raises(conn, lambda: _job(conn, co, pb, "QUEUED", key="k1"))
    _job(conn, co, pb, "SUCCEEDED", lock=lock, key="k1")  # terminal rows never collide


def test_manual_step_kind(conn):
    co, user, _ = _tenant(conn)
    _x(conn, "INSERT INTO user_notification (id, created_at, kind, dedupe_key, company_id, "
             "user_id, push_state) VALUES (:id, now(), 'ZTP_MANUAL_STEP', 'm', :co, :u, 'PENDING')",
       id=uuid.uuid4(), co=co, u=user)


def test_run_index_predicate(conn):
    for name in ("uq_provisioning_run_company_idem", "uq_provisioning_job_company_idem",
                 "uq_provisioning_job_device_lock"):
        indexdef = _x(conn, "SELECT indexdef FROM pg_indexes WHERE indexname = :n", n=name).scalar()
        assert "'PENDING_MANUAL'" in indexdef, indexdef


def test_downgrade_refuses_while_parked_then_runs(conn):
    co, user, pb = _tenant(conn)
    _job(conn, co, pb, "PENDING_MANUAL", lock=f"{co}:item:x")
    sp = conn.begin_nested()
    with pytest.raises(RuntimeError, match="PENDING_MANUAL"):
        _run(conn, "downgrade")
    sp.rollback()
    _x(conn, "UPDATE provisioning_job SET status = 'CANCELLED' WHERE company_id = :co", co=co)
    _x(conn, "INSERT INTO user_notification (id, created_at, kind, dedupe_key, company_id, "
             "user_id) VALUES (:id, now(), 'ZTP_MANUAL_STEP', 'm', :co, :u)",
       id=uuid.uuid4(), co=co, u=user)
    _run(conn, "downgrade")
    assert _x(conn, "SELECT count(*) FROM user_notification WHERE user_id = :u",
              u=user).scalar() == 0
    indexdef = _x(conn, "SELECT indexdef FROM pg_indexes "
                        "WHERE indexname = 'uq_provisioning_job_device_lock'").scalar()
    assert "PENDING_MANUAL" not in indexdef
