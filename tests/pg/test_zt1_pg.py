"""zt1_ztp_trigger on a real Postgres (doc 43 §4): the extended kinds CHECK,
the push-outbox index, user_push_token's constraints, and a downgrade that
deletes ZTP_* rows first. Run against a database already at
`alembic upgrade head`; each test runs in one rolled-back transaction."""
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
    zt1 = load("versions/zt1_ztp_trigger.py", "zt1_ztp_trigger")
    with Operations.context(MigrationContext.configure(conn)):
        getattr(zt1, fn)()


def _tenant(conn):
    co, user = uuid.uuid4(), uuid.uuid4()
    _x(conn, "INSERT INTO company (id, created_at, name, tier_id) "
             "SELECT :id, now(), :n, id FROM tier LIMIT 1", id=co, n=f"zt1-{co}")
    _x(conn, 'INSERT INTO "user" (id, created_at, name, email, age, password_hash, active, '
             "is_super_admin, company_id) VALUES (:id, now(), 'u', :e, 30, 'x', true, false, :co)", id=user, e=f"{user}@zt1.test", co=co)
    return co, user


def _notify(conn, co, user, kind, key=None):
    _x(conn, "INSERT INTO user_notification (id, created_at, kind, dedupe_key, company_id, user_id) "
             "VALUES (:id, now(), :k, :d, :co, :u)",
       id=uuid.uuid4(), k=kind, d=key or f"{kind}:{uuid.uuid4()}", co=co, u=user)


def _token(conn, co, user, token="ExponentPushToken[a]", platform="android", app="tecnicos"):
    _x(conn, "INSERT INTO user_push_token (id, created_at, updated_at, token, platform, app, "
             "company_id, user_id) VALUES (:id, now(), now(), :t, :p, :a, :co, :u)",
       id=uuid.uuid4(), t=token, p=platform, a=app, co=co, u=user)


def _raises(conn, fn):
    sp = conn.begin_nested()
    with pytest.raises(sa.exc.DBAPIError):
        fn()
    sp.rollback()


def test_kinds_check_and_push_index(conn):
    co, user = _tenant(conn)
    for kind in ("TASK_ASSIGNED", "ZTP_SUCCEEDED", "ZTP_FAILED", "ZTP_NEEDS_ATTENTION",
                 "ZTP_ROLLBACK_INCOMPLETE"):
        _notify(conn, co, user, kind)
    _raises(conn, lambda: _notify(conn, co, user, "ZTP_OTHER"))
    definition = _x(conn, "SELECT indexdef FROM pg_indexes "
                          "WHERE indexname = 'ix_user_notification_push_pending'").scalar()
    assert "WHERE" in definition and "'PENDING'" in definition


def test_push_token_constraints(conn):
    co, user = _tenant(conn)
    _token(conn, co, user)
    _raises(conn, lambda: _token(conn, co, user))  # token is unique
    _raises(conn, lambda: _token(conn, co, user, token="t2", platform="web"))
    _raises(conn, lambda: _token(conn, co, user, token="t3", app="cobros"))
    _x(conn, 'DELETE FROM "user" WHERE id = :u', u=user)
    assert _x(conn, "SELECT count(*) FROM user_push_token WHERE user_id = :u", u=user).scalar() == 0


def test_downgrade_deletes_ztp_rows_then_upgrade_again(conn):
    co, user = _tenant(conn)
    _notify(conn, co, user, "ZTP_FAILED")
    _notify(conn, co, user, "TASK_OVERDUE")
    _run(conn, "downgrade")
    kinds = [r[0] for r in _x(conn, "SELECT kind FROM user_notification WHERE user_id = :u", u=user)]
    assert kinds == ["TASK_OVERDUE"]
    assert _x(conn, "SELECT to_regclass('user_push_token')").scalar() is None
    _raises(conn, lambda: _notify(conn, co, user, "ZTP_FAILED"))
    _run(conn, "upgrade")
    _run(conn, "upgrade")  # idempotent
    _notify(conn, co, user, "ZTP_FAILED")
    _token(conn, co, user)
