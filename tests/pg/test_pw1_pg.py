"""pw1_provisioning_event on a real Postgres: at head the outbox table exists
with bigserial ids, server defaults and the partial pending index; the
downgrade drops it and the upgrade re-creates it, both idempotent. Run
against a database already at `alembic upgrade head`; each test runs in one
rolled-back transaction."""
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
    pw1 = load("versions/pw1_provisioning_event.py", "pw1_provisioning_event")
    with Operations.context(MigrationContext.configure(conn)):
        getattr(pw1, fn)()


def _index(conn):
    return _x(conn, "SELECT indexdef FROM pg_indexes WHERE tablename = 'provisioning_event' "
                    "AND indexname = 'ix_provisioning_event_pending'").scalar()


def _table(conn):
    return _x(conn, "SELECT to_regclass('provisioning_event')").scalar()


def test_head_has_the_outbox_with_defaults_and_partial_index(conn):
    assert "WHERE (processed_at IS NULL)" in _index(conn)
    co = uuid.uuid4()
    _x(conn, "INSERT INTO company (id, created_at, name, tier_id) "
             "SELECT :id, now(), :n, id FROM tier LIMIT 1", id=co, n=f"pw1-{co}")
    ids = [_x(conn, "INSERT INTO provisioning_event (company_id, kind, payload) "
                    "VALUES (:co, 'RUN_CLOSED', CAST(:p AS jsonb)) RETURNING id",
              co=co, p='{"status": "SUCCEEDED"}').scalar() for _ in range(2)]
    assert ids[1] > ids[0]
    row = _x(conn, "SELECT attempts, created_at, processed_at, payload->>'status' "
                   "FROM provisioning_event WHERE id = :id", id=ids[0]).one()
    assert row[0] == 0 and row[1] is not None and row[2] is None and row[3] == "SUCCEEDED"
    with pytest.raises(sa.exc.IntegrityError):
        with conn.begin_nested():
            _x(conn, "INSERT INTO provisioning_event (company_id, kind) VALUES (:co, 'X')",
               co=uuid.uuid4())  # company FK


def test_downgrade_drops_then_upgrade_recreates(conn):
    _run(conn, "downgrade")
    _run(conn, "downgrade")  # idempotent
    assert _table(conn) is None
    _run(conn, "upgrade")
    _run(conn, "upgrade")  # idempotent
    assert _table(conn) == "provisioning_event"
    assert _index(conn) is not None
