"""rtg2_run_trigger_backfill on a real Postgres (doc 68 §3.4, §3.10).

Run against a database already at `alembic upgrade head` (which includes
rtg2). Each test builds its own rows inside one rolled-back transaction and
calls the revision's upgrade() again; rows the migration already moved are
unaffected, so only the test's rows are checked.
"""
import os
import uuid
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from _mi_helpers import load
from alembic.migration import MigrationContext
from alembic.operations import Operations

pytestmark = [
    pytest.mark.pg,
    pytest.mark.skipif(not os.getenv("PG_TEST_URL"), reason="PG_TEST_URL not set"),
]

CRON_EMAIL = "system-cron@internal.erp"


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


def _user(conn, co, email):
    uid = uuid.uuid4()
    _x(conn, 'INSERT INTO "user" (id, created_at, name, email, age, password_hash, active, '
             "is_super_admin, company_id) VALUES (:id, now(), 'u', :e, 30, 'x', true, false, :co)",
       id=uid, e=email, co=co)
    return uid


def _world(conn, with_cron=True):
    co, client, plan, svc, pb = (uuid.uuid4() for _ in range(5))
    _x(conn, "INSERT INTO company (id, created_at, name, tier_id) "
             "SELECT :id, now(), :n, id FROM tier LIMIT 1", id=co, n=f"rtg2-{co}")
    _x(conn, "INSERT INTO client (id, created_at, name, company_id) VALUES (:id, now(), 'rtg2', :co)",
       id=client, co=co)
    _x(conn, "INSERT INTO service_plan (id, created_at, updated_at, name, price, is_active, "
             "plan_type, company_id) VALUES (:id, now(), now(), 'rtg2', 1, true, 'FIBER', :co)",
       id=plan, co=co)
    _x(conn, "INSERT INTO client_service (id, created_at, updated_at, company_id, client_id, "
             "service_plan_id) VALUES (:id, now(), now(), :co, :cl, :p)", id=svc, co=co, cl=client, p=plan)
    _x(conn, "INSERT INTO playbook (id, created_at, updated_at, company_id, name, definition, "
             "is_active, version) VALUES (:id, now(), now(), :co, 'rtg2', '{}', true, 1)", id=pb, co=co)
    admin = _user(conn, co, f"{uuid.uuid4()}@rtg2.test")
    cron = None
    if with_cron:
        _x(conn, 'INSERT INTO "user" (id, created_at, name, email, age, password_hash, active, '
                 "is_super_admin, company_id) VALUES (:id, now(), 'cron', :e, 30, 'x', true, false, :co) "
                 "ON CONFLICT (email) DO NOTHING", id=uuid.uuid4(), e=CRON_EMAIL, co=co)
        cron = _x(conn, 'SELECT id FROM "user" WHERE email = :e', e=CRON_EMAIL).scalar()
    return SimpleNamespace(co=co, svc=svc, pb=pb, admin=admin, cron=cron)


@pytest.fixture()
def world(conn):
    return _world(conn)


@pytest.fixture()
def world_without_cron(conn):
    if _x(conn, 'SELECT 1 FROM "user" WHERE email = :e', e=CRON_EMAIL).first():
        pytest.skip("the target DB already has a cron user")
    return _world(conn, with_cron=False)


def _run(conn, w, purpose, trigger, user_id):
    rid = uuid.uuid4()
    _x(conn, "INSERT INTO provisioning_run (id, created_at, updated_at, company_id, client_service_id, "
             "purpose, dry_run, status, path, plan, frames, triggered_by, triggered_by_user_id) "
             "VALUES (:id, now(), now(), :co, :svc, :purpose, false, 'SUCCEEDED', '[]', '[]', '{}', "
             ":trigger, :uid)", id=rid, co=w.co, svc=w.svc, purpose=purpose, trigger=trigger, uid=user_id)
    return rid


def _job(conn, w, run_id, trigger):
    jid = uuid.uuid4()
    _x(conn, "INSERT INTO provisioning_job (id, created_at, company_id, playbook_id, run_id, status, "
             "attempts, max_attempts, triggered_by, dry_run) VALUES (:id, now(), :co, :pb, :run, "
             "'SUCCEEDED', 0, 3, :trigger, false)", id=jid, co=w.co, pb=w.pb, run=run_id, trigger=trigger)
    return jid


def _trigger(conn, table, row_id):
    return _x(conn, f"SELECT triggered_by::text FROM {table} WHERE id = :id", id=row_id).scalar()


def _upgrade(conn):
    rtg2 = load("versions/rtg2_run_trigger_backfill.py", "rtg2_run_trigger_backfill")
    with Operations.context(MigrationContext.configure(conn)):
        rtg2.upgrade()


def test_cron_suspension_runs_and_their_children_become_auto_suspension(conn, world):
    run = _run(conn, world, "SUSPENSION", "USER", world.cron)
    job = _job(conn, world, run, "USER")
    _upgrade(conn)
    assert (_trigger(conn, "provisioning_run", run), _trigger(conn, "provisioning_job", job)) == ("AUTO_SUSPENSION",) * 2


def test_admin_suspensions_and_cron_reactivations_stay_user(conn, world):
    a = _run(conn, world, "SUSPENSION", "USER", world.admin)
    b = _run(conn, world, "REACTIVATION", "USER", world.cron)
    _upgrade(conn)
    assert {_trigger(conn, "provisioning_run", r) for r in (a, b)} == {"USER"}


def test_api_runs_and_their_children_become_bot(conn, world):
    run = _run(conn, world, "WIFI_PASSWORD", "API", None)
    job = _job(conn, world, run, "API")
    _upgrade(conn)
    assert (_trigger(conn, "provisioning_run", run), _trigger(conn, "provisioning_job", job)) == ("BOT",) * 2


def test_standalone_api_jobs_stay_api(conn, world):  # connectivity sweep jobs (run_id NULL)
    job = _job(conn, world, None, "API")
    _upgrade(conn)
    assert _trigger(conn, "provisioning_job", job) == "API"


def test_rerun_is_a_noop(conn, world):
    run = _run(conn, world, "SUSPENSION", "USER", world.cron)
    _upgrade(conn)
    _upgrade(conn)
    assert _trigger(conn, "provisioning_run", run) == "AUTO_SUSPENSION"


def _user_auto_counts(conn):  # API rows are left out: the second UPDATE rewrites them to BOT by design
    q = "SELECT triggered_by::text, count(*) FROM {} WHERE triggered_by IN ('USER', 'AUTO_SUSPENSION') GROUP BY 1"
    return {t: dict(conn.execute(sa.text(q.format(t))).all()) for t in ("provisioning_run", "provisioning_job")}


def test_without_the_cron_user_nothing_changes(conn, world_without_cron):  # spec §3.10 "a no-op without one"
    w = world_without_cron
    runs = [_run(conn, w, "SUSPENSION", "USER", None), _run(conn, w, "SUSPENSION", "USER", w.admin)]
    _job(conn, w, runs[0], "USER")
    before = _user_auto_counts(conn)
    _upgrade(conn)
    assert _user_auto_counts(conn) == before
    assert {_trigger(conn, "provisioning_run", r) for r in runs} == {"USER"}
